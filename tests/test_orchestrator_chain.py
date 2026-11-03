"""Multi-agents : découpeur le moins cher (local d'abord, jamais l'offre gratuite OpenRouter tant qu'une autre existe) et
chaîne de secours de l'assembleur."""

import asyncio
import time
from types import SimpleNamespace

from gateway import orchestrator
from gateway.classifier import classify
from gateway.providers.base import ProviderError
from gateway.registry import ModelRegistry

REG = ModelRegistry()
LOCAL = REG.get("ollama", "qwen2.5:14b", "local")
GROQ = REG.get("groq", "llama-3.3-70b-versatile", "cloud")
OR_FREE = REG.get("openrouter", "nvidia/nemotron-3.5-lightning:free", "cloud")
GROQ_BIG = REG.get("groq", "openai/gpt-oss-120b", "cloud")
GROQ_SMALL = REG.get("groq", "llama-3.1-8b-instant", "cloud")


class NoLatency:
    def get(self, ref):
        return None


def sel_with(candidates, info=None, fallbacks=()):
    return SimpleNamespace(
        candidates=candidates,
        decision=SimpleNamespace(mode="equilibre"),
        cls=classify({"messages": [{"role": "user", "content": "Découpe cette demande."}]}),
        info=info or candidates[0],
        fallbacks=list(fallbacks),
    )


def test_planner_prefers_a_local_model_then_cloud_and_never_the_capped_free_offer():
    assert OR_FREE.free and OR_FREE.provider == "openrouter"
    gw = SimpleNamespace(latency=NoLatency())
    sel = sel_with([OR_FREE, GROQ, LOCAL])
    chain = orchestrator.planner_models(gw, sel)
    assert chain[0].ref == LOCAL.ref  # 0 € et sans quota
    assert OR_FREE.ref not in [m.ref for m in chain[:2]]  # l'offre gratuite ne vient qu'après les autres


def test_planner_uses_the_free_offer_only_as_a_last_resort():
    gw = SimpleNamespace(latency=NoLatency())
    chain = orchestrator.planner_models(gw, sel_with([OR_FREE]))
    assert [m.ref for m in chain] == [OR_FREE.ref]  # seule IA disponible : elle sert


def test_assembly_chain_is_the_chosen_model_then_its_fallbacks_within_the_limit(monkeypatch):
    monkeypatch.setenv("FALLBACK_MAX_ATTEMPTS", "3")
    sel = sel_with([GROQ_BIG], info=GROQ_BIG, fallbacks=[GROQ_BIG, GROQ, LOCAL, GROQ_SMALL])
    assert [m.ref for m in orchestrator.assembly_chain(sel)] == [GROQ_BIG.ref, GROQ.ref, LOCAL.ref]


def fake_parts(monkeypatch):
    async def dispatch(gw, sel, body, parts, roles=None):
        return ["R1", "R2"], []

    monkeypatch.setattr(orchestrator, "dispatch", dispatch)


def plan_for(sel):
    planner = orchestrator._agent(LOCAL, "planner", {}, 1)
    return orchestrator.Plan(["Partie 1", "Partie 2"], planner, ["juriste", "RH"]), planner


def test_run_moves_to_the_next_assembler_when_the_first_is_saturated(monkeypatch):
    fake_parts(monkeypatch)
    sel = sel_with([GROQ_BIG], info=GROQ_BIG, fallbacks=[GROQ])

    async def call(gw, sel_, info, body, effort=None, extra=None):
        if info.ref == GROQ_BIG.ref:
            raise ProviderError(429, "quota", info.provider)
        return {"choices": [{"message": {"content": "FINAL"}}], "usage": {"total_tokens": 9}}, 5

    monkeypatch.setattr(orchestrator, "_call", call)
    plan_, _ = plan_for(sel)
    resp, meta = asyncio.run(orchestrator.run(None, sel, {"messages": []}, plan_, time.monotonic()))
    assert resp["choices"][0]["message"]["content"] == "FINAL"
    orc = meta["orchestration"]
    assert orc["assembled"] is True and meta["model"] == GROQ.id
    assert [(a["ref"], a["ok"]) for a in orc["agents"] if a["role"] == "assembler"] == [
        (GROQ_BIG.ref, False),
        (GROQ.ref, True),
    ]


def test_run_returns_the_parts_only_when_the_whole_chain_fails(monkeypatch):
    fake_parts(monkeypatch)
    sel = sel_with([GROQ_BIG], info=GROQ_BIG, fallbacks=[GROQ])

    async def call(gw, sel_, info, body, effort=None, extra=None):
        raise ProviderError(503, "panne", info.provider)

    monkeypatch.setattr(orchestrator, "_call", call)
    plan_, _ = plan_for(sel)
    resp, meta = asyncio.run(orchestrator.run(None, sel, {"messages": []}, plan_, time.monotonic()))
    assert meta["orchestration"]["assembled"] is False
    assert "## Partie 1" in resp["choices"][0]["message"]["content"]
    assert [a["ref"] for a in meta["orchestration"]["agents"] if a["role"] == "assembler"] == [GROQ_BIG.ref, GROQ.ref]


def test_stream_moves_to_the_next_assembler_before_any_chunk(monkeypatch):
    fake_parts(monkeypatch)
    mistral = REG.get("mistral", "mistral-large-latest", "cloud")  # autre fournisseur que le modèle en panne
    sel = sel_with([GROQ_BIG], info=GROQ_BIG, fallbacks=[mistral])
    gw = SimpleNamespace(
        admit=lambda *a: None,
        unadmit=lambda *a: None,
        upstream_payload=lambda body, info, effort, pc, key: {"model": info.id, "messages": body["messages"]},
        health=SimpleNamespace(mark_success=lambda ref: None, mark_failure=lambda ref, e: None),
        providers={},
    )
    sel.effort, sel.prompt_cache, sel.cache_key = None, None, None

    class Provider:
        def __init__(self, fail):
            self.fail = fail

        async def stream(self, payload):
            if self.fail:
                raise ProviderError(429, "quota", "groq")
            yield {"object": "chat.completion.chunk", "choices": [{"delta": {"content": "FIN"}}]}
            yield {"object": "chat.completion.chunk", "choices": [], "usage": {"total_tokens": 4}}

    gw.providers = {GROQ_BIG.provider: Provider(True), mistral.provider: Provider(False)}
    plan_, _ = plan_for(sel)

    async def collect():
        return [c async for c in orchestrator.stream(gw, sel, {"messages": []}, plan_, time.monotonic())]

    chunks = asyncio.run(collect())
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices"))
    assert text == "FIN" and chunks[-1]["x_gateway"]["model"] == mistral.id
