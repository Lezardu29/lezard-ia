import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanError, PlanResolver, account_identity, normalize_plan
from gateway.router import LatencyTracker

MODELS = {
    "ollama": ["llama3.1:8b"],
    "openai": ["gpt-4o", "gpt-4o-mini"],
    "groq": ["llama-3.1-8b-instant"],
    "vllm": ["llama3.2:3b"],
}
CONFIGS = [
    ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
    ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k"),
    ProviderConfig("groq", "openai", "http://groq/v1", "cloud", api_key="k"),
]
BROKEN = [
    ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
    ProviderConfig("vllm", "openai", "http://vllm/v1", "local"),
]

HARD = "Explique pourquoi et compare deux architectures, analyse les trade-offs."
EASY = "Salut !"


class Upstream:
    """Faux providers : enregistre les appels, répond '<host>:<model>'. Au découpeur (10.5), répond les
    sous-questions `parts` en JSON (aucune : réponse non découpable)."""

    def __init__(self, fail_hosts=(), parts=("Que vaut l'architecture A ?", "Que vaut l'architecture B ?")):
        self.calls: list[tuple[str, dict]] = []
        self.fail_hosts = set(fail_hosts)
        self.parts = list(parts)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": i} for i in MODELS[host]]})
        body = json.loads(request.content)
        self.calls.append((host, body))
        if host in self.fail_hosts:
            return httpx.Response(500, text="boom")
        text = f"{host}:{body['model']}"
        if role(body) == "planner":
            text = json.dumps({"parts": self.parts})
        usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        if body.get("stream"):
            chunks = [{"choices": [{"delta": {"content": text}}]}, {"choices": [], "usage": usage}]
            return httpx.Response(200, text="".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n")
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
                "usage": usage,
            },
        )


def role(body: dict) -> str:
    first = str(body["messages"][0].get("content"))
    return (
        "planner"
        if "répartis le travail" in first
        else "worker"
        if "UNE partie" in first
        else "assembler"
        if "Assemble leurs" in first
        else "single"
    )


def client(up=None, configs=CONFIGS, keys=None, default="free"):
    up = up or Upstream()
    app = create_app(
        configs,
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver(keys or {}, default),
    )
    return TestClient(app), up


def msg(text, **kw):
    return {"model": "auto", "messages": [{"role": "user", "content": text}], **kw}


# ---------- Résolution du plan ----------


def test_plan_resolution_and_no_escalation():
    r = PlanResolver({"pro-key": "pro"}, "free")
    assert r.resolve("pro-key", "1.1.1.1", None).plan == "pro"
    assert r.resolve(None, "1.1.1.1", None).plan == "free"
    assert r.resolve("pro-key", "x", "free").plan == "free"  # rétrogradation permise
    with pytest.raises(PlanError) as e:
        r.resolve(None, "x", "pro")
    assert e.value.status == 403


def test_old_trial_plan_is_read_as_free():
    """L'ancienne offre d'essai est retirée : une configuration qui la nomme encore démarre, en free."""
    assert normalize_plan("sandbox") == normalize_plan("test") == normalize_plan("demo") == "free"
    r = PlanResolver({"ancien-compte": "sandbox"}, "sandbox")
    assert r.resolve("ancien-compte", "x", None).plan == "free" and r.resolve(None, "x", None).plan == "free"
    with pytest.raises(PlanError, match="free, pro"):
        normalize_plan("inconnu")


def test_old_trial_keys_and_rules_never_gain_access():
    """Données d'avant le retrait : une session d'essai anonyme est refusée (jamais une clé free), une clé de compte
    réglée sur l'ancienne offre est lue comme free, et une règle de fournisseur ne s'ouvre jamais davantage."""
    import time

    from gateway.keys import display_prefix, generate_secret, hash_secret
    from gateway.provider_rules import validate_rules
    from gateway.store import DocumentStore

    store = DocumentStore(":memory:")
    app = create_app(
        CONFIGS,
        httpx.AsyncClient(transport=httpx.MockTransport(Upstream())),
        plans=PlanResolver({"compte-0123456789abcdefghijk": "pro"}, "free"),
        store=store,
    )

    def key(owner):
        secret = generate_secret()
        data = {
            "name": "ancienne",
            "plan": "sandbox",
            "key_prefix": display_prefix(secret),
            "expires_at": time.time() + 3600,
        }
        store.add_key(owner, data, hash_secret(secret))
        return {"Authorization": f"Bearer {secret}"}

    with TestClient(app) as c:
        r = c.get("/v1/me", headers=key("sandbox:0123abcd"))  # session anonyme, pas encore expirée
        assert r.status_code == 401 and "expirée" in r.json()["error"]["message"]
        owner = account_identity("compte-0123456789abcdefghijk")  # compte existant, clé réglée sur l'ancienne offre
        assert c.get("/v1/me", headers=key(owner)).json()["plan"] == "free"
    assert validate_rules({"plans": ["sandbox"]})["plans"] == ["pro"]
    assert validate_rules({"plans": ["sandbox", "free"]})["plans"] == ["free"]


def test_plan_endpoint_and_header_escalation():
    c, _ = client(keys={"pro-key": "pro"})
    with c:
        assert c.get("/v1/plan").json()["plan"] == "free"
        assert c.get("/v1/plan", headers={"Authorization": "Bearer pro-key"}).json()["features"]["multi_agent"]
        assert c.post("/v1/chat/completions", json=msg(EASY), headers={"X-Gateway-Plan": "pro"}).status_code == 403


# ---------- Free / Pro ----------


def test_multi_agents_included_for_everyone():
    """Tout est inclus : une demande complexe est répartie entre plusieurs IA, même sans plan Pro ; « single »
    garde une seule IA."""
    c, up = client()
    with c:
        r = c.post("/v1/chat/completions", json=msg(HARD)).json()
        assert r["x_gateway"]["orchestration"]["decomposed"] and len(up.calls) == 4
        up.calls.clear()
        r = c.post("/v1/chat/completions", json=msg(HARD, orchestration="single")).json()
        assert len(up.calls) == 1 and "orchestration" not in r["x_gateway"]


def test_pro_decomposes_dispatches_in_parallel_and_assembles():
    c, up = client(default="pro")
    with c:
        r = c.post("/v1/chat/completions", json=msg(HARD)).json()
        orch = r["x_gateway"]["orchestration"]
        assert orch["strategy"] == "decompose-parallel-assemble" and orch["decomposed"] and orch["assembled"]
        assert orch["parts"] == up.parts
        assert [a["role"] for a in orch["agents"]] == ["planner", "worker", "worker", "assembler"]
        assert [role(b) for _, b in up.calls] == ["planner", "worker", "worker", "assembler"]
        # chaque partie part seule (avec la demande complète en contexte), vers des IA différentes
        workers = [b for _, b in up.calls[1:3]]
        assert sorted(w["messages"][-1]["content"] for w in workers) == sorted(up.parts)
        assert all(HARD in w["messages"][0]["content"] for w in workers)
        refs = [a["ref"] for a in orch["agents"] if a["role"] == "worker"]
        assert len(set(refs)) == 2
        # l'assembleur reçoit les parties et leurs réponses
        last = up.calls[-1][1]["messages"][-1]["content"]
        assert "Partie 1 : " + up.parts[0] in last and "Partie 2 : " + up.parts[1] in last
        assert r["usage"] == {"prompt_tokens": 40, "completion_tokens": 80, "total_tokens": 120}
        assert r["x_gateway"]["cost_usd"] == pytest.approx(sum(a["cost_usd"] for a in orch["agents"]))
        assert r["choices"][0]["message"]["content"] == f"{up.calls[-1][0]}:{up.calls[-1][1]['model']}"


def test_indivisible_request_costs_one_call_plus_planning():
    up = Upstream(parts=["Explique et compare deux architectures."])  # le découpeur ne découpe pas
    c, _ = client(up, default="pro")
    with c:
        r = c.post("/v1/chat/completions", json=msg(HARD)).json()
        orch = r["x_gateway"]["orchestration"]
        assert [role(b) for _, b in up.calls] == ["planner", "single"]
        assert orch["decomposed"] is False and [a["role"] for a in orch["agents"]] == ["planner", "worker"]
        assert r["x_gateway"]["cost_usd"] == pytest.approx(sum(a["cost_usd"] for a in orch["agents"]))
        up.parts = []  # réponse illisible du découpeur : idem
        up.calls.clear()
        c.post("/v1/chat/completions", json=msg(HARD))
        assert [role(b) for _, b in up.calls] == ["planner", "single"]


def test_objective_speed_or_cost_drives_every_agent(monkeypatch):
    # latences a priori du registre : les faux fournisseurs répondent tous en quelques ms, et départager ces
    # mesures rendrait le test aléatoire (échecs constatés en CI sous charge)
    monkeypatch.setattr(LatencyTracker, "observe", lambda self, ref, ms: None)
    c, up = client(default="pro")
    with c:
        eco = c.post("/v1/chat/completions", json=msg(HARD, mode="economy")).json()["x_gateway"]["orchestration"]
        assert eco["agents"][0]["ref"] == "ollama/llama3.1:8b"  # découpage : gratuit
        assert eco["agents"][1]["ref"] == "ollama/llama3.1:8b"  # 1re partie : gratuite
        assert up.calls[0][1]["reasoning_effort"] == "none"  # Ollama : découpage sans réflexion
        up.calls.clear()
        fast = c.post("/v1/chat/completions", json=msg(HARD, mode="speed")).json()["x_gateway"]["orchestration"]
        assert fast["agents"][0]["ref"] == "groq/llama-3.1-8b-instant"  # découpage : le plus rapide
        assert "reasoning_effort" not in up.calls[0][1]  # paramètre réservé à Ollama


def test_pro_simple_request_stays_single_unless_forced():
    c, up = client(default="pro")
    with c:
        c.post("/v1/chat/completions", json=msg(EASY))
        assert len(up.calls) == 1
        r = c.post("/v1/chat/completions", json=msg(EASY, orchestration="single")).json()
        assert "orchestration" not in r["x_gateway"]
        r = c.post("/v1/chat/completions", json=msg(EASY, orchestration="multi")).json()
        assert "orchestration" in r["x_gateway"]


def test_pro_explicit_model_is_never_orchestrated():
    c, up = client(default="pro")
    with c:
        c.post("/v1/chat/completions", json=msg(HARD, model="openai/gpt-4o"))
        assert len(up.calls) == 1


def test_pro_survives_failed_worker():
    up = Upstream(fail_hosts={"vllm"})
    c, _ = client(up, configs=BROKEN, default="pro")
    with c:
        r = c.post("/v1/chat/completions", json=msg(EASY, orchestration="multi"))
        assert r.status_code == 200
        orch = r.json()["x_gateway"]["orchestration"]
        failed = [a for a in orch["agents"] if not a["ok"]]
        assert failed and all(a["ref"].startswith("vllm/") for a in failed)
        done = {a["part"] for a in orch["agents"] if a["role"] == "worker" and a["ok"]}
        assert done == {1, 2} and orch["agents"][-1]["role"] == "assembler"  # partie retentée sur une autre IA


def test_pro_streaming_and_route_preview():
    c, up = client(default="pro")
    with c:
        r = c.post("/v1/chat/completions", json=msg(HARD, stream=True))
        events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: {")]
        text = "".join(e["choices"][0]["delta"].get("content", "") for e in events if e.get("choices"))
        assert text == f"{up.calls[-1][0]}:{up.calls[-1][1]['model']}"
        assert events[-1]["usage"]["total_tokens"] == 120
        assert len(events[-1]["x_gateway"]["orchestration"]["agents"]) == 4
        prev = c.post("/v1/route", json=msg(HARD)).json()["orchestration"]
        assert prev["strategy"] == "decompose-parallel-assemble" and prev["max_parts"] == 4
        assert prev["planner"] and prev["assembler"] and prev["est_overhead_usd"] >= 0


def test_anthropic_endpoint_with_plans():
    c, up = client(default="pro")
    with c:
        r = c.post(
            "/v1/messages", json={"model": "auto", "max_tokens": 50, "messages": [{"role": "user", "content": HARD}]}
        ).json()
        assert r["type"] == "message" and len(r["x_gateway"]["orchestration"]["agents"]) == 4
