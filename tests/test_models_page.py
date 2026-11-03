"""Page Modèles : fournisseurs ajoutés depuis l'interface, clés chiffrées, téléchargement Ollama."""

import json

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig, load_providers
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER, ACCOUNT = {"Authorization": "Bearer master"}, {"Authorization": "Bearer compte"}


class Upstream:
    def __init__(self):
        self.ollama_models = ["llama3.1:8b"]
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.calls.append((request.method, host, path, request.headers.get("authorization")))
        if host == "ollama":
            if path == "/api/pull":
                model = json.loads(request.content)["model"]
                self.ollama_models.append(model)
                lines = [
                    {"status": "pulling manifest"},
                    {"status": "downloading", "total": 100, "completed": 40},
                    {"status": "downloading", "total": 100, "completed": 100},
                    {"status": "success"},
                ]
                return httpx.Response(200, content="\n".join(json.dumps(l) for l in lines).encode())
            if path == "/api/delete":
                model = json.loads(request.content)["model"]
                if model not in self.ollama_models:
                    return httpx.Response(404, json={"error": "not found"})
                self.ollama_models.remove(model)
                return httpx.Response(200)
            if path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": m} for m in self.ollama_models]})
        if host == "api.deepseek.com" and path == "/v1/models":
            if request.headers.get("authorization") != "Bearer sk-deepseek-123":
                return httpx.Response(401, json={"error": "bad key"})
            return httpx.Response(200, json={"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]})
        if path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                },
            )
        return httpx.Response(404)


def make(store, up):
    return create_app(
        [
            ProviderConfig("ollama", "openai", "http://ollama:11434/v1", "local"),
            ProviderConfig("openai", "openai", "http://openai/v1", "cloud", "OPENAI_API_KEY", enabled=False),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({"compte": "free"}, "free", master_key="master"),
        store=store,
    )


def test_add_custom_provider_persists_with_encrypted_key():
    store, up = DocumentStore(":memory:"), Upstream()
    with TestClient(make(store, up)) as c:
        assert c.get("/v1/admin/providers", headers=ACCOUNT).status_code == 403
        listed = {p["name"]: p for p in c.get("/v1/admin/providers", headers=MASTER).json()["data"]}
        assert listed["ollama"]["online"] and listed["ollama"]["ollama"] and listed["ollama"]["hosting"] == "local"
        assert listed["openai"]["enabled"] is False and listed["openai"]["key"]["source"] is None
        r = c.post(
            "/v1/admin/providers",
            headers=MASTER,
            json={
                "name": "deepseek",
                "base_url": "https://api.deepseek.com/v1/",
                "kind": "openai",
                "hosting": "cloud",
                "api_key": "sk-deepseek-123",
            },
        )
        assert (
            r.status_code == 201 and r.json()["online"] and r.json()["models"] == ["deepseek-chat", "deepseek-reasoner"]
        )
        # routable aussitôt
        out = c.post(
            "/v1/chat/completions",
            headers=MASTER,
            json={"model": "deepseek/deepseek-chat", "messages": [{"role": "user", "content": "Bonjour"}]},
        )
        assert out.status_code == 200 and out.json()["x_gateway"]["provider"] == "deepseek"
        p = {p["name"]: p for p in c.get("/v1/admin/providers", headers=MASTER).json()["data"]}["deepseek"]
        assert p["custom"] and p["key"]["source"] == "dashboard" and p["key"]["hint"]
        assert "sk-deepseek-123" not in json.dumps(p)  # jamais réaffichée
        # erreurs
        dup = {"name": "deepseek", "base_url": "https://x.example/v1"}
        assert c.post("/v1/admin/providers", headers=MASTER, json=dup).status_code == 409
        assert (
            c.post(
                "/v1/admin/providers", headers=MASTER, json={"name": "ollama", "base_url": "http://x/v1"}
            ).status_code
            == 409
        )
        assert (
            c.post("/v1/admin/providers", headers=MASTER, json={"name": "bad", "base_url": "ftp://x"}).status_code
            == 400
        )
        assert (
            c.post(
                "/v1/admin/providers", headers=MASTER, json={"name": "x1", "base_url": "http://x", "kind": "?"}
            ).status_code
            == 400
        )
        assert c.post("/v1/admin/providers", headers=ACCOUNT, json=dup).status_code == 403
    # la clé est chiffrée en base (jamais en clair) et rechargée au redémarrage
    raw = store._db.execute("SELECT secret FROM provider_credentials WHERE provider='deepseek'").fetchone()[0]
    assert "sk-deepseek-123" not in raw
    with TestClient(make(store, up)) as c:
        p = {p["name"]: p for p in c.get("/v1/admin/providers", headers=MASTER).json()["data"]}["deepseek"]
        assert p["online"] and p["models"]
        assert c.delete("/v1/admin/providers/openai", headers=MASTER).status_code == 409  # fourni par la config
        assert c.delete("/v1/admin/providers/deepseek", headers=MASTER).json()["deleted"] is True
        assert "deepseek" not in {p["name"] for p in c.get("/v1/admin/providers", headers=MASTER).json()["data"]}
    assert not store.list_custom_providers() and not store.get_credential("deepseek")


def test_nothing_declared_or_probed_at_startup(monkeypatch, tmp_path):
    """Aucune IA n'est recherchée au démarrage : l'administrateur les ajoute lui-même."""
    monkeypatch.delenv("GATEWAY_AUTO_DETECT", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-123456")
    assert load_providers(str(tmp_path / "absent.json")) == []
    up = Upstream()
    app = create_app(
        None,
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        assert c.get("/v1/admin/providers", headers=MASTER).json()["data"] == []
        assert up.calls == []  # aucune sonde
        presets = {p["name"]: p for p in c.get("/v1/admin/providers/presets", headers=MASTER).json()["data"]}
        assert (
            presets["openai"]["env_key"] and not presets["openai"]["added"] and presets["ollama"]["hosting"] == "local"
        )
        assert c.get("/v1/admin/providers/presets", headers=ACCOUNT).status_code == 403


def test_add_with_key_name_and_restrictions(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    store, up = DocumentStore(":memory:"), Upstream()
    with TestClient(make(store, up)) as c:
        base = {"name": "deepseek", "base_url": "https://api.deepseek.com/v1", "hosting": "cloud"}
        assert c.post("/v1/admin/providers", headers=MASTER, json=base).status_code == 400  # clé requise
        bad = {**base, "api_key": "sk-deepseek-123", "rules": {"rpd": 0}}
        assert c.post("/v1/admin/providers", headers=MASTER, json=bad).status_code == 400  # rien créé
        assert not store.list_custom_providers() and not store.get_credential("deepseek")
        r = c.post(
            "/v1/admin/providers",
            headers=MASTER,
            json={
                **base,
                "api_key": "sk-deepseek-123",
                "rules": {"rpd": 50, "tpd": 100000, "models": {"allow": ["deepseek-chat"]}},
            },
        )
        assert r.status_code == 201 and r.json()["rules"]["rpd"] == 50
        p = {p["name"]: p for p in c.get("/v1/admin/providers", headers=MASTER).json()["data"]}["deepseek"]
        assert p["rules"] == {"models": {"allow": ["deepseek-chat"]}, "rpd": 50, "tpd": 100000}
        denied = c.post(
            "/v1/chat/completions",
            headers=MASTER,
            json={"model": "deepseek/deepseek-reasoner", "messages": [{"role": "user", "content": "x"}]},
        )
        assert denied.status_code == 403
        # un serveur local se passe de clé
        local = {"name": "lmstudio", "base_url": "http://lmstudio:1234/v1", "hosting": "local"}
        assert c.post("/v1/admin/providers", headers=MASTER, json=local).status_code == 201


def test_gateway_key_refused_and_provider_error_shown():
    """La clé maître remplie par le navigateur dans le champ « Clé API » ne part pas chez le fournisseur, et une clé
    refusée affiche la raison donnée par le fournisseur."""
    store, up = DocumentStore(":memory:"), Upstream()
    master, account = "gw-master-0123456789abcdef", "ct-compte-0123456789abcdef"
    app = create_app(
        [ProviderConfig("openai", "openai", "http://openai/v1", "cloud", enabled=False)],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({account: "free"}, "free", master_key=master),
        store=store,
    )
    MASTER = {"Authorization": f"Bearer {master}"}
    with TestClient(app) as c:
        base = {"name": "deepseek", "base_url": "https://api.deepseek.com/v1", "hosting": "cloud"}
        for key in (master, account, "sk-app-0123456789abcdef"):
            r = c.post("/v1/admin/providers", headers=MASTER, json={**base, "api_key": key})
            assert r.status_code == 400 and "passerelle" in r.text, key
        assert not store.list_custom_providers()
        assert c.put("/v1/admin/credentials/openai", headers=MASTER, json={"api_key": master}).status_code == 400
        r = c.post("/v1/admin/providers", headers=MASTER, json={**base, "api_key": "sk-mauvaise-cle"})
        assert r.status_code == 201 and not r.json()["online"]
        assert r.json()["error"] == "HTTP 401 : bad key"
        p = {p["name"]: p for p in c.get("/v1/admin/providers", headers=MASTER).json()["data"]}["deepseek"]
        assert p["error"] == "HTTP 401 : bad key"
        ok = c.put("/v1/admin/credentials/deepseek", headers=MASTER, json={"api_key": "sk-deepseek-123"}).json()
        assert ok["online"] and ok["error"] is None


def test_preset_reuses_env_key_after_restart(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-env-123456")
    store, up = DocumentStore(":memory:"), Upstream()
    with TestClient(make(store, up)) as c:
        r = c.post(
            "/v1/admin/providers",
            headers=MASTER,
            json={"name": "groq", "base_url": "http://groq/openai/v1", "hosting": "cloud"},
        )
        assert r.status_code == 201
    with TestClient(make(store, up)) as c:
        p = {p["name"]: p for p in c.get("/v1/admin/providers", headers=MASTER).json()["data"]}["groq"]
        assert p["key"]["source"] == "env" and p["enabled"]


def test_ollama_pull_and_delete():
    up = Upstream()
    with TestClient(make(DocumentStore(":memory:"), up)) as c:
        with c.stream("POST", "/v1/admin/ollama/pull", headers=MASTER, json={"model": "qwen2.5:7b"}) as r:
            events = [json.loads(l) for l in r.iter_lines() if l.strip()]
        assert events[1] == {"status": "downloading", "total": 100, "completed": 40}
        assert events[-1] == {"status": "done"}
        assert ("POST", "ollama", "/api/pull", None) in up.calls  # racine Ollama, sans /v1
        models = {p["name"]: p for p in c.get("/v1/admin/providers", headers=MASTER).json()["data"]}["ollama"]["models"]
        assert "qwen2.5:7b" in models
        assert c.post("/v1/admin/ollama/pull", headers=MASTER, json={"model": "x; rm -rf"}).status_code == 400
        assert (
            c.post("/v1/admin/ollama/pull", headers=MASTER, json={"model": "a", "provider": "openai"}).status_code
            == 400
        )
        assert c.post("/v1/admin/ollama/pull", headers=ACCOUNT, json={"model": "a"}).status_code == 403
        assert c.delete("/v1/admin/ollama/models/qwen2.5:7b", headers=MASTER).json()["deleted"] is True
        assert c.delete("/v1/admin/ollama/models/absent:1b", headers=MASTER).status_code == 404


def test_models_page_is_served():
    with TestClient(make(DocumentStore(":memory:"), Upstream())) as c:
        r = c.get("/models?frame=1")
        assert (
            r.status_code == 200 and "Modèles" in r.text and "script-src 'self'" in r.headers["content-security-policy"]
        )
