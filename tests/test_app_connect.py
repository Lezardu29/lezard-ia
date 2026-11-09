"""Applications branchées d'office sur Lezard IA après leur installation (Continue, Zed, Goose, LibreChat, Cline, Kilo
Code, Jan, Cherry Studio, AnythingLLM, Chatbox, Claude Code Router) : une clé
par application (rebrancher la remplace), fichiers existants gardés (copie « .avant-lezard »), réglages de Lezard IA
seulement ajoutés ou remplacés."""

import json
from pathlib import Path

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


def test_librechat_gets_a_custom_endpoint(pc):
    store, home, _, _ = pc
    assert "introuvable" in app_connect.connect("librechat", store, OWNER, "free", 8123)
    root = home / "LibreChat"
    root.mkdir()
    (root / "package.json").write_text("{}", "utf-8")
    (root / ".env").write_text("JWT_SECRET=abc\n", "utf-8")
    assert app_connect.connect("librechat", store, OWNER, "free", 8123).startswith("LibreChat utilise Lezard IA")
    yaml = (root / "librechat.yaml").read_text("utf-8")
    assert 'baseURL: "http://127.0.0.1:8123/v1"' in yaml and "${LEZARD_API_KEY}" in yaml  # sans Docker : ce PC
    assert 'default: ["auto", "auto/speed", "auto/economy"]' in yaml and "fetch: false" in yaml
    env = (root / ".env").read_text("utf-8")
    assert env.startswith("JWT_SECRET=abc\nLEZARD_API_KEY=sk-app-")
    (root / "librechat.yaml").write_text("version: 1.3.17\nendpoints: {}\n", "utf-8")  # personnalisé à la main
    assert "personnalisé" in app_connect.connect("librechat", store, OWNER, "free", 8123)


def test_cline_and_kilo_code_files(pc):
    store, home, _, _ = pc
    data = home / ".cline" / "data"
    data.mkdir(parents=True)
    (data / "globalState.json").write_text('{"telemetrySetting": "disabled"}', "utf-8")
    assert app_connect.connect("cline", store, OWNER, "free", 8123).startswith("Cline utilise Lezard IA")
    state = json.loads((data / "globalState.json").read_text("utf-8"))
    assert state["telemetrySetting"] == "disabled" and state["openAiBaseUrl"] == "http://127.0.0.1:8123/v1"
    assert state["actModeApiProvider"] == state["planModeApiProvider"] == "openai"
    assert state["actModeOpenAiModelId"] == "auto"
    secret = json.loads((data / "secrets.json").read_text("utf-8"))["openAiApiKey"]
    assert store.key_by_hash(hash_secret(secret))[1]["name"] == "Application : Cline"
    kilo = home / ".config" / "kilo" / "kilo.jsonc"
    kilo.parent.mkdir(parents=True)
    kilo.write_text(
        '{\n  // réglages\n  "$schema": "https://app.kilo.ai/config.json",\n  "share": "disabled",\n}\n', "utf-8"
    )
    assert app_connect.connect("kilocode", store, OWNER, "free", 8123).startswith("Kilo Code utilise Lezard IA")
    config = json.loads(kilo.read_text("utf-8"))
    assert config["share"] == "disabled" and config["model"] == "lezard/auto"
    assert config["provider"]["lezard"]["options"]["baseURL"] == "http://127.0.0.1:8123/v1"


@pytest.fixture
def closed(monkeypatch):
    """Applications de bureau fermées (rien ne tourne), jamais lancées pour de vrai ; trousseau factice."""
    stored = []
    monkeypatch.setattr(app_connect, "_running", lambda image: False)
    monkeypatch.setattr(app_connect, "_shortcut", lambda name: Path(f"{name}.lnk"))
    monkeypatch.setattr(app_connect, "_program_of", lambda lnk: lnk.stem + ".exe")
    monkeypatch.setattr(app_connect.subprocess, "Popen", lambda *a, **k: pytest.fail("application lancée"))
    monkeypatch.setattr(app_connect, "_store_credential", lambda *a: stored.append(a))
    return stored


def test_desktop_app_open_or_never_started(pc, closed, monkeypatch):
    store, *_ = pc
    monkeypatch.setattr(app_connect, "_shortcut", lambda name: None)  # pas installée
    assert "introuvable dans le menu Démarrer" in app_connect.connect("jan", store, OWNER, "free", 8123)
    monkeypatch.setattr(app_connect, "_shortcut", lambda name: Path(f"{name}.lnk"))
    monkeypatch.setattr(app_connect, "_running", lambda image: True)  # ouverte par l'utilisateur : rien d'écrit
    assert "est ouvert : fermez-le" in app_connect.connect("chatbox", store, OWNER, "free", 8123)
    assert not store.list_keys(OWNER)


def test_never_started_app_is_opened_once_then_closed(pc, closed, monkeypatch):
    """Juste installée, jamais ouverte : lancée par son raccourci le temps de créer ses réglages, puis refermée."""
    store, _, appdata, _ = pc
    cfg = appdata / "xyz.chatboxapp.ce" / "config.json"
    events = []

    def launch(argv, **kw):  # « double-clic » : l'application crée ses réglages
        events.append(("open", argv[1]))
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"settings": {"language": "fr"}}), "utf-8")

    monkeypatch.setattr(app_connect.subprocess, "Popen", launch)
    monkeypatch.setattr(app_connect, "_close", lambda image: events.append(("close", image)))
    monkeypatch.setattr(app_connect.time, "sleep", lambda s: None)
    assert app_connect.connect("chatbox", store, OWNER, "free", 8123).startswith("Chatbox utilise")
    assert events == [("open", "Chatbox CE.lnk"), ("close", "Chatbox CE.exe")]
    assert json.loads(cfg.read_text("utf-8"))["settings"]["openaiCustomModel"] == "auto"


def test_jan_provider_and_keyring(pc, closed):
    store, _, appdata, _ = pc
    path = appdata / "Jan" / "data" / "settings.json"
    path.parent.mkdir(parents=True)
    other = {"provider": "openai", "active": True, "models": []}
    state = {"state": {"providers": [other], "selectedProvider": "llamacpp", "selectedModel": None}, "version": 19}
    path.write_text(json.dumps({"theme": "dark", "model-provider": json.dumps(state)}), "utf-8")
    assert app_connect.connect("jan", store, OWNER, "free", 8123).startswith("Jan utilise Lezard IA")
    saved = json.loads(path.read_text("utf-8"))
    st = json.loads(saved["model-provider"])["state"]
    assert saved["theme"] == "dark" and [p["provider"] for p in st["providers"]] == ["openai", "Lezard IA"]
    lezard = st["providers"][1]
    assert lezard["base_url"] == "http://127.0.0.1:8123/v1" and "api_key" not in lezard  # clé : trousseau seulement
    assert st["selectedProvider"] == "Lezard IA" and st["selectedModel"]["id"] == "auto"
    target, user, blob = closed[0]
    assert (target, user) == ("Lezard IA.jan-providers", "Lezard IA")
    assert store.key_by_hash(hash_secret(json.loads(blob)[0]))[1]["name"] == "Application : Jan"
    assert json.loads((path.parent / "provider_secrets.index.json").read_text("utf-8")) == ["Lezard IA"]


CHERRY_SCHEMA = """
create table user_provider (provider_id text primary key, preset_provider_id text, name text not null,
  logo_key text, endpoint_configs text, default_chat_endpoint text, api_keys text default '[]', auth_config text,
  provider_settings text, is_enabled integer not null default 0, order_key text not null,
  created_at integer not null, updated_at integer not null);
create table user_model (id text primary key, provider_id text not null, model_id text not null, name text,
  capabilities text, endpoint_types text, context_window integer, supports_streaming integer, is_enabled integer,
  is_hidden integer, is_deprecated integer, order_key text not null, created_at integer not null,
  updated_at integer not null);
create table preference (scope text not null default 'default', key text not null, value text,
  created_at integer not null, updated_at integer not null, primary key (scope, key));
insert into user_provider (provider_id, name, order_key, created_at, updated_at) values ('openai', 'OpenAI', 'b06', 0, 0);
insert into preference values ('default', 'chat.default_model_id', '"cherryai::qwen"', 0, 0);
"""


def test_cherry_studio_database(pc, closed):
    import sqlite3

    store, _, appdata, _ = pc
    path = appdata / "CherryStudio" / "Data" / "cherrystudio.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript(CHERRY_SCHEMA)
    for _ in range(2):  # rebranché : une seule entrée
        assert app_connect.connect("cherrystudio", store, OWNER, "free", 8123).startswith("Cherry Studio utilise")
    with sqlite3.connect(path) as db:
        rows = db.execute("select provider_id, endpoint_configs, api_keys, is_enabled, order_key from user_provider")
        providers = {r[0]: r for r in rows}
        models = db.execute("select id, model_id from user_model").fetchall()
        default = db.execute("select value from preference where key = 'chat.default_model_id'").fetchone()[0]
    lezard = providers["lezard-ia"]
    assert json.loads(lezard[1]) == {"openai-chat-completions": {"baseUrl": "http://127.0.0.1:8123/v1"}}
    assert lezard[3] == 1 and lezard[4] > "b06"
    key = json.loads(lezard[2])[0]["key"]
    assert store.key_by_hash(hash_secret(key))[1]["name"] == "Application : Cherry Studio"
    assert models == [("lezard-ia::auto", "auto")] and json.loads(default) == "lezard-ia::auto"


def test_anythingllm_and_chatbox_settings(pc, closed):
    store, _, appdata, _ = pc
    env = appdata / "anythingllm-desktop" / "storage" / ".env"
    env.parent.mkdir(parents=True)
    env.write_text("# Auto-dump\nSERVER_PORT='3001'\nLLM_PROVIDER='openai'\n", "utf-8")
    assert app_connect.connect("anythingllm", store, OWNER, "free", 8123).startswith("AnythingLLM utilise")
    text = env.read_text("utf-8")
    assert "SERVER_PORT='3001'" in text and "LLM_PROVIDER='generic-openai'" in text
    assert (
        "GENERIC_OPEN_AI_BASE_PATH='http://127.0.0.1:8123/v1'" in text and "GENERIC_OPEN_AI_MODEL_PREF='auto'" in text
    )
    cfg = appdata / "xyz.chatboxapp.ce" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"settings": {"aiProvider": "ollama", "language": "fr"}}), "utf-8")
    assert app_connect.connect("chatbox", store, OWNER, "free", 8123).startswith("Chatbox utilise")
    s = json.loads(cfg.read_text("utf-8"))["settings"]
    assert s["language"] == "fr" and s["aiProvider"] == "openai" and s["apiHost"] == "http://127.0.0.1:8123"
    assert (s["model"], s["openaiCustomModel"]) == ("custom-model", "auto")


def test_claude_code_router_database(pc, monkeypatch):
    import sqlite3

    from gateway import installers

    store, _, appdata, _ = pc
    calls = []
    monkeypatch.setattr(app_connect.subprocess, "run", lambda argv, **k: calls.append(argv[1:]))
    monkeypatch.setattr(installers, "program", lambda name: "ccr" if name == "ccr" else None)
    path = appdata / "claude-code-router" / "config.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("create table app_config (key text primary key, value_json text not null, updated_at text not null)")
        db.execute("insert into app_config values ('default', ?, 'x')", (json.dumps({"PORT": 3456, "Providers": []}),))
    assert app_connect.connect("ccrouter", store, OWNER, "free", 8123).startswith("Claude Code Router utilise")
    assert calls == [["stop"]]  # service arrêté le temps d'écrire
    with sqlite3.connect(path) as db:
        config = json.loads(db.execute("select value_json from app_config").fetchone()[0])
    assert config["PORT"] == 3456 and config["preferredProvider"] == "lezard"
    (provider,) = config["Providers"]
    assert provider["api_base_url"] == "http://127.0.0.1:8123/v1/chat/completions" and provider["models"] == ["auto"]


def test_cursor_keeps_its_guide(pc):
    store, *_ = pc
    assert app_connect.connect("cursor", store, OWNER, "free", 8123) is None


def test_connect_route_and_install_hook(client, pc):  # noqa: F811
    c = client
    assert c.post("/v1/admin/marketplace/apps/cursor/connect", headers=MASTER).status_code == 404
    r = c.post("/v1/admin/marketplace/apps/goose/connect", headers=MASTER)
    assert r.status_code == 200 and r.json()["connected"] and "Goose utilise Lezard IA" in r.json()["message"]
    cat = c.get("/v1/admin/marketplace", headers=MASTER).json()
    assert next(a for a in cat["apps"] if a["id"] == "goose")["connect"] is True
    assert next(a for a in cat["apps"] if a["id"] == "cursor")["connect"] is False
    assert next(a for a in cat["apps"] if a["id"] == "jan")["connect"] is True


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
