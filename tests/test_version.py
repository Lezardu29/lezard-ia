"""Numéro de version (15.4) et emplacement des données dans l'image Docker (15.2)."""

import re
from pathlib import Path

from gateway import __version__
from tests.test_remote import ACCOUNT_KEY, MASTER, make

ROOT = Path(__file__).resolve().parents[1]


def test_version_for_master_key_only():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    c, _ = make(require_key=False)
    with c:
        assert c.get("/v1/me", headers=MASTER).json()["version"] == __version__
        # un compte, une clé virtuelle ou un visiteur ne l'apprennent pas (repérage des failles connues)
        assert "version" not in c.get("/v1/me", headers={"Authorization": f"Bearer {ACCOUNT_KEY}"}).json()
        assert "version" not in c.get("/v1/me").json()
        health = c.get("/health")
        assert health.status_code == 200 and __version__ not in health.text


def test_docker_data_in_volume():
    """Base ET clé de chiffrement générée dans le volume : /app/data n'est pas accessible en écriture
    (démarrage refusé), et une clé recréée avec le conteneur rendrait illisibles les identifiants chiffrés."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    volume = re.search(r'^VOLUME \["([^"]+)"\]', text, re.M).group(1)
    for var in ("GATEWAY_DB", "GATEWAY_ENCRYPTION_KEY_FILE"):
        value = re.search(rf"\b{var}=(\S+)", text).group(1)
        assert value.startswith(volume.rstrip("/") + "/"), (var, value)
