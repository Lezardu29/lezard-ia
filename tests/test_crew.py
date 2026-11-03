"""Multi-agents CrewAI pour les gros modèles : seuil de taille, repli, et exécution hors réseau (modèle factice)."""

import asyncio
import importlib.util
import time
from types import SimpleNamespace

import pytest

from gateway import crew, orchestrator
from gateway.providers.base import ProviderError
from gateway.registry import ModelRegistry


def local(model_id):
    return ModelRegistry().get("ollama", model_id, "local")


def test_size_is_read_from_the_model_name():
    assert crew.model_size_b("openai/gpt-oss-120b") == 120
    assert crew.model_size_b("llama3.3:70b") == 70
    assert crew.model_size_b("qwen2.5:14b") == 14
    assert crew.model_size_b("gpt-4o") is None


def test_only_big_models_use_crewai(monkeypatch):
    monkeypatch.setattr(crew, "available", lambda: True)
    assert crew.applies(local("llama3.3:70b")) is True
    assert crew.applies(local("qwen2.5:14b")) is False
    assert crew.applies(local("gpt-4o")) is False  # taille inconnue : pas de CrewAI


def test_nothing_changes_when_crewai_is_not_installed(monkeypatch):
    monkeypatch.setattr(crew, "available", lambda: False)
    assert crew.applies(local("llama3.3:70b")) is False


def test_missing_package_is_detected(monkeypatch):
    monkeypatch.setattr(crew.importlib.util, "find_spec", lambda name: None)
    assert crew.available.__wrapped__() is False


def test_detection_does_not_import_the_package(monkeypatch):
    imported = []
    monkeypatch.setattr(crew.importlib.util, "find_spec", lambda name: imported.append(name) or object())
    assert crew.available.__wrapped__() is True and imported == ["crewai"]


def fake_call_factory(saturated=()):
    async def fake_call(gw, sel, info, body, effort=None, extra=None):
        if info.ref in saturated:
            raise ProviderError(429, "quota atteint", info.provider)
        system = body["messages"][0]["content"] if body["messages"][0]["role"] == "system" else ""
        last = body["messages"][-1]["content"]
        content = "RÉPONSE FINALE" if "Plusieurs assistants" in system else f"PARTIE[{last[:30]}]"
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return {"choices": [{"message": {"content": content}}], "usage": usage}, 7

    return fake_call


def test_run_falls_back_to_the_orchestrator_when_crewai_fails(monkeypatch):
    called = {}

    async def broken(*a, **k):
        raise RuntimeError("panne CrewAI simulée")

    async def fallback(gw, sel, body, plan, t0):
        called["ok"] = True
        return {"choices": [{"message": {"content": "repli"}}], "usage": {}}, {"orchestration": {}}

    monkeypatch.setattr(crew, "_run", broken)
    monkeypatch.setattr(orchestrator, "run", fallback)
    plan = orchestrator.Plan(["A", "B"], {"role": "planner"}, ["x", "y"])
    resp, _ = asyncio.run(
        crew.run(None, SimpleNamespace(info=local("llama3.3:70b")), {"messages": []}, plan, time.monotonic())
    )
    assert called == {"ok": True} and resp["choices"][0]["message"]["content"] == "repli"


BIG, SPARE, PART_A, PART_B = local("llama3.3:70b"), local("qwen2.5:32b"), local("qwen2.5:14b"), local("gemma3:12b")


def crew_run(monkeypatch, saturated=()):
    """Exécution réelle de CrewAI, hors réseau : parties réparties sur deux IA (avec remplaçante), gros modèle qui
    rédige, secours de la chaîne 2.5."""
    # le paquet est facultatif : sans lui, ces tests ne s'appliquent pas. Présence vérifiée SANS l'importer : son
    # import lit .env (python-dotenv) ; c'est crew._load qui l'importe, protégé
    if importlib.util.find_spec("crewai") is None:
        pytest.skip("crewai non installé")
    monkeypatch.setattr(orchestrator, "_call", fake_call_factory(saturated))
    monkeypatch.setattr(orchestrator, "assign", lambda gw, sel, parts: [[PART_A, SPARE], [PART_B, SPARE]])
    sel = SimpleNamespace(info=BIG, fallbacks=[SPARE])
    body = {"messages": [{"role": "user", "content": "Compare un bail et un contrat de travail."}]}
    planner = orchestrator._agent(PART_A, "planner", {}, 1)
    plan = orchestrator.Plan(["Clauses du bail", "Clauses du contrat"], planner, ["juriste", "RH"])
    return asyncio.run(crew._run(None, sel, body, plan, time.monotonic()))


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # avertissements internes de crewai (paramètres dépréciés)
def test_crewai_spreads_parts_and_lets_the_big_model_write(monkeypatch):
    resp, meta = crew_run(monkeypatch)
    assert resp["choices"][0]["message"]["content"] == "RÉPONSE FINALE"
    orc = meta["orchestration"]
    assert orc["strategy"] == crew.STRATEGY and orc["assembled"] is True
    workers = {a["expert"]: a["ref"] for a in orc["agents"] if a["role"] == "worker"}
    assert workers == {"juriste": PART_A.ref, "RH": PART_B.ref}  # une IA par partie, pas tout sur le gros modèle
    assert [a["ref"] for a in orc["agents"] if a["role"] == "assembler"] == [BIG.ref]
    assert meta["model"] == BIG.id and resp["usage"]["total_tokens"] > 0


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_saturated_models_are_replaced_for_parts_and_assembly(monkeypatch):
    resp, meta = crew_run(monkeypatch, saturated={PART_A.ref, BIG.ref})
    assert resp["choices"][0]["message"]["content"] == "RÉPONSE FINALE"
    agents = meta["orchestration"]["agents"]
    part_a = [(a["ref"], a["ok"]) for a in agents if a["role"] == "worker" and a.get("expert") == "juriste"]
    assert part_a == [(PART_A.ref, False), (SPARE.ref, True)]
    assembly = [(a["ref"], a["ok"]) for a in agents if a["role"] == "assembler"]
    assert assembly == [(BIG.ref, False), (SPARE.ref, True)]
    assert meta["model"] == SPARE.id  # l'IA qui a réellement rédigé


def cloud(provider, model_id):
    return ModelRegistry().get(provider, model_id, "cloud")


def test_frontier_cloud_models_count_as_big_even_without_a_size_in_their_name():
    assert crew.is_big(cloud("anthropic", "claude-opus-5-5")) is True
    assert crew.is_big(cloud("gemini", "gemini-3.1-pro-preview")) is True
    assert crew.is_big(cloud("openai", "gpt-4o-mini")) is False  # petit modèle cloud
    assert crew.is_big(cloud("groq", "llama-3.1-8b-instant")) is False  # taille connue : elle décide
    assert crew.is_big(local("gpt-4o")) is False  # local sans taille : jamais « gros » par défaut
    assert crew.is_big(local("qwen2.5:14b")) is False


def test_the_quality_threshold_can_be_tuned(monkeypatch):
    monkeypatch.setenv("CREW_MIN_QUALITY", "0.98")
    assert crew.is_big(cloud("anthropic", "claude-opus-5-5")) is False


def test_a_big_model_answers_alone_unless_the_request_is_complex():
    big, small = cloud("anthropic", "claude-opus-5-5"), local("qwen2.5:14b")
    assert crew.worth_experts("high", big) is True  # complexe : experts (CrewAI)
    assert crew.worth_experts("medium", big) is False  # moyenne : le gros modèle seul
    assert crew.worth_experts("low", big) is False
    assert crew.worth_experts("medium", small) is True  # local : orchestrateur dès « medium », comme avant
    assert crew.worth_experts("low", small) is False


def test_a_cloud_assembler_keeps_its_minute_quota_for_the_assembly(monkeypatch):
    """Offres gratuites (Groq : tokens par minute) : l'assembleur cloud ne traite pas de partie s'il y a d'autres IA."""
    big, a, b = (
        cloud("groq", "openai/gpt-oss-120b"),
        cloud("groq", "openai/gpt-oss-20b"),
        cloud("groq", "qwen/qwen3.8-27b"),
    )
    ranking = SimpleNamespace(ref=big.ref, alternatives=[a.ref, b.ref])
    monkeypatch.setattr(orchestrator, "route", lambda *args, **kw: ranking)
    sel = SimpleNamespace(info=big, candidates=[big, a, b], decision=SimpleNamespace(mode="balanced"), effort=None)
    jobs = orchestrator.assign(SimpleNamespace(latency=None), sel, ["partie 1", "partie 2"])
    assert [j[0].ref for j in jobs] == [a.ref, b.ref]
    four = orchestrator.assign(SimpleNamespace(latency=None), sel, ["p1", "p2", "p3", "p4"])  # plus de parties que d'IA
    assert big.ref not in [j[0].ref for j in four]
    small = local("qwen2.5:14b")  # assembleur local : rien ne change (pas de quota, pas de changement de modèle)
    monkeypatch.setattr(orchestrator, "route", lambda *args, **kw: SimpleNamespace(ref=small.ref, alternatives=[]))
    sel = SimpleNamespace(info=small, candidates=[small], decision=SimpleNamespace(mode="balanced"), effort=None)
    assert orchestrator.assign(SimpleNamespace(latency=None), sel, ["partie"])[0][0].ref == small.ref


def test_expert_answers_are_capped_so_the_assembly_fits(monkeypatch):
    body = {"messages": [{"role": "user", "content": "q"}], "max_completion_tokens": 9000}
    worker = orchestrator.worker_body(body, "q", "partie")
    assert worker["max_tokens"] == 1200 and "max_completion_tokens" not in worker
    assert orchestrator.capped({"messages": [], "max_tokens": 300})["max_tokens"] == 300  # plus court : gardé
    monkeypatch.setenv("ORCHESTRATION_PART_TOKENS", "2500")
    assert orchestrator.capped({"messages": []})["max_tokens"] == 2500


def test_crewai_never_loads_the_env_file_into_the_gateway(monkeypatch, tmp_path):
    """Constaté : CrewAI lisait .env (python-dotenv) et y prenait clé maître, licence et jetons."""
    dotenv = pytest.importorskip("dotenv")
    main = pytest.importorskip("dotenv.main")
    monkeypatch.setattr(dotenv, "load_dotenv", dotenv.load_dotenv)  # remis en place après le test
    monkeypatch.setattr(main, "load_dotenv", main.load_dotenv)
    (tmp_path / ".env").write_text("GATEWAY_FUITE_TEST=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GATEWAY_FUITE_TEST", raising=False)
    crew._no_dotenv()
    assert dotenv.load_dotenv() is False and main.load_dotenv() is False
    import os

    assert "GATEWAY_FUITE_TEST" not in os.environ
