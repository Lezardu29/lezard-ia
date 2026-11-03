"""Catégories « Dictée » et « Lecture vocale » : comme les embeddings, le meilleur modèle de voix pour ce PC (qualité
en français, place en mémoire), fixable à la main ; taille du Whisper intégré choisie selon le matériel."""

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import core
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.providers import local_whisper
from gateway.registry import ModelInfo
from gateway.store import DocumentStore
from gateway.voice_choice import VoiceChoice

from .test_transcription import wav

GB = 1024**3
MASTER_KEY = "master-0123456789abcdef0123456789"
MASTER, ACCOUNT = {"Authorization": f"Bearer {MASTER_KEY}"}, {"Authorization": "Bearer compte-0123456789abcdef"}
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
GPU = {"name": "RTX 5070", "vram_total": 12 * GB, "vram_used": 1 * GB}


def scanner(gpus):
    class FakeScanner:
        async def snapshot(self, providers, client):
            return {"gpus": gpus, "ram": {"available": 16 * GB}, "disk": {}, "models_vram": 0}

    return FakeScanner


class Upstream:
    """Ce PC (Speaches : large-v3 et Kokoro) et un autre PC du réseau (small et Piper)."""

    def __init__(self):
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path.endswith("/models"):
            ids = {
                "localhost": ["Systran/faster-whisper-large-v3", "speaches-ai/Kokoro-82M-v1.0-ONNX"],
                "pc-salon": ["Systran/faster-whisper-small", "rhasspy/piper-fr_FR-siwis"],
            }[host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        self.calls.append(host)
        if path.endswith("/audio/speech"):
            return httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})
        return httpx.Response(200, json={"text": f"via {host}", "duration": 2.0})


def make(monkeypatch, gpus, store=None):
    monkeypatch.setattr(core, "HardwareScanner", scanner(gpus))
    up = Upstream()
    app = create_app(
        [
            ProviderConfig("speaches", "openai", "http://localhost:8000/v1", "local"),
            ProviderConfig("salon", "openai", "http://pc-salon:8000/v1", "local"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({"compte-0123456789abcdef": "free"}, "free", master_key=MASTER_KEY),
        store=store or DocumentStore(":memory:"),
    )
    return app, up


def dictate(c):
    return c.post(
        "/v1/audio/transcriptions",
        headers=MASTER,
        files={"file": ("d.wav", wav(2), "audio/wav")},
        data={"model": "auto"},
    )


def test_profiles_in_french():
    stt = VoiceChoice("stt")
    p = lambda mid, hosting="local": stt.profile_of(ModelInfo(mid, "x", hosting))
    assert p("Systran/faster-whisper-large-v3")["label"] == "Whisper large-v3"
    assert p("deepdml/faster-whisper-large-v3-turbo-ct2")["label"] == "Whisper large-v3-turbo"
    assert p("whisper-small")["quality"] < p("whisper-medium")["quality"] < p("whisper-large-v3")["quality"]
    assert p("Systran/faster-whisper-medium.en")["multilingual"] is False  # anglais seulement : pénalisé
    assert p("nvidia/parakeet-tdt-0.6b-v3")["multilingual"] is True
    assert p("whisper-1")["known"] is False  # alias d'un serveur local : taille inconnue
    assert p("whisper-1", "cloud")["label"] == "Whisper (whisper-1)"
    tts = VoiceChoice("tts")
    assert tts.profile_of(ModelInfo("tts-1-hd", "openai", "cloud"))["label"] == "OpenAI TTS HD"
    assert tts.profile_of(ModelInfo("kyutai/tts-1.6b-en_fr", "x", "local"))["label"] == "Kyutai TTS 1.6B"
    assert tts.profile_of(ModelInfo("canopylabs/orpheus-3b", "x", "local"))["multilingual"] is False


def test_best_model_for_this_pc_and_pinned(monkeypatch):
    app, up = make(monkeypatch, [GPU])
    with TestClient(app) as c:
        assert c.get("/v1/admin/voice", headers=ACCOUNT).status_code == 403
        st = c.get("/v1/admin/voice", headers=MASTER).json()
        assert st["gpu"] == "RTX 5070" and st["whisper"] is None
        stt = st["stt"]
        assert stt["mode"] == "auto" and stt["chosen"] == "speaches/Systran/faster-whisper-large-v3"
        best, other = stt["models"]
        assert best["fit"] == "vram" and best["this_pc"] and best["chosen"]
        assert other["fit"] == "unknown" and not other["this_pc"]  # autre PC : matériel inconnu, pas de malus
        assert st["tts"]["chosen"] == "speaches/speaches-ai/Kokoro-82M-v1.0-ONNX"
        assert dictate(c).json()["text"] == "via localhost"
        # fixé à la main : la dictée part sur l'autre PC, le reste devient secours
        st = c.put(
            "/v1/admin/voice", headers=MASTER, json={"kind": "stt", "model": "salon/Systran/faster-whisper-small"}
        ).json()
        assert st["stt"]["mode"] == "pinned" and st["stt"]["chosen"] == "salon/Systran/faster-whisper-small"
        assert dictate(c).json()["text"] == "via pc-salon"
        c.put("/v1/admin/voice", headers=MASTER, json={"kind": "tts", "model": "salon/rhasspy/piper-fr_FR-siwis"})
        c.post("/v1/audio/speech", headers=MASTER, json={"input": "Bonjour"})
        assert up.calls[-1] == "pc-salon"
        st = c.put("/v1/admin/voice", headers=MASTER, json={"kind": "stt", "model": None}).json()
        assert st["stt"]["mode"] == "auto" and dictate(c).json()["text"] == "via localhost"
        assert c.put("/v1/admin/voice", headers=MASTER, json={"kind": "stt", "model": "salon/nope"}).status_code == 404
        assert c.put("/v1/admin/voice", headers=MASTER, json={"kind": "chat", "model": None}).status_code == 400
        audit = c.get("/v1/admin/audit", headers=MASTER).json()
        assert any(e["action"] == "voice.choice" for e in audit["data"])


def test_pinned_model_gone_falls_back_to_best(monkeypatch):
    store = DocumentStore(":memory:")
    store.set_setting("voice_choice_stt", {"model": "ancien/whisper-large"})  # fixé, puis serveur retiré
    app, _ = make(monkeypatch, [GPU], store)
    with TestClient(app) as c:
        st = c.get("/v1/admin/voice", headers=MASTER).json()["stt"]
        assert st["pinned_missing"] and st["chosen"] == "speaches/Systran/faster-whisper-large-v3"


def test_without_gpu_big_model_on_this_pc_loses(monkeypatch):
    app, _ = make(monkeypatch, [])  # pas de carte graphique : large-v3 passerait par la RAM, bien plus lent
    with TestClient(app) as c:
        stt = c.get("/v1/admin/voice", headers=MASTER).json()["stt"]
        assert stt["chosen"] == "salon/Systran/faster-whisper-small"
        assert stt["models"][1]["fit"] == "offload"


def test_builtin_whisper_runs_where_it_was_loaded():
    choice = VoiceChoice("stt")
    builtin = ModelInfo("whisper-large-v3-turbo", local_whisper.NAME, "local")

    class Loaded:
        device = "cpu"

    choice.builtin = Loaded()
    a = choice.assess(builtin)
    assert a["fit"] == "cpu" and a["score"] == round(a["quality"] - 0.3, 3)  # gros modèle sur le processeur
    assert choice.assess(ModelInfo("whisper-small", local_whisper.NAME, "local"))["score"] == 0.78
    Loaded.device = "cuda"
    assert choice.assess(builtin)["fit"] == "vram"


@pytest.mark.parametrize(
    "gpus,cores,ram,cuda,expected",
    [
        ([GPU], 12, 32 * GB, True, ("large-v3-turbo", "cuda")),
        ([GPU], 12, 32 * GB, False, ("small", "cpu")),  # bibliothèques CUDA absentes
        ([{"name": "GT 710", "vram_total": 2 * GB}], 8, 16 * GB, True, ("small", "cpu")),  # trop peu de VRAM
        ([], 2, 4 * GB, False, ("base", "cpu")),  # petit PC
    ],
)
def test_builtin_whisper_size_for_this_pc(gpus, cores, ram, cuda, expected):
    size, device, why = local_whisper.auto_plan(gpus, cores, ram, cuda)
    assert (size, device) == expected and why
    if gpus and not cuda and gpus[0]["vram_total"] >= 4 * GB:
        assert "CUDA" in why and "RTX 5070" in why


def test_whisper_model_auto_explicit_and_gpu_fallback(monkeypatch):
    monkeypatch.setattr(local_whisper, "_plan_for_this_pc", lambda device: ("large-v3-turbo", "cuda", "carte"))
    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    w = local_whisper.LocalWhisperProvider(httpx.AsyncClient())
    assert (w.model_id, w.device, w.auto) == ("whisper-large-v3-turbo", "cuda", True)
    loaded = []

    class FakeModel:
        def __init__(self, size, device, compute_type):
            if device == "cuda":
                raise RuntimeError("cudnn64_9.dll introuvable")
            loaded.append((size, device, compute_type))

    faster_whisper = pytest.importorskip("faster_whisper")  # composant « voix », facultatif
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    w._load()
    assert loaded == [("small", "cpu", "int8")] and w.device == "cpu" and "cudnn" in w.reason
    monkeypatch.setenv("WHISPER_MODEL", "medium")
    w = local_whisper.LocalWhisperProvider(httpx.AsyncClient())
    assert (w.model_id, w.device, w.auto) == ("whisper-medium", "cpu", False)


def test_cuda_ready(monkeypatch):
    ctranslate2 = pytest.importorskip("ctranslate2")  # composant « voix », facultatif

    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 0)
    assert local_whisper.cuda_ready() is False
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)

    def missing(name):
        raise OSError(name)

    monkeypatch.setattr(local_whisper.ctypes, "CDLL", missing)
    assert local_whisper.cuda_ready() is False
    monkeypatch.setattr(local_whisper.ctypes, "CDLL", lambda name: object())
    assert local_whisper.cuda_ready() is True


# ---------- voix de lecture (Kokoro) ----------
class Kokoro:
    STYLES = {"ff_siwis": 1.0, "af_heart": 10.0, "am_michael": 100.0}

    def __init__(self):
        self.calls = []

    def get_voices(self):
        return list(self.STYLES)

    def get_voice_style(self, name):
        return self.STYLES[name]

    def create(self, text, voice, speed, lang, **options):
        self.calls.append({"voice": voice, "speed": speed, "lang": lang, **options})
        return [0.0] * 240, 24000


@pytest.fixture
def kokoro_app(monkeypatch):
    from gateway.providers import local_tts

    fake = Kokoro()
    monkeypatch.setattr(local_tts, "enabled", lambda: True)
    monkeypatch.setattr(local_tts.LocalTtsProvider, "_load", lambda self: fake)
    app = create_app(
        [],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )
    return app, fake


def read(c, **body):
    return c.post("/v1/audio/speech", headers=MASTER, json={"input": "Bonjour", "language": "fr", **body})


def test_reading_voice_accent_blend_speed_and_pauses(kokoro_app):
    app, fake = kokoro_app
    with TestClient(app) as c:
        st = c.get("/v1/admin/voice", headers=MASTER).json()["reading"]
        assert st["voice"] is None and st["builtin"] and st["kokoro"]
        assert {"id": "ff_siwis", "label": "Siwis (femme)", "language": "français"} in st["voices"]
        read(c)
        assert fake.calls[-1] == {"voice": "ff_siwis", "speed": 1.0, "lang": "fr-fr"}  # par défaut : rien ne change
        body = {"voice": "am_michael", "speed": 1.25, "pause": 0.6, "blend_voice": "ff_siwis", "blend_share": 0.3}
        r = c.put("/v1/admin/voice/reading", headers=MASTER, json=body)
        assert r.status_code == 200, r.text
        assert r.json()["reading"]["voice"] == "am_michael" and r.json()["reading"]["pronunciation"] == "text"
        read(c)
        call = fake.calls[-1]
        # voix anglaise, texte français prononcé en français : l'accent anglais ; 70 % Michael + 30 % Siwis
        assert call["lang"] == "fr-fr" and call["speed"] == 1.25 and call["sentence_pause"] == 0.6
        assert call["voice"] == pytest.approx(100.0 * 0.7 + 1.0 * 0.3)
        c.put("/v1/admin/voice/reading", headers=MASTER, json={"voice": "am_michael", "pronunciation": "voice"})
        read(c)
        assert fake.calls[-1] == {"voice": "am_michael", "speed": 1.0, "lang": "en-us"}  # langue de la voix
        read(c, voice="af_heart", speed=0.8)  # voix et vitesse demandées par le client : les réglages ne jouent pas
        assert fake.calls[-1] == {"voice": "af_heart", "speed": 0.8, "lang": "en-us"}
        assert c.put("/v1/admin/voice/reading", headers=MASTER, json={}).json()["reading"]["voice"] is None
        read(c)
        assert fake.calls[-1] == {"voice": "ff_siwis", "speed": 1.0, "lang": "fr-fr"}
        audit = c.get("/v1/admin/audit", headers=MASTER).json()
        assert any(e["action"] == "voice.reading" for e in audit["data"])


@pytest.mark.parametrize(
    "body",
    [
        {"voice": "inconnue"},
        {"blend_voice": "../x"},
        {"speed": 3},
        {"speed": True},
        {"pause": 2},
        {"blend_share": 0},
        {"pronunciation": "accent"},
        {"couleur": "bleu"},
    ],
)
def test_reading_settings_checked(kokoro_app, body):
    app, _ = kokoro_app
    with TestClient(app) as c:
        assert c.put("/v1/admin/voice/reading", headers=MASTER, json=body).status_code == 400
        assert c.put("/v1/admin/voice/reading", headers=ACCOUNT, json={}).status_code in (401, 403)


def test_reading_on_other_servers_only_speed():
    from gateway.voice_choice import check_reading, reading_payload

    reading = check_reading({"voice": "ff_siwis", "speed": 1.5, "pause": 0.5, "blend_voice": "ff_siwis"}, ["ff_siwis"])
    assert reading["blend_voice"] is None  # mélangée avec elle-même : rien à mélanger
    assert reading_payload(reading, "ff_siwis", "fr", builtin=False) == {"speed": 1.5}
    assert reading_payload(reading, "ff_siwis", "fr", builtin=True) == {"speed": 1.5, "lang": "fr-fr", "pause": 0.5}
    assert reading_payload(reading, "ff_siwis", "", builtin=True) == {"speed": 1.5, "pause": 0.5}  # langue inconnue


def test_builtin_kokoro_preferred_when_its_settings_are_used(monkeypatch):
    from gateway.providers import local_tts

    fake = Kokoro()
    monkeypatch.setattr(local_tts, "enabled", lambda: True)
    monkeypatch.setattr(local_tts.LocalTtsProvider, "_load", lambda self: fake)

    def speaches(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "speaches-ai/Kokoro-82M-v1.0-ONNX"}]})
        return httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})

    app = create_app(
        [ProviderConfig("aaa-speaches", "openai", "http://localhost:8000/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(speaches)),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        assert read(c).headers["x-gateway-provider"] == "aaa-speaches"  # même qualité : l'ordre habituel
        st = c.put("/v1/admin/voice/reading", headers=MASTER, json={"voice": "am_michael"}).json()
        assert st["tts"]["chosen"] == "tts-local/kokoro"
        assert read(c).headers["x-gateway-provider"] == "tts-local" and fake.calls[-1]["lang"] == "fr-fr"
        c.put("/v1/admin/voice/reading", headers=MASTER, json={"speed": 1.2})  # la vitesse marche partout
        assert read(c).headers["x-gateway-provider"] == "aaa-speaches"
