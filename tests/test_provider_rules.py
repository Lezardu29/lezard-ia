"""Restrictions sur les IA en entrée : modèles, plans, débit, budget et tokens par fournisseur."""

import json

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.limits import RateLimited
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.provider_rules import ProviderGuard, validate_rules
from gateway.registry import ModelInfo
from gateway.store import DocumentStore

MASTER, ACCOUNT = {"Authorization": "Bearer master"}, {"Authorization": "Bearer compte"}
MODELS = {"ollama": ["llama3.1:8b", "qwen2.5:7b"], "lms": ["phi-4"]}


class Upstream:
    def __init__(self):
        self.sent = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in MODELS[host]]})
        if path.endswith("/chat/completions"):
            body = json.loads(request.content)
            self.sent.append((host, body))
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
            ProviderConfig("lms", "openai", "http://lms:1234/v1", "local"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({"compte": "free"}, "free", master_key="master"),
        store=store,
    )


def ask(c, model, headers=ACCOUNT, **extra):
    return c.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": model, "messages": [{"role": "user", "content": "Bonjour"}], **extra},
    )


def test_rules_are_validated_listed_and_persisted():
    store = DocumentStore(":memory:")
    with TestClient(make(store, Upstream())) as c:
        url = "/v1/admin/providers/ollama/rules"
        assert c.put(url, headers=ACCOUNT, json={"rpm": 5}).status_code == 403
        assert c.put("/v1/admin/providers/absent/rules", headers=MASTER, json={}).status_code == 404
        for bad in (
            {"rpm": 0},
            {"plans": ["gold"]},
            {"models": {"allow": "x"}},
            {"max_tokens": -1},
            {"oups": 1},
            {"budget": {"amount": 5, "period": "année"}},
        ):
            assert c.put(url, headers=MASTER, json=bad).status_code == 400, bad
        r = c.put(
            url,
            headers=MASTER,
            json={
                "models": {"allow": ["Llama*"]},
                "plans": ["pro", "free"],
                "rpm": 10,
                "budget": {"amount": 5, "currency": "EUR", "period": "month"},
            },
        )
        assert r.status_code == 200
        assert r.json()["rules"] == {
            "models": {"allow": ["llama*"]},
            "plans": ["free", "pro"],
            "rpm": 10,
            "budget": [{"amount": 5.0, "currency": "EUR", "period": "month"}],
        }
    with TestClient(make(store, Upstream())) as c:  # rechargées au redémarrage
        listed = {p["name"]: p for p in c.get("/v1/admin/providers", headers=MASTER).json()["data"]}
        assert listed["ollama"]["rules"]["rpm"] == 10 and listed["lms"]["rules"] == {}
        assert listed["ollama"]["usage"]["rpm"] == {"limit": 10, "used": 0}
        assert c.put("/v1/admin/providers/ollama/rules", headers=MASTER, json={}).json()["rules"] == {}
    assert store.list_provider_rules() == {}


def test_model_and_plan_restrictions():
    up = Upstream()
    with TestClient(make(DocumentStore(":memory:"), up)) as c:
        c.put("/v1/admin/providers/ollama/rules", headers=MASTER, json={"models": {"deny": ["*8b"]}})
        r = ask(c, "ollama/llama3.1:8b")
        assert r.status_code == 403 and "non autorisé" in r.json()["error"]["message"]
        assert ask(c, "ollama/qwen2.5:7b").status_code == 200
        c.put("/v1/admin/providers/lms/rules", headers=MASTER, json={"plans": ["pro"]})
        r = ask(c, "lms/phi-4")
        assert r.status_code == 403 and "réservé aux plans pro" in r.json()["error"]["message"]
        assert ask(c, "lms/phi-4", headers=MASTER).status_code == 200  # clé maître exemptée
        for _ in range(4):  # routage auto : ne choisit que ce qui est permis
            assert ask(c, "auto").json()["x_gateway"]["model"] == "qwen2.5:7b"


def test_rate_limit_moves_auto_routing_and_caps_tokens():
    up = Upstream()
    with TestClient(make(DocumentStore(":memory:"), up)) as c:
        c.put("/v1/admin/providers/ollama/rules", headers=MASTER, json={"rpm": 1, "max_tokens": 50})
        c.put("/v1/admin/providers/lms/rules", headers=MASTER, json={"rpm": 1})
        assert ask(c, "ollama/qwen2.5:7b", max_tokens=4000).status_code == 200
        assert up.sent[-1][1]["max_tokens"] == 50  # plafonné
        r = ask(c, "ollama/qwen2.5:7b")
        assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
        assert "ollama" in r.json()["error"]["message"]
        assert ask(c, "auto").json()["x_gateway"]["provider"] == "lms"  # bascule sur l'autre IA
        r = ask(c, "auto")  # tout est saturé
        assert r.status_code == 429
        usage = {p["name"]: p for p in c.get("/v1/admin/providers", headers=MASTER).json()["data"]}
        assert usage["ollama"]["usage"]["rpm"]["used"] == 1 and usage["ollama"]["usage"]["saturated"]


def test_budget_blocks_provider_until_reset():
    now = [1_760_000_000.0]
    store = DocumentStore(":memory:")
    guard = ProviderGuard(store, clock=lambda: now[0])
    guard.set("groq", validate_rules({"budget": {"amount": 1, "currency": "USD", "period": "day"}, "tpm": 1000}))
    info = ModelInfo("llama-3.3-70b", "groq", "cloud")
    guard.check(info, "free")
    guard.record("groq", 400, 0.6)
    guard.check(info, "free")
    guard.record("groq", 700, 0.5)  # 1,10 $ dépensés, 1 100 tokens / minute
    try:
        guard.check(info, "free")
        raise AssertionError("aurait dû être bloqué")
    except RateLimited as e:
        assert e.limit == "provider_tpm"
    now[0] += 61  # la minute glissante est passée…
    try:
        guard.check(info, "free")
        raise AssertionError("aurait dû être bloqué")
    except RateLimited as e:
        assert e.limit == "provider_budget_day" and "budget day de 1 USD" in e.message
    now[0] += 86400  # …et le budget quotidien remis à zéro
    guard.check(info, "free")
    assert guard.cap("groq", {"max_tokens": 10}) == {"max_tokens": 10}
