import json

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.health import HealthTracker, is_retryable
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.providers.base import ProviderError

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}

MODELS = {"ollama": ["llama3.1:8b"], "groq": ["llama-3.1-8b-instant"], "openai": ["gpt-4o-mini", "gpt-4o"]}
CONFIGS = [
    ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
    ProviderConfig("groq", "openai", "http://groq/v1", "cloud", api_key="k"),
    ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k"),
]
USAGE = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}


class Upstream:
    """fail : {host: status | "conn"} ; les appels de chat sont enregistrés."""

    def __init__(self, fail=None, retry_after=None):
        self.fail = dict(fail or {})
        self.retry_after = retry_after
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": i} for i in MODELS[host]]})
        body = json.loads(request.content)
        self.calls.append(f"{host}/{body['model']}")
        f = self.fail.get(f"{host}/{body['model']}") or self.fail.get(host)
        if f == "conn":
            raise httpx.ConnectError("serveur local hors-ligne")
        if f:
            headers = {"retry-after": str(self.retry_after)} if self.retry_after else {}
            return httpx.Response(f, text=f"erreur {f}", headers=headers)
        text = f"{host}:{body['model']}"
        if body.get("stream"):
            chunks = [{"choices": [{"delta": {"content": text}}]}, {"choices": [], "usage": USAGE}]
            return httpx.Response(200, text="".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n")
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
                "usage": USAGE,
            },
        )


def client(up, plan="free"):
    app = create_app(
        CONFIGS,
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, plan, master_key=MASTER_KEY),
    )
    return TestClient(app)


def eco(text="Salut !", **kw):
    # mode économique + message simple -> ollama en 1er, puis groq, puis openai
    return {"model": "auto/economy", "messages": [{"role": "user", "content": text}], **kw}


def test_retryable_classification():
    assert is_retryable(ProviderError(429, "", "x")) and is_retryable(ProviderError(503, "", "x"))
    assert is_retryable(ProviderError(401, "", "x")) and not is_retryable(ProviderError(400, "", "x"))


def test_health_cooldowns_with_clock():
    now = [0.0]
    h = HealthTracker(clock=lambda: now[0])
    h.mark_failure("groq/m", ProviderError(429, "", "groq", retry_after=5))
    assert not h.available("groq/m") and h.available("groq/other")  # 429 -> modèle seul
    h.mark_failure("ollama/m", ProviderError(503, "", "ollama"))
    assert not h.available("ollama/anything")  # panne -> tout le provider
    h.mark_failure("openai/m", ProviderError(400, "", "openai"))
    assert h.available("openai/m")  # 400 : pas la faute du provider
    now[0] = 6
    assert h.available("groq/m") and not h.available("ollama/x")
    h.mark_success("ollama/x")
    assert h.available("ollama/x")


def test_safety_classifiers_are_never_routed_for_chat():
    from gateway.registry import ModelRegistry
    from gateway.router import candidates

    found = [
        "nvidia/nemotron-3.5-content-safety:free",
        "meta-llama/llama-guard-4-12b:free",
        "nvidia/nemotron-3.5-lightning:free",
    ]
    refs = [m.ref for m in candidates(ModelRegistry(), {"openrouter": found}, {"openrouter": "cloud"})]
    assert refs == ["openrouter/nvidia/nemotron-3.5-lightning:free"]


def test_403_on_one_model_does_not_disable_the_whole_provider():
    now = [0.0]
    h = HealthTracker(clock=lambda: now[0])
    h.mark_failure("openrouter/thinkingmachines/inkling:free", ProviderError(403, "agentic harnesses", "openrouter"))
    assert not h.available("openrouter/thinkingmachines/inkling:free")
    assert h.available("openrouter/nvidia/nemotron-3.5-lightning:free")  # les autres modèles restent utilisables
    now[0] = 3599
    assert not h.available("openrouter/thinkingmachines/inkling:free")  # écarté une heure
    h.mark_failure("openai/m", ProviderError(401, "clé invalide", "openai"))
    assert not h.available("openai/autre")  # 401 : la clé, donc tout le fournisseur


def test_local_offline_falls_back_and_is_skipped_next_time():
    up = Upstream(fail={"ollama": "conn"})
    with client(up) as c:
        r = c.post("/v1/chat/completions", json=eco()).json()
        fb = r["x_gateway"]["fallback"]
        assert fb["used"] == "groq/llama-3.1-8b-instant"
        assert fb["attempts"][0]["ref"] == "ollama/llama3.1:8b" and fb["attempts"][0]["status"] == 503
        assert r["choices"][0]["message"]["content"] == "groq:llama-3.1-8b-instant"
        assert c.get("/v1/providers", headers=MASTER).json()["ollama"]["cooldown"]["reason"] == "HTTP 503"
        up.calls.clear()
        r = c.post("/v1/chat/completions", json=eco()).json()
        assert up.calls == ["groq/llama-3.1-8b-instant"] and "fallback" not in r["x_gateway"]


def test_rate_limit_falls_back_with_retry_after():
    up = Upstream(fail={"ollama": 429}, retry_after=120)
    with client(up) as c:
        r = c.post("/v1/chat/completions", json=eco()).json()
        assert r["x_gateway"]["fallback"]["used"] == "groq/llama-3.1-8b-instant"
        cd = c.get("/v1/providers", headers=MASTER).json()["ollama"]["model_cooldowns"]["ollama/llama3.1:8b"]
        assert 115 < cd["remaining_s"] <= 120


def test_bad_request_is_not_retried():
    up = Upstream(fail={"ollama": 400})
    with client(up) as c:
        r = c.post("/v1/chat/completions", json=eco())
        assert r.status_code == 400 and up.calls == ["ollama/llama3.1:8b"]


def test_all_fail_respects_max_attempts():
    up = Upstream(fail={"ollama": 503, "groq": 503, "openai": 503})
    with client(up) as c:
        r = c.post("/v1/chat/completions", json=eco())
        assert r.status_code == 503 and len(up.calls) == 3
        assert "Tous les modèles ont échoué" in r.json()["error"]["message"]


def test_explicit_model_fallbacks():
    up = Upstream(fail={"openai": 503})
    with client(up) as c:
        body = {"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "yo"}]}
        assert c.post("/v1/chat/completions", json=body).status_code == 503  # pas de substitution
        r = c.post("/v1/chat/completions", json={**body, "fallbacks": ["groq/llama-3.1-8b-instant"]}).json()
        assert r["x_gateway"]["fallback"]["used"] == "groq/llama-3.1-8b-instant"
        r = c.post("/v1/chat/completions", json=body, headers={"X-Gateway-Fallbacks": "auto"}).json()
        assert r["x_gateway"]["provider"] != "openai"
        prev = c.post("/v1/route", json={**body, "fallbacks": ["ollama/llama3.1:8b"]}).json()
        assert prev["fallbacks"] == ["ollama/llama3.1:8b"]


def test_streaming_fallback_and_total_failure_status():
    up = Upstream(fail={"ollama": "conn"})
    with client(up) as c:
        r = c.post("/v1/chat/completions", json=eco(stream=True))
        events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: {")]
        text = "".join(e["choices"][0]["delta"].get("content", "") for e in events if e.get("choices"))
        assert text == "groq:llama-3.1-8b-instant"
        assert events[-1]["x_gateway"]["fallback"]["used"] == "groq/llama-3.1-8b-instant"
    up = Upstream(fail={"ollama": 503, "groq": 503, "openai": 503})
    with client(up) as c:
        assert c.post("/v1/chat/completions", json=eco(stream=True)).status_code == 503
        r = c.post(
            "/v1/messages",
            json={
                "model": "auto/economy",
                "max_tokens": 10,
                "stream": True,
                "messages": [{"role": "user", "content": "yo"}],
            },
        )
        assert r.status_code == 503


def test_pro_assembler_failure_returns_the_parts():
    """Assembleur en panne (10.5) : les réponses des parties sont renvoyées telles quelles, avec leurs titres."""

    class Splitter(Upstream):
        def __call__(self, request):
            if not request.url.path.endswith("/models"):
                body = json.loads(request.content)
                if "répartis le travail" in str(body["messages"][0].get("content")):
                    parts = json.dumps({"parts": ["Partie A ?", "Partie B ?"]})
                    return httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "index": 0,
                                    "finish_reason": "stop",
                                    "message": {"role": "assistant", "content": parts},
                                }
                            ],
                            "usage": USAGE,
                        },
                    )
            return super().__call__(request)

    hard = "Explique pourquoi et compare deux architectures, analyse les trade-offs."
    up = Splitter()
    body = {"model": "auto", "messages": [{"role": "user", "content": hard}]}
    with client(up, plan="pro") as c:
        prev = c.post("/v1/route", json=body).json()["orchestration"]
        assembler = prev["assembler"]
        assert prev["planner"] != assembler  # sinon le test ne discrimine pas
        up.fail[assembler] = 503  # seul le modèle assembleur tombe : le secours de la chaîne rédige
        r = c.post("/v1/chat/completions", json=body)
        assert r.status_code == 200
        orch = r.json()["x_gateway"]["orchestration"]
        assert orch["assembled"] is True
        assert {"ref": assembler, "role": "assembler", "ok": False, "error": "erreur 503"} in orch["agents"]
        assert orch["agents"][-1]["role"] == "assembler" and orch["agents"][-1]["ref"] != assembler
        assert r.json()["x_gateway"]["model"] == orch["agents"][-1]["ref"].split("/", 1)[1]
