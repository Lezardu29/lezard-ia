"""Dictée vocale (13.9) : POST /v1/audio/transcriptions routé comme le chat, local d'abord, cloud facturé."""

import struct

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore
from gateway.transcription import sniff, wav_seconds

MASTER, ACCOUNT = {"Authorization": "Bearer master"}, {"Authorization": "Bearer compte"}
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 4000  # en-tête EBML + contenu factice


def wav(seconds: float, rate: int = 16000) -> bytes:
    data = b"\x00\x00" * int(rate * seconds)
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)
        + fmt
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )


class Upstream:
    def __init__(self, duration=30.0, local_status=200, verbose=True):
        self.calls, self.duration, self.local_status, self.verbose = [], duration, local_status, verbose

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path.endswith("/models"):
            ids = {"localai": ["whisper-1", "llama3.1:8b"], "openai": ["gpt-4o-mini", "whisper-1"]}[host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        if path.endswith("/audio/transcriptions"):
            body = request.content
            fmt = next(
                (f for f in ("verbose_json", "text", "json") if b'name="response_format"\r\n\r\n' + f.encode() in body),
                "json",
            )
            self.calls.append((host, fmt, request.headers.get("authorization")))
            if host == "localai" and self.local_status != 200:
                return httpx.Response(self.local_status, json={"error": "panne"})
            if fmt == "verbose_json" and not self.verbose:
                return httpx.Response(400, json={"error": "verbose_json non pris en charge"})
            if fmt == "text":
                return httpx.Response(200, text="Bonjour tout le monde")
            out = {"text": "Bonjour tout le monde"}
            if fmt == "verbose_json":
                out["duration"] = self.duration
            return httpx.Response(200, json=out)
        if path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                    ]
                },
            )
        return httpx.Response(404)


def make(up, local=True, cloud=True):
    configs = []
    if local:
        configs.append(ProviderConfig("localai", "openai", "http://localai:8080/v1", "local"))
    if cloud:
        configs.append(ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="sk-test"))
    return create_app(
        configs,
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({"compte": "free"}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )


def post(c, headers, audio=WEBM, name="dictee.webm", ctype="audio/webm", **fields):
    return c.post(
        "/v1/audio/transcriptions",
        headers=headers,
        files={"file": (name, audio, ctype)},
        data={"model": "auto", **fields},
    )


def test_local_whisper_preferred_and_free():
    up = Upstream()
    with TestClient(make(up)) as c:
        r = post(c, ACCOUNT)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["text"] == "Bonjour tout le monde" and set(out) == {"text", "x_gateway"}  # format json
        m = out["x_gateway"]
        assert m["provider"] == "localai" and m["hosting"] == "local" and m["cost_eur"] == 0
        assert m["audio_seconds"] == 30.0 and "usage_estimated" not in m
        assert [h for h, _, _ in up.calls] == ["localai"] and up.calls[0][1] == "verbose_json"
        assert r.headers["x-gateway-provider"] == "localai"
        # les SDK OpenAI envoient « whisper-1 » : même routage
        assert post(c, ACCOUNT, model="whisper-1").json()["x_gateway"]["provider"] == "localai"


def test_cloud_billed_and_counted_in_key_budget():
    up = Upstream(duration=1800.0)  # 30 min × 0,006 $ = 0,18 $
    with TestClient(make(up, local=False)) as c:
        k = c.post(
            "/v1/keys",
            headers=ACCOUNT,
            json={"name": "dictée", "limits": {"budget": {"amount": 0.1, "currency": "EUR", "period": "month"}}},
        ).json()
        h = {"Authorization": f"Bearer {k['key']}"}
        r = post(c, h)
        assert r.status_code == 200, r.text
        m = r.json()["x_gateway"]
        assert m["provider"] == "openai" and m["cost_usd"] == pytest.approx(0.18)
        assert up.calls[0][2] == "Bearer sk-test"
        spent = c.get("/v1/keys", headers=ACCOUNT, params={"with_usage": "true"}).json()["data"][0]["usage"]
        assert spent["budgets"][0]["spent"] > 0.1  # 0,18 $ comptés dans le budget de la clé
        r = post(c, h)  # budget dépassé : refus avant l'appel
        assert r.status_code == 429 and "Budget" in r.text and len(up.calls) == 1


def test_cloud_fallback_when_local_server_fails():
    up = Upstream(local_status=500)
    with TestClient(make(up)) as c:
        r = post(c, ACCOUNT)
        assert r.status_code == 200, r.text
        m = r.json()["x_gateway"]
        assert m["provider"] == "openai" and m["fallback"]["attempts"][0]["ref"] == "localai/whisper-1"


def test_server_without_verbose_json_and_text_format():
    up = Upstream(verbose=False)
    with TestClient(make(up, cloud=False)) as c:
        r = post(c, ACCOUNT)
        assert r.status_code == 200 and r.json()["x_gateway"]["usage_estimated"] is True
        assert [f for _, f, _ in up.calls] == ["verbose_json", "json"]  # reprise au format demandé
        r = post(c, ACCOUNT, response_format="text")
        assert r.status_code == 200 and r.text == "Bonjour tout le monde"
        assert r.headers["content-type"].startswith("text/plain")


def test_too_big_too_long_and_invalid_type(monkeypatch):
    up = Upstream()
    with TestClient(make(up)) as c:
        assert post(c, ACCOUNT, audio=b"Ceci n'est pas un fichier audio.").status_code == 400
        assert post(c, ACCOUNT, response_format="docx").status_code == 400
        assert c.post("/v1/audio/transcriptions", headers=ACCOUNT, json={"file": "x"}).status_code == 400
        monkeypatch.setenv("TRANSCRIPTION_MAX_BYTES", "2000")
        r = post(c, ACCOUNT, audio=WEBM)  # 4 Ko : sous la marge multipart, refusé à la lecture
        assert r.status_code == 413
        r = post(c, ACCOUNT, audio=b"\x1aE\xdf\xa3" + b"\x00" * 100_000)  # au-delà de la marge multipart
        assert r.status_code == 413
        monkeypatch.delenv("TRANSCRIPTION_MAX_BYTES")
        monkeypatch.setenv("TRANSCRIPTION_MAX_SECONDS", "1")
        r = post(c, ACCOUNT, audio=wav(2), name="a.wav", ctype="audio/wav")
        assert r.status_code == 413 and "trop long" in r.text
        assert up.calls == []  # rien n'est parti vers un serveur


def test_key_permissions_apply():
    up = Upstream()
    with TestClient(make(up, local=False)) as c:
        k = c.post("/v1/keys", headers=ACCOUNT, json={"name": "local", "permissions": {"hosting": "local"}}).json()
        r = post(c, {"Authorization": f"Bearer {k['key']}"})
        assert r.status_code == 503 and "aucun autorisé" in r.text and up.calls == []


def test_status_and_page_permissions_policy():
    with TestClient(make(Upstream())) as c:
        st = c.get("/v1/audio/status", headers=ACCOUNT).json()
        assert st.pop("speech")["available"] is False  # pas de serveur de synthèse vocale ici (19.4)
        assert st == {
            "available": True,
            "local": True,
            "parallel": 1,
            "max_bytes": 25 * 1024 * 1024,
        }
        assert "microphone=(self)" in c.get("/app").headers["permissions-policy"]
    with TestClient(make(Upstream(), local=False, cloud=False)) as c:
        assert c.get("/v1/audio/status", headers=ACCOUNT).json()["available"] is False


def test_audio_never_spooled_to_disk(monkeypatch):
    import starlette.formparsers as fp

    created = []
    real = fp.SpooledTemporaryFile

    def spy(*a, **kw):
        f = real(*a, **kw)
        created.append(f)
        return f

    monkeypatch.setattr(fp, "SpooledTemporaryFile", spy)
    with TestClient(make(Upstream())) as c:
        r = post(c, ACCOUNT, audio=b"\x1aE\xdf\xa3" + b"\x00" * (3 * 1024 * 1024))  # > 1 Mo, seuil de Starlette
        assert r.status_code == 200
    assert created and not any(f._rolled for f in created)


def test_chat_page_dictation_inserts_without_browser_speech_api():
    with TestClient(make(Upstream())) as c:
        html = c.get("/app?frame=1").text
        assert 'id="mic"' in html and 'id="dictation"' in html
        js = c.get("/static/dictation.js").text
        assert "/v1/audio/transcriptions" in js and "isSecureContext" in js and "MediaRecorder" in js
        assert "SpeechRecognition" not in js  # pas d'envoi de l'audio à Google (Chrome)
        assert "requestSubmit" not in js and ".submit(" not in js  # insère dans le champ, n'envoie jamais
        assert "MAX_MS = 600_000" in js and '"Escape"' in js


def test_sniff_and_wav_duration():
    assert sniff(WEBM) == "webm" and sniff(b"OggS....") == "ogg" and sniff(wav(0.1)) == "wav"
    assert sniff(b"\x00\x00\x00\x18ftypM4A ") == "mp4" and sniff(b"ID3\x03") == "mp3" and sniff(b"<html>") is None
    assert wav_seconds(wav(1.5)) == pytest.approx(1.5)
