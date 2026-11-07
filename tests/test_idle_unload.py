"""Déchargement automatique : une IA locale de ce PC inutilisée depuis une minute (réglable) est déchargée de la
mémoire — modèles d'Ollama, même chargés par une autre application, et IA intégrées (Kokoro, Whisper)."""

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from gateway.config import ProviderConfig
from gateway.local_gpu import DEFAULT_KEEP_ALIVE, IDLE_GRACE, LocalGpu, default_keep_alive
from gateway.providers.local_tts import LocalTtsProvider
from gateway.providers.local_whisper import LocalWhisperProvider

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def one_minute(monkeypatch):
    monkeypatch.delenv("GATEWAY_KEEP_ALIVE", raising=False)  # réglage par défaut (les autres tests le coupent)


class Ollama:
    """/api/ps : les modèles chargés et leur échéance ; /api/generate keep_alive=0 : déchargé."""

    def __init__(self):
        self.loaded = {"gemma4:e4b": "T1", "qwen3:4b": "T1"}
        self.unloaded = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": m, "expires_at": e} for m, e in self.loaded.items()]})
        body = json.loads(request.content)
        if body.get("keep_alive") == 0:
            self.unloaded.append(body["model"])
            self.loaded.pop(body["model"], None)
        return httpx.Response(200, json={"done": True})


class Server:
    def __init__(self, name, url, transport):
        self.name, self.hosting, self.gate = name, "local", object()
        self.cfg = ProviderConfig(name, "openai", url, "local")
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))

    def _headers(self):
        return {}


def manager(*servers):
    lg = LocalGpu({s.name: s for s in servers})
    clock = [1000.0]
    lg.monotonic = lambda: clock[0]
    return lg, clock


def test_unused_models_are_unloaded_after_one_minute():
    up = Ollama()
    lg, clock = manager(Server("ollama", "http://127.0.0.1:11434/v1", up))
    assert DEFAULT_KEEP_ALIVE == 60 and lg.keep_alive() == 60  # une minute par défaut

    async def scenario():
        assert await lg.unload_idle() == []  # vus pour la première fois : le délai part d'ici
        clock[0] += 30
        up.loaded["qwen3:4b"] = "T2"  # utilisé entre-temps par une autre application (échéance repoussée)
        assert await lg.unload_idle() == []
        clock[0] += 60 + IDLE_GRACE - 30
        assert await lg.unload_idle() == ["ollama/gemma4:e4b"]  # une minute sans usage
        clock[0] += 30
        assert await lg.unload_idle() == ["ollama/qwen3:4b"]

    asyncio.run(scenario())
    assert up.unloaded == ["gemma4:e4b", "qwen3:4b"]


def test_settings_and_requests_in_progress_are_respected():
    up = Ollama()
    srv = Server("ollama", "http://127.0.0.1:11434/v1", up)
    lg, clock = manager(srv)

    class Gate:
        total = 1  # une requête en cours

    async def scenario():
        await lg.unload_idle()
        clock[0] += 3600
        lg.gates["ollama"] = Gate()
        assert await lg.unload_idle() == []  # jamais pendant une requête
        del lg.gates["ollama"]
        lg.config = {"keep_alive": -1}  # « toujours chargé »
        assert await lg.unload_idle() == []
        lg.config = {"keep_alive": 300}
        assert await lg.unload_idle() == []  # la requête en cours a compté comme un usage
        clock[0] += 300 + IDLE_GRACE
        lg._last_use[("ollama", "gemma4:e4b")] = clock[0] - 100  # servi par la gateway il y a 100 s
        assert await lg.unload_idle() == ["ollama/qwen3:4b"]

    asyncio.run(scenario())


def test_only_this_pc_is_touched():
    lan = Ollama()
    lg, clock = manager(Server("salon", "http://192.168.1.20:11434/v1", lan))

    async def scenario():
        await lg.unload_idle()
        clock[0] += 3600
        return await lg.unload_idle()

    assert asyncio.run(scenario()) == [] and lan.unloaded == []  # le PC du salon sert d'autres personnes


def test_builtin_voices_are_released_and_reloaded():
    loads = []
    tts = LocalTtsProvider(httpx.AsyncClient(), factory=lambda: loads.append(1) or object())
    whisper = LocalWhisperProvider(httpx.AsyncClient(), size="small", factory=lambda: object())
    assert not tts.release_if_idle(0)  # jamais chargée : rien à faire
    tts._load()
    whisper._load()
    assert not tts.release_if_idle(60)  # utilisée à l'instant
    tts._run_lock.acquire()
    tts._used = whisper._used = time.monotonic() - 120
    assert not tts.release_if_idle(60)  # synthèse en cours
    tts._run_lock.release()
    assert tts.release_if_idle(60) and tts._model is None
    assert whisper.release_if_idle(60) and whisper._model is None
    tts._load()
    assert loads == [1, 1]  # rechargée à la demande suivante

    lg, _ = manager()  # passage du déchargement automatique : l'IA intégrée inutilisée est libérée
    lg.providers[tts.name] = tts
    tts.gate = object()
    tts._used = time.monotonic() - 120
    assert asyncio.run(lg.unload_idle()) == [f"{tts.name}/kokoro"] and tts._model is None


def test_default_delay_can_be_set_in_env(monkeypatch):
    assert default_keep_alive() == 60
    for raw, want in (("300", 300), ("-1", -1), ("ollama", None), ("n'importe quoi", 60)):
        monkeypatch.setenv("GATEWAY_KEEP_ALIVE", raw)
        assert default_keep_alive() == want
    lg, clock = manager(Server("ollama", "http://127.0.0.1:11434/v1", Ollama()))
    monkeypatch.setenv("GATEWAY_KEEP_ALIVE", "ollama")  # réglage d'Ollama : pas de déchargement automatique
    clock[0] += 3600
    assert asyncio.run(lg.unload_idle()) == []


def test_the_loop_is_started_and_the_page_says_one_minute():
    main = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
    assert "local.idle_loop()" in main
    html = (ROOT / "gateway" / "web" / "admin.html").read_text(encoding="utf-8")
    assert '<option value="">1 minute (par défaut)</option>' in html
