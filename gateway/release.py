"""Versions signées de Lezard IA (32.3) : le serveur central publie, le logiciel vérifie avant d'installer.

Format : « LZU1.<contenu>.<signature> » (base64url), contenu JSON :
    {"v": 1, "kid": "u1", "version": "1.2.0", "url": "https://…/LezardIA-Setup-1.2.0.exe",
     "sha256": "<64 hex>", "size": <octets>, "notes": "…", "published": <date>,
     "zip": {"url": "https://…/LezardIA-1.2.0.zip", "sha256": "<64 hex>", "size": <octets>}}
« zip » (facultatif) : la même version sans installateur, pour une installation faite depuis le zip ; les versions
qui ne le connaissent pas l'ignorent.
Signature Ed25519 de « LZU1.<contenu> », par une clé à part de celle des licences : une fuite de l'une ne permet
pas de fabriquer l'autre. Le logiciel n'installe qu'un fichier dont la taille et l'empreinte SHA-256 sont celles
de la version signée : un installateur modifié, ou servi par un faux serveur, est refusé.

Outils (sur la machine qui détient la clé privée) :
    python -m gateway.release genkey --out chemin.key       crée une paire, affiche la clé publique à intégrer
    python -m gateway.release sign --key chemin.key --installer LezardIA-Setup-1.2.0.exe
        --url https://…/LezardIA-Setup-1.2.0.exe [--version 1.2.0] [--notes "…"] --out data/release.lzu
        [--zip LezardIA-1.2.0.zip --zip-url https://…/LezardIA-1.2.0.zip] [--signed]
--signed : installateur signé (Authenticode) ; les installations faites depuis le zip passent alors à l'installateur à
leur prochaine mise à jour (même dossier, mêmes choix, données gardées).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .license import _b64, _unb64, load_private, public_key_b64

PREFIX = "LZU1."
PUBLIC_KEYS = {"u1": "sx2FdJpBK64Dg9JVSUiqxfNKKXb4ESgrAajjeWrLgqg="}
VERSION = re.compile(r"^\d{1,4}(\.\d{1,4}){1,3}$")


class ReleaseError(ValueError):
    pass


def version_tuple(version: str) -> tuple[int, ...]:
    if not VERSION.match(version or ""):
        raise ReleaseError(f"Numéro de version illisible : {version!r}.")
    parts = tuple(int(p) for p in version.split("."))
    return parts + (0,) * (4 - len(parts))


def newer(candidate: str, current: str) -> bool:
    return version_tuple(candidate) > version_tuple(current)


def _download(data, what: str) -> dict:
    """Fichier annoncé : adresse https, empreinte SHA-256 et taille, ou ReleaseError."""
    try:
        out = {"url": str(data["url"]), "sha256": str(data["sha256"]).lower(), "size": int(data["size"])}
    except (KeyError, TypeError, ValueError):
        raise ReleaseError(f"Annonce de version incomplète ({what}).")
    if urlparse(out["url"]).scheme != "https" or not re.fullmatch(r"[0-9a-f]{64}", out["sha256"]) or out["size"] <= 0:
        raise ReleaseError(f"Annonce de version incomplète ({what}).")
    return out


def verify(token: str, keys: dict[str, str] | None = None) -> dict:
    """Contenu vérifié d'une version signée, ou ReleaseError avec la raison."""
    token = (token or "").strip()
    if not token.startswith(PREFIX) or token.count(".") != 2:
        raise ReleaseError("Annonce de version illisible.")
    body, sig = token[len(PREFIX) :].split(".")
    try:
        data = json.loads(_unb64(body))
        signature = _unb64(sig)
    except (ValueError, UnicodeDecodeError):
        raise ReleaseError("Annonce de version illisible.")
    if not isinstance(data, dict) or data.get("v") != 1:
        raise ReleaseError("Format de version inconnu : mettez Lezard IA à jour à la main.")
    raw = (keys or PUBLIC_KEYS).get(str(data.get("kid")))
    if not raw:
        raise ReleaseError("Version signée par une clé inconnue.")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(raw)).verify(signature, (PREFIX + body).encode())
    except (InvalidSignature, ValueError):
        raise ReleaseError("Signature de la version invalide : annonce modifiée ou contrefaite, rien n'est installé.")
    try:
        out = {
            "version": str(data["version"]),
            "url": str(data["url"]),
            "sha256": str(data["sha256"]).lower(),
            "size": int(data["size"]),
            "notes": str(data.get("notes") or ""),
            "published": float(data.get("published") or 0),
        }
    except (KeyError, TypeError, ValueError):
        raise ReleaseError("Annonce de version incomplète.")
    version_tuple(out["version"])
    if urlparse(out["url"]).scheme != "https" or not re.fullmatch(r"[0-9a-f]{64}", out["sha256"]) or out["size"] <= 0:
        raise ReleaseError("Annonce de version incomplète.")
    out["zip"] = _download(data["zip"], "zip") if data.get("zip") is not None else None
    # installateur signé (Authenticode) : les installations faites depuis le zip passent à l'installateur
    out["signed"] = data.get("signed") is True
    return out


def sign(private_key: Ed25519PrivateKey, kid: str, manifest: dict) -> str:
    data = {"v": 1, "kid": kid, **manifest}
    body = _b64(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode())
    return f"{PREFIX}{body}.{_b64(private_key.sign((PREFIX + body).encode()))}"


def file_digest(path: str | Path) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Versions signées de Lezard IA (machine qui détient la clé privée).")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("genkey", help="crée une paire de clés")
    g.add_argument("--out", required=True)
    s = sub.add_parser("sign", help="signe l'annonce d'un installateur")
    s.add_argument("--key", required=True)
    s.add_argument("--kid", default="u1")
    s.add_argument("--installer", required=True)
    s.add_argument("--url", required=True)
    s.add_argument("--version")
    s.add_argument("--notes", default="")
    s.add_argument("--zip", help="la même version en zip (installation sans installateur)")
    s.add_argument("--zip-url")
    s.add_argument("--signed", action="store_true",
                   help="installateur signé (Authenticode) : les installations zip passent à l'installateur")  # fmt: skip
    s.add_argument("--out", required=True)
    a = p.parse_args(argv)
    if a.cmd == "genkey":
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        with open(a.out, "xb") as f:  # jamais d'écrasement d'une clé existante
            f.write(pem)
        print(f"Clé privée : {a.out} (à garder secrète, hors du dépôt)\nClé publique : {public_key_b64(key)}")
        return 0
    version = a.version or (re.search(r"(\d+(?:\.\d+){1,3})\.exe$", a.installer) or [None, ""])[1]
    sha, size = file_digest(a.installer)
    manifest = {"version": version, "url": a.url, "sha256": sha, "size": size, "notes": a.notes,
                "published": int(time.time())}  # fmt: skip
    if a.zip:
        if not a.zip_url:
            print("--zip-url manquant : adresse publique du zip")
            return 2
        zsha, zsize = file_digest(a.zip)
        manifest["zip"] = {"url": a.zip_url, "sha256": zsha, "size": zsize}
    if a.signed:
        manifest["signed"] = True
    token = sign(load_private(a.key), a.kid, manifest)
    verify(token, {a.kid: public_key_b64(load_private(a.key))})  # annonce relue avant d'être publiée
    Path(a.out).write_text(token + "\n", encoding="ascii")
    print(f"Version {version} signée : {a.out} ({size} octets, SHA-256 {sha[:16]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
