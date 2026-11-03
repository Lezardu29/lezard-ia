"""Lecture vocale locale intégrée (19.4) : Kokoro dans la gateway, modèle simulé ; téléchargement vérifié."""

import hashlib
import math
import wave
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.providers import local_tts
from gateway.providers.local_tts import LocalTtsProvider, pcm16, to_wav
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer master"}


class FakeKokoro:
    def __init__(self):
        self.calls = []

    def get_voices(self):
        return ["af_heart", "ff_siwis", "am_michael"]

    def create(self, text, voice, speed, lang):
        self.calls.append((text, voice, speed, lang))
        rate = 24000
        return [0.5 * math.sin(2 * math.pi * 440 * i / rate) for i in range(rate // 10)], rate


def upstream(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "tts-1"}, {"id": "gpt-4o-mini"}]})
    if request.url.path.endswith("/audio/speech"):
        return httpx.Response(200, content=b"ID3" + b"\x00" * 100, headers={"content-type": "audio/mpeg"})
    return httpx.Response(404)


@pytest.fixture
def app(monkeypatch):
    fake = FakeKokoro()
    monkeypatch.setattr(local_tts, "enabled", lambda: True)
    monkeypatch.setattr(LocalTtsProvider, "_load", lambda self: fake)
    application = create_app(
        [ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="sk-test")],
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        plans=PlanResolver({"compte-0123456789abcdef": "free"}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    return application, fake


def test_builtin_kokoro_preferred_free_and_wav(app):
    application, fake = app
    with TestClient(application) as c:
        r = c.post("/v1/audio/speech", headers=MASTER, json={"input": "Bonjour", "language": "fr"})
        assert r.status_code == 200, r.text
        assert r.headers["x-gateway-provider"] == "tts-local" and r.headers["x-gateway-model"] == "kokoro"
        assert r.headers["content-type"] == "audio/wav" and float(r.headers["x-gateway-cost-eur"]) == 0
        with wave.open(BytesIO(r.content)) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (24000, 1, 2, 2400)
        assert fake.calls[-1] == ("Bonjour", "ff_siwis", 1.0, "fr-fr")
        # voix OpenAI envoyée par un SDK : voix par défaut de la langue ; voix anglaise : accent anglais
        c.post("/v1/audio/speech", headers=MASTER, json={"input": "Hi", "voice": "alloy", "language": "en"})
        assert fake.calls[-1][1:] == ("af_heart", 1.0, "en-us")
        c.post("/v1/audio/speech", headers=MASTER, json={"input": "Hi", "voice": "am_michael", "speed": 3})
        assert fake.calls[-1][1:] == ("am_michael", 2.0, "en-us")  # vitesse bornée à ce que Kokoro sait faire
        r = c.post("/v1/audio/speech", headers=MASTER, json={"input": "Salut", "voice": "inconnue"})
        assert r.status_code == 400 and "ff_siwis" in r.json()["error"]["message"]
        r = c.post("/v1/audio/speech", headers=MASTER, json={"input": "Salut", "response_format": "pcm"})
        assert r.headers["content-type"].startswith("audio/L16") and len(r.content) == 4800
        st = c.get("/v1/audio/status", headers=MASTER).json()["speech"]
        assert st["available"] and st["local"]
        # pas un modèle de chat
        r = c.post(
            "/v1/chat/completions",
            headers=MASTER,
            json={"model": "tts-local/kokoro", "messages": [{"role": "user", "content": "Salut"}]},
        )
        assert r.status_code == 400


def test_private_mode_uses_builtin(app):
    application, fake = app
    with TestClient(application) as c:
        r = c.post("/v1/audio/speech", headers={**MASTER, "X-Gateway-Private": "true"}, json={"input": "Secret"})
        assert r.status_code == 200 and r.headers["x-gateway-provider"] == "tts-local"


def test_builtin_goes_through_local_queue(app):
    """Kokoro intégré suit la file d'attente et le mode PC occupé (18.4), comme Ollama : un autre compte ne peut
    ni occuper le processeur du propriétaire, ni épuiser les threads partagés de la gateway."""
    application, fake = app
    account = {"Authorization": "Bearer compte-0123456789abcdef"}
    with TestClient(application) as c:
        gpu = c.get("/v1/admin/gpu", headers=MASTER).json()
        tts = next(s for s in gpu["servers"] if s["name"] == "tts-local")
        assert tts["this_pc"] and tts["busy_reserved"]
        assert c.post("/v1/audio/speech", headers=account, json={"input": "Salut"}).status_code == 200
        assert c.post("/v1/admin/gpu/busy", headers=MASTER, json={"on": True}).status_code == 200
        n = len(fake.calls)
        r = c.post("/v1/audio/speech", headers=account, json={"input": "Salut"})
        assert r.status_code == 503 and len(fake.calls) == n  # refusé avant d'occuper le processeur
        assert c.post("/v1/audio/speech", headers=MASTER, json={"input": "Salut"}).status_code == 200
        served = next(s for s in c.get("/v1/admin/gpu", headers=MASTER).json()["servers"] if s["name"] == "tts-local")
        assert served["served"] == 2


def test_power_throttling_opt_out_is_harmless():
    assert local_tts.no_power_throttling() in (True, False)  # Windows : True ; ailleurs : False, sans erreur


def test_pcm_conversion():
    assert pcm16([0.0, 1.0, -1.0, 2.0]) == b"\x00\x00\xff\x7f\x01\x80\xff\x7f"
    assert to_wav([0.0] * 10, 8000)[:4] == b"RIFF"


def test_enabled_requires_model_files(tmp_path, monkeypatch):
    monkeypatch.setenv("KOKORO_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_TTS", "true")
    assert local_tts.model_dir() == tmp_path and not local_tts.installed() and not local_tts.enabled()
    for name in local_tts.FILES:
        (tmp_path / name).write_bytes(b"x")
    assert local_tts.installed()
    monkeypatch.setenv("LOCAL_TTS", "false")
    assert not local_tts.enabled()


def test_download_verifies_checksums(tmp_path, monkeypatch):
    blobs = {"kokoro-v1.0.onnx": b"modele", "voices-v1.0.bin": b"voix"}
    monkeypatch.setattr(local_tts, "FILES", {k: hashlib.sha256(v).hexdigest() for k, v in blobs.items()})
    served = []

    def handler(request):
        name = request.url.path.rsplit("/", 1)[1]
        served.append(name)
        return httpx.Response(200, content=blobs[name])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        local_tts.download(tmp_path, client, out=lambda *_: None)
        assert {p.name for p in tmp_path.iterdir()} == set(blobs)
        local_tts.download(tmp_path, client, out=lambda *_: None)  # déjà là : rien n'est retéléchargé
    assert len(served) == 2
    blobs["voices-v1.0.bin"] = b"voix piegee"
    (tmp_path / "voices-v1.0.bin").unlink()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="SHA-256"):
            local_tts.download(tmp_path, client, out=lambda *_: None)
    assert not (tmp_path / "voices-v1.0.bin").exists() and not list(tmp_path.glob("*.part"))
