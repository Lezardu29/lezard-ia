"""Bibliothèque Ollama (tous les modèles, rien d'installé d'office) : lecture de la page, cache, repli hors ligne,
mesure réelle d'un modèle, conseil fondé sur la vitesse mesurée."""

import asyncio
import json

import httpx
import pytest

from gateway import benchmark, model_catalog, ollama_library
from gateway.store import DocumentStore

CARD = """<li  class="x"><a href="/library/{name}" class="group"><div title="{name}">
<h2><span>{name}</span></h2><p class="max-w-lg break-words text-neutral-800 text-md">{desc}</p></div>
<div class="flex flex-wrap space-x-2">{caps}{sizes}</div>
<p class="flex space-x-5"><span>120.7M</span> Pulls</p></a></li>"""
PAGE = (
    "<ul>"
    + "".join(
        [
            CARD.format(
                name="qwen3.8",
                desc="Qwen3.8 &amp; co.",
                caps='<span class="a">vision</span><span class="a">tools</span>',
                sizes='<span class="b">27b</span>',
            ),
            CARD.format(
                name="bge-m3",
                desc="BGE-M3 embeddings",
                caps='<span class="a">embedding</span>',
                sizes='<span class="b">567m</span>',
            ),
            CARD.format(name="cloud-only", desc="Cloud", caps='<span class="a">cloud</span>', sizes=""),
        ]
    )
    + "</ul>"
)


def test_parse_reads_every_local_size_and_skips_cloud_only_models():
    models = ollama_library.parse(PAGE)
    names = [m["name"] for m in models]
    assert names == [
        "qwen3.8",
        "bge-m3",
    ]  # cloud-only ignoré ; le nombre de téléchargements (120.7M) n'est pas une taille
    qwen = models[0]
    assert qwen["sizes"] == [{"params_b": 27.0, "label": "27b"}]
    assert qwen["capabilities"] == ["tools", "vision"] and qwen["description"] == "Qwen3.8 & co."
    assert models[1]["sizes"][0]["params_b"] == pytest.approx(0.567)
    assert ollama_library.uses(models[1]["capabilities"]) == ("embeddings",)
    assert ollama_library.uses(qwen["capabilities"]) == ("chat", "vision")


def test_the_list_is_cached_a_day_and_served_stale_when_offline():
    store = DocumentStore(":memory:")
    calls = []

    def page(request):
        calls.append(request.url)
        return httpx.Response(200, text=PAGE)

    client = httpx.AsyncClient(transport=httpx.MockTransport(page))
    first = asyncio.run(ollama_library.cached(store, client))
    assert len(calls) == 1 and first["stale"] is False and len(first["models"]) == 2
    second = asyncio.run(ollama_library.cached(store, client))
    assert len(calls) == 1 and second["models"] == first["models"]  # pas de nouvel appel dans la journée

    store.set_setting(ollama_library.SETTING, {**store.get_setting(ollama_library.SETTING), "at": 0})  # périmée
    offline = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    stale = asyncio.run(ollama_library.cached(store, offline))
    assert stale["stale"] is True and stale["models"] == first["models"]  # hors ligne : dernière liste connue
    with pytest.raises(httpx.HTTPError):
        asyncio.run(ollama_library.cached(DocumentStore(":memory:"), offline))  # jamais lue : erreur, pas de liste vide


def fake_ollama(log: list):
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen3.8:27b", "size": 17e9, "size_vram": 11e9}]})
        body = json.loads(request.content)
        log.append((request.url.path, body.get("keep_alive")))
        if body.get("keep_alive") == 0:
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"eval_count": 160, "eval_duration": int(160 / 15.0 * 1e9)})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_measuring_a_model_gives_its_real_speed_and_unloads_it():
    log, steps = [], []
    result = asyncio.run(benchmark.measure_model(fake_ollama(log), "http://ollama", "qwen3.8:27b", steps.append))
    assert result["tps"] == pytest.approx(15.0, abs=0.1)
    assert result["size"] == 17e9 and result["vram"] == 11e9  # part dans la carte graphique, connue
    assert log[-1][1] == 0  # déchargé à la fin
    assert steps[0].startswith("Chargement") and steps[-1].startswith("Mesure")


def test_a_measured_speed_is_used_instead_of_the_estimate():
    bench = {"gpu": {"overhead_s": 0.001, "bandwidth_gbs": 500.0}, "cpu": {"overhead_s": 0.0, "bandwidth_gbs": 53.0}}
    hw = {"gpus": [{"name": "RTX", "vram_total": 12e9, "vram_used": 0}], "ram": {"available": 16e9}}
    measured = {"qwen2.5:14b": {"tps": 15.0, "size": 9e9, "vram": 9e9, "at": 0}}
    out = model_catalog.recommend(hw, bench=bench, measured=measured)
    mesure = next(m for m in out["models"] if m["model"] == "qwen2.5:14b")
    estime = next(m for m in out["models"] if m["model"] == "llama3.1:8b")
    assert mesure["speed"] == 15.0 and mesure["speed_source"] == "mesuré"
    assert estime["speed_source"] == "estimé" and estime["speed"] != 15.0
