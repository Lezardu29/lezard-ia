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


def test_parse_reads_every_local_size_and_marks_the_others():
    models = ollama_library.parse(PAGE)
    assert [m["name"] for m in models] == [
        "qwen3.8",
        "bge-m3",
        "cloud-only",
    ]  # 120.7M (téléchargements) : pas une taille
    qwen = models[0]
    assert qwen["sizes"] == [{"params_b": 27.0, "label": "27b"}]
    assert qwen["capabilities"] == ["tools", "vision"] and qwen["description"] == "Qwen3.8 & co."
    assert models[1]["sizes"][0]["params_b"] == pytest.approx(0.567)
    assert models[2]["sizes"] == [] and models[2]["pending"] == "cloud"  # complété par la page de ses étiquettes
    assert ollama_library.uses(models[1]["capabilities"]) == ("embeddings",)
    assert ollama_library.uses(qwen["capabilities"]) == ("chat", "vision")


def tags_page(name: str, rows: list[tuple[str, str]]) -> str:
    """Page des étiquettes d'ollama.com : « nom:étiquette empreinte • taille • contexte »."""
    return "".join(
        f'<a href="/library/{name}:{t}"><span>{name}:{t}</span> <span>0a377808afea</span> • <span>{size}</span> • 32K'
        f" context window</a>"
        for t, size in rows
    )


def test_every_model_of_the_library_is_listed():
    """Tailles que la carte ne sait pas dire (« 8x7b », « e4b », aucune) : lues sur la page des étiquettes ;
    modèles cloud d'Ollama : listés avec leur étiquette « cloud »."""
    page = (
        "<ul>"
        + "".join(
            [
                CARD.format(name="qwen3.8", desc="Q", caps="", sizes='<span class="b">27b</span>'),
                CARD.format(name="mixtral", desc="M", caps="", sizes="<span>8x7b</span><span>8x22b</span>"),
                CARD.format(name="nomic-embed-text", desc="N", caps="<span>embedding</span>", sizes=""),
                CARD.format(name="wizardlm", desc="W", caps="", sizes=""),
                CARD.format(name="kimi-k3", desc="K", caps="<span>vision</span><span>cloud</span>", sizes=""),
            ]
        )
        + "</ul>"
    )
    tags = {
        "mixtral": [("latest", "26GB"), ("instruct", "26GB"), ("8x22b", "80GB"), ("8x7b-instruct-v0.1-q8_0", "50GB")],
        "nomic-embed-text": [("latest", "274MB"), ("v1.5", "274MB"), ("q8_0", "200MB")],
        "wizardlm": [("7b-q2_K", "2.8GB"), ("7b-q4_0", "3.8GB"), ("13b-q4_0", "7.4GB")],
        "kimi-k3": [("cloud", "Extra High Usage")],
    }

    def site(request):
        path = request.url.path
        if path == "/library":
            return httpx.Response(200, text=page)
        name = path.split("/")[2]
        return httpx.Response(200, text=tags_page(name, tags[name]))

    models = asyncio.run(ollama_library.fetch(httpx.AsyncClient(transport=httpx.MockTransport(site))))
    by = {m["name"]: [(s["label"], s.get("download_gb"), s.get("cloud")) for s in m["sizes"]] for m in models}
    assert len(models) == 5 and "pending" not in str(models)
    assert by["qwen3.8"] == [("27b", None, None)]  # taille de la carte : téléchargement estimé
    assert by["mixtral"] == [("8x7b", 26.0, None), ("8x22b", 80.0, None)]  # tailles de la carte, dans l'ordre
    assert by["nomic-embed-text"] == [("latest", 0.274, None)]  # « latest » d'abord ; q8_0 (quantification) écarté
    assert by["wizardlm"] == [("7b-q4_0", 3.8, None), ("13b-q4_0", 7.4, None)]  # seulement des variantes : Q4
    assert by["kimi-k3"] == [("cloud", 0, True)]
    sizes = {m["name"]: m["sizes"] for m in models}
    assert ollama_library.public_size("kimi-k3", sizes["kimi-k3"][0]) == {
        "tag": "kimi-k3:cloud",
        "params_b": 0,
        "download": 0,
        "cloud": True,
    }
    assert ollama_library.public_size("mixtral", sizes["mixtral"][0])["download"] == 26e9  # exact, pas estimé


def test_ollama_cloud_models_are_never_local():
    """Ils passent par l'Ollama de ce PC mais tournent chez Ollama : jamais en mode privé ni pour une clé « locale »."""
    from gateway.permissions import model_allowed
    from gateway.registry import ModelRegistry, is_ollama_cloud

    assert is_ollama_cloud("kimi-k3:cloud") and is_ollama_cloud("gpt-oss:120b-cloud")
    assert not is_ollama_cloud("qwen3:8b") and not is_ollama_cloud("cloudy")
    reg = ModelRegistry([])
    assert reg.get("ollama", "kimi-k3:cloud", "local").hosting == "cloud"
    assert reg.get("ollama", "qwen3:8b", "local").hosting == "local"
    assert not model_allowed({"hosting": "local"}, reg.get("ollama", "kimi-k3:cloud", "local"))


def test_the_list_is_cached_a_day_and_served_stale_when_offline():
    store = DocumentStore(":memory:")
    calls = []

    def page(request):
        calls.append(request.url)
        if request.url.path.endswith("/tags"):
            return httpx.Response(200, text=tags_page("cloud-only", [("cloud", "-")]))
        return httpx.Response(200, text=PAGE)

    client = httpx.AsyncClient(transport=httpx.MockTransport(page))
    first = asyncio.run(ollama_library.cached(store, client))
    assert len(calls) == 2 and first["stale"] is False and len(first["models"]) == 3  # liste + étiquettes du cloud
    second = asyncio.run(ollama_library.cached(store, client))
    assert len(calls) == 2 and second["models"] == first["models"]  # pas de nouvel appel dans la journée

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
