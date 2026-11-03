import json
import time
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.registry import ModelInfo, usd_eur_rate

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
OLLAMA = ProviderConfig("ollama", "openai", "http://ollama/v1", "local")
OPENAI = ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k1")
ANTH = ProviderConfig("anthropic", "anthropic", "http://anthropic/v1", "cloud", api_key="k2")
OFF = ProviderConfig("groq", "openai", "http://groq/v1", "cloud", enabled=False)
DOWN = ProviderConfig("vllm", "openai", "http://vllm/v1", "local")

OAI_RESP = {
    "id": "x",
    "object": "chat.completion",
    "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "salut"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
}


def handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    body = json.loads(request.content) if request.content else {}
    if host == "vllm":
        raise httpx.ConnectError("offline")
    if path.endswith("/models"):
        ids = {"ollama": ["llama3:8b"], "openai": ["gpt-4o-mini"], "anthropic": ["claude-haiku-4-5"]}[host]
        return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
    if host == "anthropic":
        assert request.headers["x-api-key"] == "k2"
        assert body["system"] == "sois bref" and body["max_tokens"] == 4096
        if body.get("stream"):
            evs = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "He"}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "y"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
                {"type": "message_stop"},
            ]
            return httpx.Response(200, text="".join(f"data: {json.dumps(e)}\n\n" for e in evs))
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "content": [{"type": "text", "text": "Hey"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
            },
        )
    if host == "openai":
        assert request.headers["authorization"] == "Bearer k1"
    if body.get("stream"):
        chunks = [
            {"choices": [{"delta": {"content": "a"}}]},
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}},
        ]
        return httpx.Response(200, text="".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n")
    return httpx.Response(200, json=OAI_RESP)


def make_client():
    transport = httpx.MockTransport(handler)
    app = create_app(
        [OLLAMA, OPENAI, ANTH, OFF, DOWN],
        httpx.AsyncClient(transport=transport),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
    )
    return TestClient(app)


REQ = {"messages": [{"role": "system", "content": "sois bref"}, {"role": "user", "content": "yo"}]}


def counting_client(monkeypatch, interval: str):
    """Client dont on compte les sondes /models (détection)."""
    monkeypatch.setenv("DETECT_UNKNOWN_INTERVAL", interval)
    probes = []

    def counting(request):
        if request.url.path.endswith("/models"):
            probes.append(request.url.host)
        return handler(request)

    app = create_app(
        [OLLAMA, OPENAI],
        httpx.AsyncClient(transport=httpx.MockTransport(counting)),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
    )
    return TestClient(app), probes


def unknown(i):
    return {"model": f"modele-inconnu-{i}", "messages": [{"role": "user", "content": "Bonjour"}]}


def test_unknown_models_do_not_amplify_detection(monkeypatch):
    """9.3 : des noms de modèles inventés ne déclenchent plus une sonde de tous les fournisseurs par requête."""
    c, probes = counting_client(monkeypatch, "60")
    with c:
        start = len(probes)  # sonde du démarrage
        for i in range(50):
            assert c.post("/v1/chat/completions", json=unknown(i)).status_code == 404
        for _ in range(20):
            assert c.get("/v1/providers", headers=MASTER).status_code == 200
        assert len(probes) == start  # tout servi depuis le cache
        # un changement de configuration (admin) force une sonde neuve
        c.get("/v1/admin/providers", headers=MASTER)
        assert len(probes) == start + 2


def test_unknown_model_redetects_once(monkeypatch):
    c, probes = counting_client(monkeypatch, "0")  # cache désactivé pour le test
    with c:
        start = len(probes)
        for i in range(3):
            c.post("/v1/chat/completions", json=unknown(i))
        assert len(probes) - start == 3 * 2  # 1 redétection (2 fournisseurs) par requête, pas 2


def test_forced_detect_with_coarse_clock(monkeypatch):
    """15.2 : horloge à gros grain (Windows avant Python 3.13, 15,6 ms) — une sonde terminée dans le même tic
    passait pour « partie après l'appel » : une détection forcée renvoyait l'ancien résultat."""
    import gateway.core as core

    c, probes = counting_client(monkeypatch, "60")
    with c:
        # l'horloge du moteur n'avance plus — celle du moteur seulement : figer time.monotonic pour tout le
        # processus figerait aussi la boucle asyncio, et les tâches de fond (supervision 18.5…) partiraient
        # aussitôt si l'horloge réelle est en dessous (machine de CI démarrée depuis peu)
        monkeypatch.setattr(core, "time", SimpleNamespace(monotonic=lambda: 1000.0, time=time.time))
        start = len(probes)
        c.get("/v1/admin/providers", headers=MASTER)  # détection forcée
        c.get("/v1/admin/providers", headers=MASTER)  # la suivante aussi : sonde neuve
        assert len(probes) - start == 2 * 2


def test_detect_single_flight():
    """Appels simultanés : une sonde en cours est partagée au lieu d'être relancée par chacun."""
    import asyncio
    from gateway.main import Gateway

    hits = []

    async def slow(request):
        hits.append(1)
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    async def run():
        gw = Gateway([OLLAMA], httpx.AsyncClient(transport=httpx.MockTransport(slow)))
        await asyncio.gather(*(gw.detect(force=True) for _ in range(10)))
        await asyncio.gather(*(gw.detect() for _ in range(10)))
        return gw.probes

    assert asyncio.run(run()) <= 2 and len(hits) <= 2


def test_detect_and_models():
    with make_client() as c:
        assert c.get("/v1/providers").status_code == 403  # réservé aux comptes (9.3)
        prov = c.get("/v1/providers", headers=MASTER).json()
        assert prov["ollama"]["online"] and prov["ollama"]["models"] == ["llama3:8b"]
        assert not prov["vllm"]["online"]
        ids = {m["id"] for m in c.get("/v1/models").json()["data"]}
        assert "ollama/llama3:8b" in ids and "openai/gpt-4o-mini" in ids
        assert not any(i.startswith("groq/") for i in ids)
        meta = next(m for m in c.get("/v1/models").json()["data"] if m["id"] == "openai/gpt-4o-mini")
        assert meta["metadata"]["context_window"] == 128_000


def test_local_is_free_and_bare_model_resolves():
    with make_client() as c:
        r = c.post("/v1/chat/completions", json={**REQ, "model": "llama3:8b"}).json()
        assert r["x_gateway"]["provider"] == "ollama"
        assert r["x_gateway"]["cost_eur"] == 0
        assert r["x_gateway"]["classification"]["task"] == "chat"
        cl = c.post("/v1/classify", json={"messages": [{"role": "user", "content": "debug ce code"}]})
        assert cl.json()["needs_code"]


def test_auto_routing_endpoints():
    with make_client() as c:
        r = c.post("/v1/chat/completions", json={**REQ, "model": "auto/economy"}).json()
        assert r["x_gateway"]["provider"] == "ollama"
        assert r["x_gateway"]["routing"]["mode"] == "economy"
        r = c.post("/v1/chat/completions", json={**REQ, "model": "auto"}, headers={"X-Gateway-Mode": "speed"}).json()
        assert r["x_gateway"]["routing"]["mode"] == "speed"
        sim = c.post("/v1/route", json={**REQ, "mode": "balanced"}).json()
        assert sim["routing"]["ref"] and sim["classification"]["task"] == "chat"
        assert c.post("/v1/chat/completions", json={**REQ, "model": "auto/turbo"}).status_code == 400
        assert c.post("/v1/chat/completions", json={**REQ, "model": "auto", "effort": "turbo"}).status_code == 400
        sim = c.post(
            "/v1/route", json={**REQ, "model": "anthropic/claude-haiku-4-5"}, headers={"X-Gateway-Effort": "eleve"}
        ).json()
        assert sim["effort"] == "high" and sim["upstream"]["thinking"]["type"] == "enabled"
        sim = c.post("/v1/route", json={**REQ, "model": "ollama/llama3:8b", "effort": "high"}).json()
        assert sim["upstream"]["cot_prompt"] and sim["upstream"]["reasoning_effort"] is None
        r = c.post("/v1/chat/completions", json={**REQ, "model": "llama3:8b", "effort": "low"}).json()
        assert r["x_gateway"]["effort"] == "low"
        m = c.post(
            "/v1/messages",
            json={"model": "auto-eco", "max_tokens": 10, "messages": [{"role": "user", "content": "yo"}]},
        ).json()
        assert m["type"] == "message"


def test_cloud_cost_conversion():
    with make_client() as c:
        r = c.post("/v1/chat/completions", json={**REQ, "model": "openai/gpt-4o-mini"}).json()
        # 1M in * 0.15 + 1M out * 0.60 = 0.75 USD
        assert r["x_gateway"]["cost_usd"] == 0.75
        assert abs(r["x_gateway"]["cost_eur"] - 0.75 * usd_eur_rate()) < 1e-6


def test_anthropic_translation():
    with make_client() as c:
        r = c.post("/v1/chat/completions", json={**REQ, "model": "anthropic/claude-haiku-4-5"}).json()
        assert r["choices"][0]["message"]["content"] == "Hey"
        assert r["usage"]["prompt_tokens"] == 1_000_000
        assert r["x_gateway"]["cost_usd"] == 6.0


def test_anthropic_stream_to_openai():
    with make_client() as c:
        r = c.post("/v1/chat/completions", json={**REQ, "model": "anthropic/claude-haiku-4-5", "stream": True})
        lines = [l for l in r.text.splitlines() if l.startswith("data: ")]
        assert lines[-1] == "data: [DONE]"
        chunks = [json.loads(l[6:]) for l in lines[:-1]]
        text = "".join(ch["choices"][0]["delta"].get("content", "") for ch in chunks if ch["choices"])
        assert text == "Hey"
        assert chunks[-1]["usage"]["completion_tokens"] == 2 and "x_gateway" in chunks[-1]


def test_openai_stream_passthrough_has_meta():
    with make_client() as c:
        r = c.post("/v1/chat/completions", json={**REQ, "model": "ollama/llama3:8b", "stream": True})
        assert '"x_gateway"' in r.text and r.text.strip().endswith("[DONE]")


def test_anthropic_inbound_endpoint():
    with make_client() as c:
        r = c.post(
            "/v1/messages",
            json={
                "model": "ollama/llama3:8b",
                "max_tokens": 10,
                "system": "sois bref",
                "messages": [{"role": "user", "content": "yo"}],
            },
        ).json()
        assert r["type"] == "message" and r["content"][0]["text"] == "salut"
        assert r["stop_reason"] == "end_turn"
        s = c.post(
            "/v1/messages",
            json={
                "model": "ollama/llama3:8b",
                "max_tokens": 10,
                "stream": True,
                "messages": [{"role": "user", "content": "yo"}],
            },
        )
        assert "content_block_delta" in s.text and "message_stop" in s.text


def test_errors():
    with make_client() as c:
        assert c.post("/v1/chat/completions", json={**REQ, "model": "nope"}).status_code == 404
        assert c.post("/v1/chat/completions", json={**REQ, "model": "groq/x"}).status_code == 400
        assert c.post("/v1/chat/completions", json={"model": "ollama/x"}).status_code == 400


def test_registry_default_unknown_local_free():
    assert ModelInfo("x", "ollama", "local", 1, 5, 5).cost_usd(10**6, 10**6) == 0
