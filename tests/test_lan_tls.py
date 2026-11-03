"""HTTPS sur le réseau local (12.7)."""

import os
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from cryptography import x509

from gateway.tls import ca_fingerprint, ensure_certificates

ROOT = Path(__file__).resolve().parents[1]
MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"


def san(path: Path):
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return sorted(str(i) for i in ext.get_values_for_type(x509.IPAddress)), ext.get_values_for_type(x509.DNSName)


def test_certificates_issued_by_local_ca(tmp_path):
    f = ensure_certificates(["192.168.1.20"], tmp_path)
    ca = x509.load_pem_x509_certificate(f.ca_cert.read_bytes())
    server = x509.load_pem_x509_certificate(f.cert.read_bytes())
    server.verify_directly_issued_by(ca)  # signé par l'autorité locale
    ips, dns = san(f.cert)
    assert ips == ["127.0.0.1", "192.168.1.20"] and "localhost" in dns and socket.gethostname().lower() in dns
    assert (server.not_valid_after_utc - server.not_valid_before_utc).days <= 825  # limite iOS / macOS
    assert f.fingerprint == ca_fingerprint(tmp_path) and len(f.fingerprint.split(":")) == 32
    # relance à l'identique : rien n'est refait (les appareils gardent leur confiance)
    before = (f.cert.read_bytes(), f.ca_cert.read_bytes())
    assert ensure_certificates(["192.168.1.20"], tmp_path).fingerprint == f.fingerprint
    assert (f.cert.read_bytes(), f.ca_cert.read_bytes()) == before
    # nouvelle adresse (box redémarrée) : nouveau certificat de serveur, MÊME autorité
    g = ensure_certificates(["192.168.1.42"], tmp_path)
    assert g.fingerprint == f.fingerprint and f.ca_cert.read_bytes() == before[1]
    assert san(g.cert)[0] == ["127.0.0.1", "192.168.1.42"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_served_over_https_on_lan_and_http_on_this_machine_only(tmp_path):
    """Vrai serveur : HTTPS sur l'adresse « réseau » (127.0.0.2), HTTP sur 127.0.0.1 seulement, un seul état."""
    http_port, tls_port = free_port(), free_port()
    env = {
        **os.environ,
        "GATEWAY_DB": str(tmp_path / "g.db"),
        "GATEWAY_MASTER_KEY": MASTER_KEY,
        "GATEWAY_ENCRYPTION_KEY": "q0Zc7oV2m1kq8bX4n9yL3pR5tW6uA2sD1fG7hJ8kL0M=",
        "GATEWAY_BIND": "127.0.0.2",
        "GATEWAY_TLS_PORT": str(tls_port),
        "GATEWAY_TLS_DIR": str(tmp_path / "tls"),
        "GATEWAY_PORT": str(http_port),
        "GATEWAY_ANONYMOUS_FROM": "",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "gateway.serve", "--port", str(http_port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        ca = tmp_path / "tls" / "ca.crt"
        lan, local = f"https://127.0.0.2:{tls_port}", f"http://127.0.0.1:{http_port}"
        trusted = None
        for _ in range(150):
            if proc.poll() is not None:
                pytest.fail(proc.stderr.read().decode(errors="replace")[-2000:])
            try:
                if ca.exists():
                    trusted = ssl.create_default_context(cafile=str(ca))
                    httpx.get(lan + "/health", verify=trusted, timeout=1)
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("la gateway HTTPS ne répond pas")
        master = {"Authorization": f"Bearer {MASTER_KEY}"}
        # un appareil qui a installé l'autorité locale : HTTPS vérifié
        # (un compte nommé : permis en version gratuite, contrairement aux clés API pour d'autres logiciels)
        k = httpx.post(lan + "/v1/admin/accounts", headers=master, json={"name": "tel"}, verify=trusted).json()
        # même état côté HTTP local (même processus, même base, même application)
        names = [x["name"] for x in httpx.get(local + "/v1/admin/accounts", headers=master).json()["data"]]
        assert "tel" in names and k["token"].startswith("sk-acct-")
        access = httpx.get(lan + "/v1/access", headers=master, verify=trusted).json()
        assert access["tls"]["enabled"] and access["tls"]["fingerprint"] == ca_fingerprint(tmp_path / "tls")
        assert {"kind": "lan", "url": lan, "label": "Réseau local (même Wi-Fi)", "secure": True} in access["urls"]
        assert httpx.get(local + "/ca.crt").content == ca.read_bytes()
        # un appareil sans l'autorité : refusé (pas de repli silencieux en clair)
        with pytest.raises(httpx.ConnectError):
            httpx.get(lan + "/health", timeout=3)
        # HTTP non joignable depuis le « réseau » : il n'écoute que sur 127.0.0.1
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):  # Windows : délai plutôt que refus
            httpx.get(f"http://127.0.0.2:{http_port}/health", timeout=2)
    finally:
        proc.terminate()
        proc.wait(10)
