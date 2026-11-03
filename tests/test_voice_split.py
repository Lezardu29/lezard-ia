"""Voix répartie entre plusieurs serveurs locaux (comme le découpage des longs messages, 13.4) : dictée longue coupée
aux silences et transcrite en parallèle, lecture vocale envoyée au serveur le moins occupé."""

import asyncio
import math
import re
from array import array

import httpx
from fastapi.testclient import TestClient

from gateway import voice_split
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.registry import ModelInfo
from gateway.store import DocumentStore

from .test_transcription import wav

MASTER = {"Authorization": "Bearer master"}
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64


class Servers:
    """Deux serveurs locaux de voix (LocalAI, Speaches) et un cloud ; chaque morceau répond « partie N »."""

    def __init__(self, failing=()):
        self.calls, self.failing, self.active, self.peak = [], set(failing), 0, 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path.endswith("/models"):
            ids = {"localai": ["whisper-1", "kokoro"], "speaches": ["whisper-1", "kokoro"], "openai": ["whisper-1"]}
            return httpx.Response(200, json={"data": [{"id": i} for i in ids[host]]})
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.02)  # deux demandes à la fois : en parallèle seulement si chaque serveur en a une
            if path.endswith("/audio/speech"):
                self.calls.append((host, "speech"))
                return httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})
            part = re.search(rb'filename="partie-(\d+)\.wav"', request.content)
            self.calls.append((host, int(part.group(1)) if part else 0))
            if host in self.failing:
                return httpx.Response(500, json={"error": "panne"})
            return httpx.Response(
                200, json={"text": f" partie {part.group(1).decode() if part else 0} ", "duration": 30.0}
            )
        finally:
            self.active -= 1


def make(up, servers=("localai", "speaches")):
    configs = [ProviderConfig(n, "openai", f"http://{n}:8000/v1", "local") for n in servers]
    configs.append(ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="sk-test"))
    return create_app(
        configs,
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )


def dictate(c, audio, **fields):
    return c.post(
        "/v1/audio/transcriptions",
        headers=MASTER,
        files={"file": ("dictee.wav", audio, "audio/wav")},
        data={"model": "auto", "language": "fr", **fields},
    )


def speech_with_pauses(seconds: float, pauses: list[float], rate: int = 16000) -> array:
    """Voix simulée (sinusoïde forte) avec un silence de 0,4 s à chaque instant de `pauses`."""
    out = array("h")
    for i in range(int(seconds * rate)):
        t = i / rate
        quiet = any(p <= t < p + 0.4 for p in pauses)
        out.append(0 if quiet else int(12000 * math.sin(2 * math.pi * 220 * t)))
    return out


def test_cuts_land_in_the_pauses():
    samples = speech_with_pauses(100, [26.0, 61.5, 88.0])  # coupures visées vers 30, 61 et 91 s
    parts = voice_split.cuts(samples)
    assert parts[0][0] == 0 and parts[-1][1] == len(samples)
    assert all(a[1] == b[0] for a, b in zip(parts, parts[1:]))  # rien de perdu, rien en double
    cuts = [b / 16000 for _, b in parts[:-1]]
    assert len(cuts) == 2 and 26.0 <= cuts[0] <= 26.4 and 61.5 <= cuts[1] <= 61.9


def test_short_or_undecodable_audio_not_split(monkeypatch):
    assert voice_split.split_audio(wav(20), "wav") is None  # sous TRANSCRIPTION_SPLIT_SECONDS
    assert voice_split.split_audio(b"\x1aE\xdf\xa3" + b"\x00" * 4000, "webm") is None  # WebM illisible
    parts = voice_split.split_audio(wav(150), "wav")
    assert [round(s) for _, s in parts] == [30, 30, 30, 30, 30]
    assert all(audio[:4] == b"RIFF" for audio, _ in parts)
    assert voice_split._wav_pcm(wav(1, rate=8000)) is None  # pas du 16 kHz : laissé à PyAV
    monkeypatch.setenv("TRANSCRIPTION_SPLIT_SECONDS", "200")
    assert voice_split.split_audio(wav(150), "wav") is None


def test_long_dictation_split_between_local_servers_in_order():
    up = Servers()
    with TestClient(make(up)) as c:
        r = dictate(c, wav(150))
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["text"] == "partie 1 partie 2 partie 3 partie 4 partie 5"
        m = out["x_gateway"]
        assert m["split"]["parts"] == 5 and m["split"]["parallel"] is True
        assert sorted(m["split"]["workers"]) == ["localai/whisper-1", "speaches/whisper-1"]
        assert m["audio_seconds"] == 150.0 and m["cost_eur"] == 0 and m["hosting"] == "local"
        hosts = {h for h, _ in up.calls}
        assert hosts == {"localai", "speaches"} and up.peak == 2  # les deux à la fois, jamais le cloud
        assert sorted(p for _, p in up.calls) == [1, 2, 3, 4, 5]
        text = dictate(c, wav(150), response_format="text")
        assert text.text == "partie 1 partie 2 partie 3 partie 4 partie 5"


def test_failed_server_parts_taken_by_the_other():
    up = Servers(failing={"speaches"})
    with TestClient(make(up)) as c:
        r = dictate(c, wav(150))
        assert r.status_code == 200, r.text
        assert r.json()["text"] == "partie 1 partie 2 partie 3 partie 4 partie 5"
        assert r.json()["x_gateway"]["split"]["workers"] == ["localai/whisper-1"]
        assert [h for h, _ in up.calls].count("speaches") == 2  # deux échecs : ce worker s'arrête


def test_all_servers_failing_reports_the_split():
    up = Servers(failing={"localai", "speaches"})
    with TestClient(make(up)) as c:
        r = dictate(c, wav(150))
        assert r.status_code == 502 and "Audio découpé" in r.text


def test_one_local_server_or_short_audio_not_split():
    up = Servers()
    with TestClient(make(up, servers=("localai",))) as c:
        r = dictate(c, wav(150))
        assert r.status_code == 200 and "split" not in r.json()["x_gateway"] and len(up.calls) == 1
    up = Servers()
    with TestClient(make(up)) as c:
        r = dictate(c, wav(20))
        assert "split" not in r.json()["x_gateway"] and len(up.calls) == 1


def test_speech_pieces_go_to_the_least_busy_server():
    up = Servers()
    with TestClient(make(up)) as c:
        st = c.get("/v1/audio/status", headers=MASTER).json()
        assert st["parallel"] == 2 and st["speech"]["parallel"] == 2

        async def three():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url="http://t") as a:
                body = {"input": "Bonjour", "model": "auto"}
                rs = await asyncio.gather(*(a.post("/v1/audio/speech", headers=MASTER, json=body) for _ in range(2)))
                return [r.headers["x-gateway-provider"] for r in rs]

        assert sorted(c.portal.call(three)) == ["localai", "speaches"]  # un morceau chacun, en même temps
    assert not voice_split.IN_FLIGHT  # compteurs remis à zéro


def test_load_counts_local_servers_only():
    local = ModelInfo("kokoro", "localai", "local")
    cloud = ModelInfo("tts-1", "openai", "cloud")
    with voice_split.using("localai"), voice_split.using("openai"):
        assert voice_split.load(local) == 1 and voice_split.load(cloud) == 0
    assert voice_split.load(local) == 0
    assert voice_split.local_servers([local, cloud, local]) == [local]


def test_chat_reads_several_pieces_at_once_and_dictates_longer():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "gateway" / "web" / "static"
    read = (static / "read-aloud.js").read_text(encoding="utf-8")
    assert "st.parallel" in read and "prepare(i + ahead)" in read
    dictation = (static / "dictation.js").read_text(encoding="utf-8")
    assert "MAX_MS = 600_000" in dictation and "m.split?.parallel" in dictation
