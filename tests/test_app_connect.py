"""Applications branchées d'office sur Lezard IA après leur installation (Continue, Zed, Goose, LibreChat) : une clé
par application (rebrancher la remplace), fichiers existants gardés (copie « .avant-lezard »), réglages de Lezard IA
seulement ajoutés ou remplacés."""

import json

import pytest

from gateway import app_connect
from gateway.keys import hash_secret
from gateway.store import DocumentStore

from .test_marketplace import MASTER, client, install_log  # noqa: F401  (passerelle de test de la marketplace)

OWNER = "compte-maitre"


@pytest.fixture
def pc(tmp_path, monkeypatch):
    """Un PC de test : dossier personnel, APPDATA, variables de l'utilisateur Windows (jamais les vraies)."""
    home, appdata, env = tmp_path / "home", tmp_path / "appdata", {}
    home.mkdir()
    monkeypatch.setattr(app_connect.Path, "home", lambda: home)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(app_connect, "user_env", env.get)
    monkeypatch.setattr(app_connect, "set_user_env", env.__setitem__)
    store = DocumentStore(":memory:")
    return store, home, appdata, env


def active(store, name):
    return [k for k in store.list_keys(OWNER) if k["name"] == name and k["status"] == "active"]


def test_continue_gets_its_own_key_and_keeps_the_rest(pc):
    store, home, _, _ = pc
    cfg = home / ".continue" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("name: Local\nversion: 1.0.0\nschema: v1\nmodels: []\ncontext:\n  - provider: code\n", "utf-8")
    msg = app_connect.connect("continue", store, OWNER, "free", 8123)
    assert msg.startswith("Continue utilise Lezard IA")
    text = cfg.read_text("utf-8")
    key = [k for k in store.list_keys(OWNER) if k["name"] == "Application : Continue"]
    assert len(key) == 1 and "apiBase: http://127.0.0.1:8123/v1" in text and "  - provider: code" in text
    secret = text.split("apiKey: ")[1].split("\n")[0]
    assert store.key_by_hash(hash_secret(secret))[1]["name"] == "Application : Continue"
    assert (cfg.parent / "config.yaml.avant-lezard").read_text("utf-8").startswith("name: Local")  # original gardé
    app_connect.connect("continue", store, OWNER, "free", 8123)  # rebranchée : une seule entrée, nouvelle clé
    assert cfg.read_text("utf-8").count("Lezard IA (auto)") == 1 and len(active(store, "Application : Continue")) == 1


def test_zed_reads_its_key_from_the_shared_variable(pc):
    store, _, appdata, env = pc
    settings = appdata / "Zed" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('// Réglages de Zed\n{\n  "theme": "One Dark", // thème\n  "url": "http://a//b",\n}\n', "utf-8")
    assert app_connect.connect("zed", store, OWNER, "free", 8123).startswith("Zed utilise Lezard IA")
    data = json.loads(settings.read_text("utf-8"))
    assert data["theme"] == "One Dark" and data["url"] == "http://a//b"  # commentaires ôtés, valeurs gardées
    provider = data["language_models"]["openai_compatible"]["Lezard"]  # → variable LEZARD_API_KEY (doc de Zed)
    assert provider["api_url"] == "http://127.0.0.1:8123/v1" and provider["available_models"][0]["name"] == "auto"
    assert "sk-app" not in settings.read_text("utf-8")  # jamais de clé dans settings.json
    secret = env["LEZARD_API_KEY"]
    assert store.key_by_hash(hash_secret(secret))[1]["name"] == app_connect.SHARED_KEY
    app_connect.connect("goose", store, OWNER, "free", 8123)  # Goose ensuite : la même clé, encore active
    assert env["LEZARD_API_KEY"] == secret and len(active(store, app_connect.SHARED_KEY)) == 1


def test_goose_gets_a_provider_and_is_switched_to_it(pc):
    store, _, appdata, env = pc
    root = appdata / "Block" / "goose" / "config"
    root.mkdir(parents=True)
    (root / "config.yaml").write_text("GOOSE_PROVIDER: openai\nGOOSE_MODEL: gpt-4o\nextensions: {}\n", "utf-8")
    app_connect.connect("goose", store, OWNER, "free", 8123)
    provider = json.loads((root / "custom_providers" / "lezard.json").read_text("utf-8"))
    assert provider["engine"] == "openai" and provider["api_key_env"] == "LEZARD_API_KEY"
    assert provider["base_url"] == "http://127.0.0.1:8123/v1/chat/completions"  # chemin complet (moteur openai)
    assert (root / "config.yaml").read_text("utf-8") == "GOOSE_PROVIDER: lezard\nGOOSE_MODEL: auto\nextensions: {}\n"
    assert env["LEZARD_API_KEY"]


def test_librechat_gets_a_custom_endpoint(pc, monkeypatch):
    store, home, _, _ = pc
    monkeypatch.setattr(app_connect.shutil, "which", lambda name: None)  # pas de Docker ici
    assert "introuvable" in app_connect.connect("librechat", store, OWNER, "free", 8123)
    root = home / "LibreChat"
    root.mkdir()
    (root / "docker-compose.yml").write_text("services: {}\n", "utf-8")
    (root / ".env").write_text("JWT_SECRET=abc\n", "utf-8")
    (root / "docker-compose.override.yml").write_text("services:\n  api:\n    image: x\n", "utf-8")
    assert app_connect.connect("librechat", store, OWNER, "free", 8123).startswith("LibreChat utilise Lezard IA")
    yaml = (root / "librechat.yaml").read_text("utf-8")
    assert 'baseURL: "http://host.docker.internal:8123/v1"' in yaml and "${LEZARD_API_KEY}" in yaml
    env = (root / ".env").read_text("utf-8")
    assert env.startswith("JWT_SECRET=abc\nLEZARD_API_KEY=sk-app-")
    override = (root / "docker-compose.override.yml").read_text("utf-8")
    assert "target: /app/librechat.yaml" in override and "host.docker.internal:host-gateway" in override
    (root / "librechat.yaml").write_text("version: 1.3.17\nendpoints: {}\n", "utf-8")  # personnalisé à la main
    assert "personnalisé" in app_connect.connect("librechat", store, OWNER, "free", 8123)


def test_other_apps_keep_their_guide(pc):
    store, *_ = pc
    for app in ("jan", "chatbox", "cursor", "cline", "anythingllm"):
        assert app_connect.connect(app, store, OWNER, "free", 8123) is None


def test_connect_route_and_install_hook(client, pc):  # noqa: F811
    c = client
    assert c.post("/v1/admin/marketplace/apps/jan/connect", headers=MASTER).status_code == 404
    r = c.post("/v1/admin/marketplace/apps/goose/connect", headers=MASTER)
    assert r.status_code == 200 and r.json()["connected"] and "Goose utilise Lezard IA" in r.json()["message"]
    cat = c.get("/v1/admin/marketplace", headers=MASTER).json()
    assert next(a for a in cat["apps"] if a["id"] == "goose")["connect"] is True
    assert next(a for a in cat["apps"] if a["id"] == "jan")["connect"] is False


def test_install_connects_the_app_at_the_end(client, pc, monkeypatch, install_log):  # noqa: F811
    """Installation réussie d'une application concernée : branchée aussitôt, le message de fin le dit."""
    from .test_marketplace import fake_popen, market_api, wait_job

    fake_popen(monkeypatch, [])
    monkeypatch.setattr(market_api.chat_tools, "available", lambda store: [{"id": "goose"}])
    assert client.post("/v1/admin/marketplace/apps/goose/install", headers=MASTER).status_code == 202
    job = wait_job(client)
    assert job["state"] == "done" and not job["restart"]
    assert job["after"].startswith("Dans le chat de Lezard IA : /goose. Goose utilise Lezard IA")
    assert "Branchez" not in job["after"]  # plus de branchement à la main
