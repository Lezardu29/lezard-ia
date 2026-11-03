"""Test de performance du PC (choix des IA locales après l'installation) : mesure avec un faux Ollama, estimation par
« coût fixe + taille / débit », recommandation du plus gros modèle confortable, routes de la page Modèles."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import benchmark, model_catalog
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
# mesures réelles d'une RTX 5070 (12 Go) : probes et deux vrais modèles, pour vérifier l'estimation
MEASURED = {("qwen2.5:0.5b", False): 608.2, ("qwen2.5:1.5b", False): 356.4,
            ("qwen2.5:0.5b", True): 139.5, ("qwen2.5:1.5b", True): 55.3}  # fmt: skip
SIZES = {"qwen2.5:0.5b": 397_821_319, "qwen2.5:1.5b": 986_061_892}


def fake_ollama(pulled: list, have_probes: bool = True):
    def handler(request):
        if request.url.path == "/api/tags":
            names = SIZES if have_probes or pulled else {}
            return httpx.Response(200, json={"models": [{"name": n, "size": s} for n, s in names.items()]})
        if request.url.path == "/api/delete":
            pulled.append("supprimé:" + json.loads(request.content)["model"])
            return httpx.Response(200, json={})
        if request.url.path == "/api/pull":
            pulled.append(json.loads(request.content)["model"])
            return httpx.Response(200, json={"status": "success"})
        body = json.loads(request.content)
        if "prompt" not in body:  # déchargement (keep_alive 0)
            return httpx.Response(200, json={})
        rate = MEASURED[(body["model"], body["options"].get("num_gpu") == 0)]
        return httpx.Response(200, json={"eval_count": 160, "eval_duration": int(160 / rate * 1e9)})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_the_estimate_is_close_to_real_speeds_and_on_the_safe_side():
    result = asyncio.run(benchmark.run(fake_ollama([]), "http://ollama"))
    assert 450 < result["gpu"]["bandwidth_gbs"] < 560 and result["gpu"]["overhead_s"] > 0
    # vraies vitesses sur ce matériel : qwen2.5:14b 64 tok/s, gemma3:12b 72 tok/s (fichiers de 8,99 et 8,16 Go)
    for size, real in ((8.99, 64.3), (8.16, 72.3)):
        est = benchmark.estimate(result, size, vram_gb=11.4)
        assert 0.75 * real < est <= real  # prudente, à moins de 25 % près
    slower = benchmark.estimate(result, 20.0, vram_gb=11.4)  # partagé VRAM + RAM : bien plus lent
    assert slower < 10


def test_missing_probe_models_are_downloaded_once():
    pulled = []
    asyncio.run(benchmark.run(fake_ollama(pulled, have_probes=False), "http://ollama"))
    assert pulled == [*benchmark.PROBES, *(f"supprimé:{p}" for p in benchmark.PROBES)]  # place rendue après


def test_without_ollama_the_test_explains_what_to_do():
    async def down(request):
        raise httpx.ConnectError("refusé")

    client = httpx.AsyncClient(transport=httpx.MockTransport(down))
    with pytest.raises(benchmark.BenchmarkError, match="Ollama ne répond pas"):
        asyncio.run(benchmark.run(client, "http://ollama"))


def test_labels():
    assert [benchmark.label(x) for x in (60, 12, 5, 1)] == ["rapide", "confortable", "lent", "très lent"]


def test_recommendation_picks_the_biggest_model_that_stays_comfortable():
    bench = asyncio.run(benchmark.run(fake_ollama([]), "http://ollama"))
    hw = {"gpus": [{"name": "RTX", "vram_total": 12e9, "vram_used": 1e9}], "ram": {"available": 16e9}}
    out = model_catalog.recommend(hw, bench=bench)
    chat = next(m for m in out["models"] if m["model"] == out["best"]["chat"])
    assert chat["speed"] >= benchmark.COMFORT_TPS and chat["speed_label"] in ("rapide", "confortable")
    assert all((m["speed"] is None) == (m["uses"] == ["embeddings"]) for m in out["models"])
    assert out["best"]["embeddings"]  # choisi comme avant, sans vitesse
    assert out["benchmark"] == bench
    plain = model_catalog.recommend(hw)  # sans test : comme avant, aucune vitesse
    assert plain["benchmark"] is None and all(m["speed"] is None for m in plain["models"])


def make():
    store = DocumentStore(":memory:")
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=store,
    )
    return TestClient(app), store


def test_routes_require_ollama_and_the_master_key(monkeypatch):
    c, _ = make()
    with c:
        assert c.post("/v1/admin/benchmark").status_code in (401, 403)
        assert c.get("/v1/admin/benchmark", headers=MASTER).json()["state"] == "idle"
        monkeypatch.setattr("gateway.api.admin.find_servers", lambda *a: asyncio.sleep(0, result=[]))
        r = c.post("/v1/admin/benchmark", headers=MASTER)
        assert r.status_code == 409 and "Ollama ne tourne pas" in r.text
        # hors installateur Windows (copie git du développeur) : Ollama s'installe à la main
        r = c.post("/v1/admin/local/ollama/install", headers=MASTER)
        assert r.status_code == 409 and "ollama.com" in r.text
