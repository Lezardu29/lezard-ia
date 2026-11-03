"""HTTPS sur le réseau local (12.7).

En mode réseau local, les clés et les conversations circulaient en HTTP clair sur le Wi-Fi. Avec
GATEWAY_TLS_PORT (lancer.ps1 -Lan -Https), la gateway ouvre en plus un port HTTPS pour les autres appareils ;
le HTTP reste limité à cette machine (127.0.0.1 : tunnel, agent de pont, outils locaux inchangés).

Certificat : une autorité de certification locale propre à ce PC (créée une fois, dans data/tls) signe le
certificat du serveur, qui couvre les adresses réseau du moment, le nom de la machine et localhost ; il est
refait si ces adresses changent ou s'il approche de l'expiration. Pour qu'un appareil fasse confiance à la
gateway sans avertissement, on y installe une fois le certificat de l'autorité (/ca.crt) après avoir comparé
son empreinte avec celle affichée sur le PC. La clé privée de l'autorité ne quitte jamais data/tls.

    python -m gateway.tls            crée ou met à jour les certificats, affiche l'empreinte
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

log = logging.getLogger("gateway.tls")
CA_DAYS = 3650
SERVER_DAYS = 825  # plafond accepté par iOS / macOS pour un certificat de serveur
RENEW_BEFORE_DAYS = 30


def tls_port() -> int | None:
    raw = os.getenv("GATEWAY_TLS_PORT", "").strip()
    return int(raw) if raw.isdigit() and 0 < int(raw) < 65536 else None


def tls_dir() -> Path:
    return Path(os.getenv("GATEWAY_TLS_DIR", os.path.join("data", "tls")))


@dataclass
class TlsFiles:
    ca_cert: Path
    cert: Path
    key: Path
    fingerprint: str  # SHA-256 du certificat de l'autorité, à comparer sur l'appareil


def fingerprint(cert: x509.Certificate) -> str:
    return ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))


def ca_fingerprint(directory: Path | None = None) -> str | None:
    path = (directory or tls_dir()) / "ca.crt"
    try:
        return fingerprint(x509.load_pem_x509_certificate(path.read_bytes()))
    except (OSError, ValueError):
        return None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _write(path: Path, data: bytes, private: bool = False) -> None:
    path.write_bytes(data)
    if private:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _key_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def _ensure_ca(directory: Path) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    cert_path, key_path = directory / "ca.crt", directory / "ca.key"
    if cert_path.exists() and key_path.exists():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        if cert.not_valid_after_utc - _now() > dt.timedelta(days=RENEW_BEFORE_DAYS):
            return cert, key
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Smart AI Gateway"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"Smart AI Gateway - CA locale ({socket.gethostname()})"),
        ]
    )
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write(key_path, _key_pem(key), private=True)
    _write(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    log.warning(
        "Autorité de certification locale créée (empreinte %s) : installez <adresse>/ca.crt sur vos "
        "appareils (à refaire seulement si cette autorité est recréée).",
        fingerprint(cert),
    )
    return cert, key


def _names(ips: list[str]) -> tuple[list[str], list[str]]:
    host = socket.gethostname().lower()
    dns = sorted({"localhost", host, f"{host}.local"})
    addrs = sorted({"127.0.0.1", *ips}, key=lambda a: ipaddress.ip_address(a).packed)
    return dns, addrs


def ensure_certificates(ips: list[str], directory: Path | None = None) -> TlsFiles:
    """Crée l'autorité locale si besoin, puis un certificat de serveur couvrant `ips` (refait si elles changent)."""
    directory = directory or tls_dir()
    directory.mkdir(parents=True, exist_ok=True)
    ca, ca_key = _ensure_ca(directory)
    dns, addrs = _names(ips)
    cert_path, key_path = directory / "server.crt", directory / "server.key"
    if cert_path.exists() and key_path.exists():
        cur = x509.load_pem_x509_certificate(cert_path.read_bytes())
        try:
            san = cur.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            same = sorted(san.get_values_for_type(x509.DNSName)) == dns and sorted(
                str(i) for i in san.get_values_for_type(x509.IPAddress)
            ) == sorted(addrs)
        except x509.ExtensionNotFound:
            same = False
        fresh = cur.not_valid_after_utc - _now() > dt.timedelta(days=RENEW_BEFORE_DAYS)
        if same and fresh and cur.issuer == ca.subject:
            return TlsFiles(directory / "ca.crt", cert_path, key_path, fingerprint(ca))
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, socket.gethostname())]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=SERVER_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(
                [*(x509.DNSName(d) for d in dns), *(x509.IPAddress(ipaddress.ip_address(a)) for a in addrs)]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write(key_path, _key_pem(key), private=True)
    _write(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    log.info("Certificat HTTPS réseau local émis pour %s", ", ".join(addrs + dns))
    return TlsFiles(directory / "ca.crt", cert_path, key_path, fingerprint(ca))


if __name__ == "__main__":
    from .config import bind_address
    from .remote import lan_addresses

    files = ensure_certificates(lan_addresses(bind_address() if bind_address() != "127.0.0.1" else "0.0.0.0"))
    print(f"Autorité locale : {files.ca_cert.resolve()}")
    print(f"Empreinte SHA-256 : {files.fingerprint}")
