import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.keys import VirtualKeyError, parse_expiry
from gateway.main import create_app
from gateway.permissions import (
    PermissionDenied,
    cap_effort,
    check_network,
    model_allowed,
    resolve_mode,
    resolve_orchestration,
    validate_permissions,
)
from gateway.plans import PlanResolver
from gateway.registry import CLOUD_MODELS, infer_local
from gateway.store import DocumentStore

GPT4O = next(m for m in CLOUD_MODELS if m.id == "gpt-4o")
MINI = next(m for m in CLOUD_MODELS if m.id == "gpt-4o-mini")
LLAMA = infer_local("ollama", "llama3.1:8b")


# ---------- unités ----------


def test_validate_permissions():
    p = validate_permissions(
        {
            "models": {"allow": ["Ollama", "openai/gpt-4o-mini"], "deny": ["openai/gpt-4o"]},
            "ip_allow": ["192.168.1.7/24", "10.0.0.1"],
            "origins": ["https://App.Example.com/x"],
            "modes": ["éco", "speed"],
            "orchestration": False,
            "max_effort": "moyen",
        }
    )
    assert p["models"] == {"allow": ["ollama/*", "openai/gpt-4o-mini"], "deny": ["openai/gpt-4o"]}
    assert p["ip_allow"] == ["192.168.1.0/24", "10.0.0.1/32"] and p["origins"] == ["app.example.com"]
    assert p["modes"] == ["economy", "speed"] and p["max_effort"] == "medium"
    for bad in (
        {"models": ["x"]},
        {"hosting": "mars"},
        {"ip_allow": ["999.1.1.1"]},
        {"modes": ["turbo"]},
        {"orchestration": "non"},
        {"max_effort": "infini"},
        {"inconnu": 1},
    ):
        with pytest.raises(PermissionDenied):
            validate_permissions(bad)


def test_model_rules():
    only_local = {"hosting": "local"}
    assert model_allowed(only_local, LLAMA) and not model_allowed(only_local, MINI)
    deny_4o = {"models": {"deny": ["openai/gpt-4o"]}}
    assert not model_allowed(deny_4o, GPT4O) and model_allowed(deny_4o, MINI)  # motif exact
    both = {"models": {"allow": ["openai/*"], "deny": ["openai/gpt-4o"]}}
    assert model_allowed(both, MINI) and not model_allowed(both, GPT4O) and not model_allowed(both, LLAMA)


def test_network_rules():
    perms = validate_permissions({"ip_allow": ["10.0.0.0/8"], "origins": ["*.example.com"]})
    check_network(perms, "10.2.3.4", "https://app.example.com")
    check_network(perms, "10.2.3.4", "https://example.com/page")  # domaine racine
    for ip, origin in [
        ("192.168.0.1", "https://app.example.com"),
        ("10.0.0.1", "https://evil.com"),
        ("10.0.0.1", None),
        ("pas-une-ip", "https://app.example.com"),
    ]:
        with pytest.raises(PermissionDenied):
            check_network(perms, ip, origin)


def test_mode_orchestration_effort_rules():
    assert resolve_mode({"modes": ["economy"]}, None) == "economy"  # mode imposé
    assert resolve_mode({"modes": ["economy", "speed"]}, "vitesse") == "speed"
    with pytest.raises(PermissionDenied):
        resolve_mode({"modes": ["economy"]}, "balanced")
    assert resolve_orchestration({"orchestration": False}, None) == "single"
    with pytest.raises(PermissionDenied):
        resolve_orchestration({"orchestration": False}, "multi")
    assert cap_effort({"max_effort": "medium"}, "high", explicit=False) == "medium"
    with pytest.raises(PermissionDenied):
        cap_effort({"max_effort": "low"}, "high", explicit=True)


def test_parse_expiry():
    now = 1_790_000_000.0  # ~2026
    assert parse_expiry(None, "30d", now) == now + 30 * 86400
    assert parse_expiry(None, 3600, now) == now + 3600
    assert parse_expiry("2099-01-01T00:00:00Z", None, now) == 4070908800.0
    assert parse_expiry(None, None, now) is None
    for args in [("2001-01-01", None), (None, "-5d"), (None, "bientôt"), ("hier", None)]:
        with pytest.raises(VirtualKeyError):
            parse_expiry(*args, now)


# ---------- API ----------

MASTER = {"Authorization": "Bearer master"}
HARD = "Explique pourquoi et compare deux architectures, analyse les trade-offs."


class Upstream:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path.endswith("/models"):
            ids = {"ollama": ["llama3.1:8b"], "openai": ["gpt-4o", "gpt-4o-mini"]}[host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        body = json.loads(request.content)
        self.calls.append((host, body))
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 2000, "completion_tokens": 10},
            },
        )


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("GATEWAY_TRUST_PROXY", "true")
    up = Upstream()
    app = create_app(
        [
            ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
            ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:

        def key(**data):
            r = c.post("/v1/keys", headers=MASTER, json={"name": "k", **data})
            assert r.status_code == 201, r.text
            return {"Authorization": f"Bearer {r.json()['key']}", "X-Forwarded-For": "10.0.0.5"}

        yield c, up, key


def chat(model="auto", text="Salut", **kw):
    return {"model": model, "messages": [{"role": "user", "content": text}], **kw}


def test_local_only_key_never_reaches_cloud(api, monkeypatch):
    c, up, key = api
    h = key(permissions={"hosting": "local"})
    assert c.post("/v1/chat/completions", headers=h, json=chat("openai/gpt-4o-mini")).status_code == 403
    for mode in ("speed", "balanced", "economy"):
        r = c.post("/v1/chat/completions", headers=h, json=chat(f"auto/{mode}", HARD)).json()
        assert r["x_gateway"]["provider"] == "ollama"
    assert {m["id"] for m in c.get("/v1/models", headers=h).json()["data"]} == {"ollama/llama3.1:8b"}
    assert (
        c.post(
            "/v1/chat/completions", headers=h, json=chat("ollama/llama3.1:8b", fallbacks=["openai/gpt-4o-mini"])
        ).status_code
        == 403
    )
    # même le résumeur imposé (cloud) est ignoré au profit d'un modèle local
    monkeypatch.setenv("SUMMARY_MODEL", "openai/gpt-4o-mini")
    hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": "bla " * 200} for i in range(16)]
    r = c.post(
        "/v1/chat/completions",
        headers=h,
        json={
            "model": "ollama/llama3.1:8b",
            "max_context_tokens": 1024,
            "messages": hist + [{"role": "user", "content": "?"}],
        },
    ).json()
    assert r["x_gateway"]["compression"]["summarizer"] == ["ollama/llama3.1:8b"]
    assert all(host == "ollama" for host, _ in up.calls)


def test_deny_specific_model(api):
    c, _, key = api
    h = key(permissions={"models": {"deny": ["openai/gpt-4o"]}})
    assert c.post("/v1/chat/completions", headers=h, json=chat("openai/gpt-4o")).status_code == 403
    assert c.post("/v1/chat/completions", headers=h, json=chat("openai/gpt-4o-mini")).status_code == 200
    prev = c.post("/v1/route", headers=h, json=chat("auto/balanced", HARD)).json()
    assert prev["routing"]["ref"] != "openai/gpt-4o" and "openai/gpt-4o" not in prev["routing"]["alternatives"]
    h2 = key(permissions={"models": {"allow": ["anthropic/*"]}})  # rien de disponible
    assert c.post("/v1/chat/completions", headers=h2, json=chat()).status_code == 403


def test_network_restrictions(api):
    c, _, key = api
    h = key(permissions={"ip_allow": ["10.0.0.0/24"], "origins": ["app.example.com"]})
    ok = {**h, "Origin": "https://app.example.com"}
    assert c.post("/v1/chat/completions", headers=ok, json=chat("ollama/llama3.1:8b")).status_code == 200
    assert (
        c.post(
            "/v1/chat/completions", headers={**ok, "X-Forwarded-For": "8.8.8.8"}, json=chat("ollama/llama3.1:8b")
        ).status_code
        == 403
    )
    assert (
        c.post(
            "/v1/chat/completions", headers={**h, "Origin": "https://evil.com"}, json=chat("ollama/llama3.1:8b")
        ).status_code
        == 403
    )
    assert c.get("/v1/me", headers=h).status_code == 403  # pas d'Origin


def test_expiration(api):
    c, _, key = api
    r = c.post("/v1/keys", headers=MASTER, json={"name": "temp", "expires_in": "1h"}).json()
    assert time.time() + 3500 < r["expires_at"] <= time.time() + 3600
    h = {"Authorization": f"Bearer {r['key']}"}
    assert c.post("/v1/chat/completions", headers=h, json=chat("ollama/llama3.1:8b")).status_code == 200
    assert c.post("/v1/keys", headers=MASTER, json={"name": "x", "expires_at": "2001-01-01"}).status_code == 400
    upd = c.patch(f"/v1/keys/{r['id']}", headers=MASTER, json={"expires_at": None}).json()
    assert upd["expires_at"] is None


def test_expired_key_is_rejected(monkeypatch):
    up = Upstream()
    store = DocumentStore(":memory:")
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free", master_key="master"),
        store=store,
    )
    with TestClient(app) as c:
        r = c.post("/v1/keys", headers=MASTER, json={"name": "court", "expires_in": 60}).json()
        h = {"Authorization": f"Bearer {r['key']}"}
        assert c.get("/v1/me", headers=h).status_code == 200
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + 120)
        resp = c.get("/v1/me", headers=h)
        assert resp.status_code == 401 and "expirée" in resp.json()["error"]["message"]


def test_routing_rights(api):
    c, up, key = api
    h = key(plan="pro", permissions={"modes": ["economy"], "orchestration": False, "max_effort": "medium"})
    r = c.post("/v1/chat/completions", headers=h, json=chat("auto", HARD)).json()
    assert r["x_gateway"]["routing"]["mode"] == "economy"  # mode imposé
    assert "orchestration" not in r["x_gateway"] and len(up.calls) == 1  # pas de multi-agents
    assert c.post("/v1/chat/completions", headers=h, json=chat("auto/speed")).status_code == 403
    assert c.post("/v1/chat/completions", headers=h, json=chat(orchestration="multi")).status_code == 403
    assert c.post("/v1/chat/completions", headers=h, json=chat(effort="high")).status_code == 403
    r = c.post(
        "/v1/chat/completions", headers=h, json=chat(text=HARD + " Démontre étape par étape.", effort="auto")
    ).json()
    assert r["x_gateway"]["effort"] in ("low", "medium")  # auto plafonné
    # sans restriction, la même clé Pro orchestre
    h_free = key(plan="pro")
    up.calls.clear()
    c.post("/v1/chat/completions", headers=h_free, json=chat("auto", HARD))
    # découpage tenté (10.5) ; ce faux fournisseur ne découpe pas -> un seul appel de réponse ensuite
    assert len(up.calls) == 2 and "répartis le travail" in up.calls[0][1]["messages"][0]["content"]


def test_permissions_crud(api):
    c, _, _ = api
    r = c.post("/v1/keys", headers=MASTER, json={"name": "x", "permissions": {"hosting": "mars"}})
    assert r.status_code == 400
    k = c.post("/v1/keys", headers=MASTER, json={"name": "x", "permissions": {"hosting": "local"}}).json()
    assert k["permissions"] == {"hosting": "local"}
    upd = c.patch(f"/v1/keys/{k['id']}", headers=MASTER, json={"permissions": {"modes": ["speed"]}}).json()
    assert upd["permissions"] == {"modes": ["speed"]}
    assert c.get(f"/v1/keys/{k['id']}", headers=MASTER).json()["permissions"] == {"modes": ["speed"]}
