"""Licence signée d'accès par le site (30.4) : le serveur central signe, le logiciel vérifie avec la clé publique intégrée.

Format : « LZ1.<contenu>.<signature> » (base64url), contenu JSON :
    {"v": 1, "kid": "k1", "lic": "…", "acct": "…", "edition": "pro", "device": "<instance_id> | *",
     "iat": <émise le>, "exp": <valable jusqu'au>}
Signature Ed25519 de « LZ1.<contenu> ». Modifier un seul caractère l'invalide ; sans la clé privée (gardée par le
serveur central, jamais dans ce dépôt), impossible d'en fabriquer une.
- device : l'identifiant de cette installation (Store.instance_id) — une licence copiée sur un autre PC y est
  refusée ; « * » pour une licence non liée (installation de développement).
- délai de grâce : GRACE_DAYS jours après `exp`, pour renouveler hors ligne (30.4).

Outils (sur la machine qui détient la clé privée) :
    python -m gateway.license genkey --out chemin.key          crée une paire, affiche la clé publique à intégrer
    python -m gateway.license issue --key chemin.key --account X [--device ID] [--days 35]
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PREFIX = "LZ1."
GRACE_DAYS = 7
# clés publiques du serveur de licences (rotation : ajouter la nouvelle, garder l'ancienne le temps du renouvellement)
PUBLIC_KEYS = {"k1": "OLZla174a0mWGPsRcaSd5n88eWcAZYR4M9x+bTOj3yo="}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class LicenseError(ValueError):
    pass


@dataclass
class License:
    edition: str
    account: str
    device: str
    issued: float
    expires: float
    license_id: str

    def status(self, now: float | None = None) -> str:
        """valide | grace (expirée, encore active le temps de renouveler) | expiree"""
        now = time.time() if now is None else now
        if now <= self.expires:
            return "valide"
        return "grace" if now <= self.expires + GRACE_DAYS * 86400 else "expiree"

    def public(self, now: float | None = None) -> dict:
        return {
            "status": self.status(now),
            "edition": self.edition,
            "account": self.account,
            "device": self.device,
            "expires": self.expires,
            "grace_until": self.expires + GRACE_DAYS * 86400,
        }


def verify(token: str, instance_id: str | None, keys: dict[str, str] | None = None) -> License:
    """Licence lue et vérifiée, ou LicenseError avec la raison (affichée telle quelle à l'administrateur)."""
    token = (token or "").strip()
    if not token.startswith(PREFIX) or token.count(".") != 2:
        raise LicenseError("Code de licence illisible : copiez-le en entier (il commence par LZ1.).")
    body, sig = token[len(PREFIX) :].split(".")
    try:
        data = json.loads(_unb64(body))
        signature = _unb64(sig)
    except (ValueError, UnicodeDecodeError):
        raise LicenseError("Code de licence illisible : copiez-le en entier (il commence par LZ1.).")
    if not isinstance(data, dict) or data.get("v") != 1:
        raise LicenseError("Version de licence inconnue : mettez Lezard IA à jour.")
    raw = (keys or PUBLIC_KEYS).get(str(data.get("kid")))
    if not raw:
        raise LicenseError("Licence signée par une clé inconnue : mettez Lezard IA à jour.")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(raw)).verify(signature, (PREFIX + body).encode())
    except (InvalidSignature, ValueError):
        raise LicenseError("Licence invalide : la signature ne correspond pas (code modifié ou contrefait).")
    device = str(data.get("device") or "")
    if device != "*" and device != instance_id:
        raise LicenseError("Cette licence appartient à une autre installation : activez-la depuis votre compte.")
    try:
        lic = License(
            edition=str(data["edition"]),
            account=str(data["acct"]),
            device=device,
            issued=float(data["iat"]),
            expires=float(data["exp"]),
            license_id=str(data["lic"]),
        )
    except (KeyError, TypeError, ValueError):
        raise LicenseError("Licence incomplète.")
    if lic.edition != "pro":
        raise LicenseError(f"Offre inconnue dans la licence : {lic.edition}.")
    return lic


def issue(private_key: Ed25519PrivateKey, kid: str, account: str, device: str, days: float) -> str:
    now = time.time()
    data = {"v": 1, "kid": kid, "lic": secrets.token_hex(8), "acct": account, "edition": "pro", "device": device,
            "iat": int(now), "exp": int(now + days * 86400)}  # fmt: skip
    body = _b64(json.dumps(data, separators=(",", ":")).encode())
    return f"{PREFIX}{body}.{_b64(private_key.sign((PREFIX + body).encode()))}"


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def load_private(path: str) -> Ed25519PrivateKey:
    with open(path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("Ce fichier n'est pas une clé Ed25519.")
    return key


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Licences d'accès par le site (machine qui détient la clé privée).")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("genkey", help="crée une paire de clés")
    g.add_argument("--out", required=True)
    i = sub.add_parser("issue", help="émet une licence")
    i.add_argument("--key", required=True)
    i.add_argument("--kid", default="k1")
    i.add_argument("--account", required=True)
    i.add_argument("--device", default="*")
    i.add_argument("--days", type=float, default=35)
    a = p.parse_args(argv)
    if a.cmd == "genkey":
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        with open(a.out, "xb") as f:  # jamais d'écrasement d'une clé existante
            f.write(pem)
        print(f"Clé privée : {a.out} (à garder secrète, hors du dépôt)\nClé publique : {public_key_b64(key)}")
    else:
        print(issue(load_private(a.key), a.kid, a.account, a.device, a.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
