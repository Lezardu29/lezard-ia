"""Chiffrement au repos des clés API des providers (tâche 4.4).

AES-256-GCM (chiffrement authentifié) ; format stocké : "v1:" + base64(nonce 12 o | chiffré+tag).
Le nom du provider sert de données associées (AAD) : un secret recopié sur une autre ligne
ne se déchiffre pas.

Clé de chiffrement (32 octets), par ordre de priorité :
1. GATEWAY_ENCRYPTION_KEY : 32 octets en base64 ou hex, ou une phrase de passe (dérivée par scrypt) ;
2. sinon fichier $GATEWAY_ENCRYPTION_KEY_FILE (défaut data/encryption.key), généré au 1er lancement.

Rotation de la clé : python -m gateway.crypto rotate --old <ancienne> --new <nouvelle> [--db chemin]
"""

from __future__ import annotations

import argparse
import base64
import binascii
import functools
import hashlib
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = "v1"


class CryptoError(Exception):
    pass


def raw_key(material: str) -> bytes | None:
    """Les 32 octets d'une clé fournie en base64 ou hex, sinon None (phrase de passe)."""
    m = material.strip()
    for decode in (lambda s: base64.b64decode(s, validate=True), bytes.fromhex):
        try:
            raw = decode(m)
            if len(raw) == 32:
                return raw
        except (binascii.Error, ValueError):
            pass
    return None


@functools.lru_cache(maxsize=8)
def derive_key(material: str) -> bytes:
    """32 octets depuis base64/hex brut, sinon dérivation scrypt d'une phrase de passe."""
    return raw_key(material) or hashlib.scrypt(
        material.strip().encode(), salt=b"smart-ai-gateway/v1", n=2**14, r=8, p=1, dklen=32
    )


def key_file_path() -> str:
    return os.getenv("GATEWAY_ENCRYPTION_KEY_FILE", os.path.join("data", "encryption.key"))


def load_key() -> bytes:
    env = os.getenv("GATEWAY_ENCRYPTION_KEY")
    if env:
        return derive_key(env)
    path = key_file_path()
    if os.path.exists(path):
        with open(path, encoding="ascii") as f:
            return derive_key(f.read())
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(base64.b64encode(key).decode())
    return key


class Cipher:
    def __init__(self, key: bytes | None = None):
        key = key if key is not None else load_key()
        if len(key) != 32:
            raise CryptoError("La clé de chiffrement doit faire 32 octets (AES-256).")
        self._aes = AESGCM(key)
        self._key = key  # pour export_key seulement (18.2)
        self.fingerprint = hashlib.sha256(key).hexdigest()[:12]  # identifie la clé sans la révéler

    def export_key(self, passphrase: str) -> dict:
        return wrap_key(self._key, passphrase)

    def encrypt(self, plaintext: str, context: str) -> str:
        nonce = secrets.token_bytes(12)
        ct = self._aes.encrypt(nonce, plaintext.encode(), context.encode())
        return f"{VERSION}:{base64.b64encode(nonce + ct).decode()}"

    def decrypt(self, token: str, context: str) -> str:
        try:
            version, payload = token.split(":", 1)
            if version != VERSION:
                raise CryptoError(f"Format de chiffrement inconnu '{version}'.")
            raw = base64.b64decode(payload)
            return self._aes.decrypt(raw[:12], raw[12:], context.encode()).decode()
        except (InvalidTag, ValueError, binascii.Error) as e:
            raise CryptoError("Déchiffrement impossible (mauvaise clé ou donnée altérée).") from e


# ---------- export de la clé, protégé par une phrase de passe (18.2) ----------
# Fichier JSON à garder À PART de la base (clé USB, gestionnaire de mots de passe) : sans lui, une sauvegarde de
# la base restaurée sur un disque neuf garde ses clés virtuelles mais perd les clés API des fournisseurs.
KEY_EXPORT_FORMAT = "smart-ai-gateway/encryption-key"
KEY_EXPORT_MIN_PASSPHRASE = 12
_SCRYPT = {"n": 2**15, "r": 8, "p": 1}


def _wrapping_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=128 * r * n * 2)


def wrap_key(key: bytes, passphrase: str) -> dict:
    if len(passphrase) < KEY_EXPORT_MIN_PASSPHRASE:
        raise CryptoError(f"Phrase de passe trop courte : {KEY_EXPORT_MIN_PASSPHRASE} caractères minimum.")
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    wrapped = AESGCM(_wrapping_key(passphrase, salt, **_SCRYPT)).encrypt(nonce, key, KEY_EXPORT_FORMAT.encode())
    return {
        "format": KEY_EXPORT_FORMAT,
        "version": 1,
        "kdf": "scrypt",
        **_SCRYPT,
        "salt": base64.b64encode(salt).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "key": base64.b64encode(wrapped).decode(),
        "fingerprint": hashlib.sha256(key).hexdigest()[:12],
    }


def unwrap_key(data: dict, passphrase: str) -> bytes:
    try:
        if data.get("format") != KEY_EXPORT_FORMAT or data.get("version") != 1 or data.get("kdf") != "scrypt":
            raise CryptoError("Ce fichier n'est pas un export de clé de la Smart AI Gateway.")
        n, r, p = int(data["n"]), int(data["r"]), int(data["p"])
        if not (2**14 <= n <= 2**20 and 1 <= r <= 16 and 1 <= p <= 4):  # pas de calcul démesuré imposé
            raise CryptoError("Paramètres de dérivation invalides.")
        wrapping = _wrapping_key(passphrase, base64.b64decode(data["salt"]), n, r, p)
        key = AESGCM(wrapping).decrypt(
            base64.b64decode(data["nonce"]), base64.b64decode(data["key"]), KEY_EXPORT_FORMAT.encode()
        )
    except (KeyError, TypeError, ValueError, binascii.Error) as e:
        raise CryptoError("Fichier d'export de clé illisible.") from e
    except InvalidTag as e:
        raise CryptoError("Phrase de passe incorrecte (ou fichier modifié).") from e
    if len(key) != 32:
        raise CryptoError("Clé exportée invalide.")
    return key


def mask(secret: str) -> str:
    return f"…{secret[-4:]}" if len(secret) > 8 else "…"


def main(argv: list[str] | None = None) -> None:
    from .store import DocumentStore

    parser = argparse.ArgumentParser(prog="python -m gateway.crypto")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rot = sub.add_parser("rotate", help="rechiffre tous les secrets avec une nouvelle clé")
    rot.add_argument("--old", required=True)
    rot.add_argument("--new", required=True)
    rot.add_argument("--db", default=None)
    sub.add_parser("genkey", help="génère une clé aléatoire (base64)")
    args = parser.parse_args(argv)
    if args.cmd == "genkey":
        print(base64.b64encode(secrets.token_bytes(32)).decode())
        return
    n = DocumentStore(args.db).reencrypt_credentials(Cipher(derive_key(args.old)), Cipher(derive_key(args.new)))
    print(f"{n} secret(s) rechiffré(s). Pensez à mettre à jour GATEWAY_ENCRYPTION_KEY.")


if __name__ == "__main__":
    main()
