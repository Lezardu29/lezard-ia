"""Nextcloud saisi dans la page Modèles (adresse, utilisateur, mot de passe d'application) : chiffré, outils enregistrés
aussitôt, rechargé au démarrage, retiré d'un clic."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import sources
from gateway.config import ProviderConfig
from gateway.crypto import Cipher
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore
from gateway.tools import TOOLS

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
APP_PASSWORD = "abcd-efgh-ijkl-mnop-qrst"


@pytest.fixture(autouse=True)
def clean_sources(monkeypatch):
    for name in ("NEXTCLOUD_URL", "NEXTCLOUD_USER", "NEXTCLOUD_APP_PASSWORD", "PAPERLESS_URL", "PAPERLESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    yield
    sources.SAVED.clear()
    sources.register_sources()


def make():
    cipher = Cipher(b"\x09" * 32)
    store = DocumentStore(":memory:")
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=store,
        cipher=cipher,
    )
    return TestClient(app), store, cipher


def save(c, **body):
    payload = {"url": "https://cloud.test/", "user": "bob", "password": APP_PASSWORD, **body}
    return c.put("/v1/admin/nextcloud", headers=MASTER, json=payload)


def test_settings_are_saved_encrypted_and_the_tools_appear():
    c, store, cipher = make()
    with c:
        r = save(c)
        assert r.status_code == 200, r.text
        assert r.json()["tools"] == ["nextcloud_list", "nextcloud_search", "nextcloud_read"]
        assert "nextcloud_read" in TOOLS and sources.nextcloud_url() == "https://cloud.test"
        raw = store.get_credential("svc:nextcloud")
        assert APP_PASSWORD not in raw and cipher.decrypt(raw, "svc:nextcloud") == APP_PASSWORD
        view = c.get("/v1/admin/nextcloud", headers=MASTER).json()["data"]
        assert view == {"url": "https://cloud.test", "user": "bob", "password_set": True, "active": True}
        assert APP_PASSWORD not in json.dumps(view) and APP_PASSWORD not in json.dumps(r.json())


def test_the_password_is_hidden_from_the_credentials_list():
    c, _, _ = make()
    with c:
        save(c)
        listing = c.get("/v1/admin/credentials", headers=MASTER).json()
        assert all(not entry["provider"].startswith("svc:") for entry in listing["data"])


def test_an_empty_password_keeps_the_saved_one():
    c, store, cipher = make()
    with c:
        save(c)
        r = save(c, password="", url="https://autre.test", user="alice")
        assert r.status_code == 200, r.text
        assert cipher.decrypt(store.get_credential("svc:nextcloud"), "svc:nextcloud") == APP_PASSWORD
        assert sources.nextcloud_url() == "https://autre.test" and sources._env("NEXTCLOUD_USER") == "alice"


def test_invalid_entries_are_refused():
    c, store, _ = make()
    with c:
        assert save(c, url="cloud.test").status_code == 400  # sans https:// ni http://
        assert save(c, user="   ").status_code == 400
        assert save(c, password="").status_code == 400  # aucun mot de passe enregistré encore
        assert store.get_setting("nextcloud") is None
        assert "nextcloud_list" not in TOOLS


def test_restart_reloads_the_saved_settings():
    c, store, cipher = make()
    with c:
        save(c)
    sources.SAVED.clear()
    sources.load_nextcloud(store, cipher)
    assert sources.nextcloud_url() == "https://cloud.test"
    assert sources._env("NEXTCLOUD_APP_PASSWORD") == APP_PASSWORD


def test_delete_removes_the_tools():
    c, store, cipher = make()
    with c:
        save(c)
        r = c.delete("/v1/admin/nextcloud", headers=MASTER)
        assert r.status_code == 200 and r.json()["data"]["active"] is False
        assert store.get_credential("svc:nextcloud") is None
    assert "nextcloud_list" not in TOOLS and "nextcloud_read" not in TOOLS


def test_saved_settings_win_over_the_env_file(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_URL", "https://ancien.test")
    monkeypatch.setenv("NEXTCLOUD_USER", "ancien")
    monkeypatch.setenv("NEXTCLOUD_APP_PASSWORD", "ancien-mdp")
    c, _, _ = make()
    with c:
        save(c)
    assert sources.nextcloud_url() == "https://cloud.test" and sources._env("NEXTCLOUD_USER") == "bob"
