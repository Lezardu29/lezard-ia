"""Lecture vocale (19.4) : POST /v1/audio/speech routé comme la dictée, local d'abord, cloud facturé au caractère."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.speech import is_tts
from gateway.store import DocumentStore

MASTER, ACCOUNT = {"Authorization": "Bearer master"}, {"Authorization": "Bearer compte"}
MP3 = b"ID3\x04\x00" + b"\x00" * 2000


class Upstream:
    def __init__(self, local_status=200, local_type="audio/mpeg", local_body=MP3):
        self.calls = []
        self.local_status, self.local_type, self.local_body = local_status, local_type, local_body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path.endswith("/models"):
            ids = {
                "speaches": ["speaches-ai/Kokoro-82M-v1.0-ONNX", "Systran/faster-whisper-small", "llama3.1:8b"],
                "openai": ["gpt-4o-mini", "tts-1", "whisper-1", "tts-maison"],
            }[host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        if path.endswith("/audio/speech"):
            body = json.loads(request.content)
            self.calls.append((host, body, request.headers.get("authorization")))
            if host == "speaches":
                if self.local_status != 200:
                    return httpx.Response(self.local_status, json={"error": "panne"})
                return httpx.Response(200, content=self.local_body, headers={"content-type": self.local_type})
            return httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})
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


def make(up, local=True, cloud=True, keys=None):
    configs = []
    if local:
        configs.append(ProviderConfig("speaches", "openai", "http://speaches:8000/v1", "local"))
    if cloud:
        configs.append(ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="sk-test"))
    return create_app(
        configs,
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver(keys or {"compte": "free"}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )


def say(c, headers=MASTER, **body):
    return c.post("/v1/audio/speech", headers=headers, json={"input": "Bonjour tout le monde", **body})


def test_is_tts():
    assert is_tts("tts-1") and is_tts("gpt-4o-mini-tts") and is_tts("speaches-ai/Kokoro-82M-v1.0-ONNX")
    assert is_tts("speaches-ai/piper-fr_FR-siwis-medium") and not is_tts("whisper-1")
    assert not is_tts("llama3.1:8b") and not is_tts("Systran/faster-whisper-small")


def test_local_first_free_with_default_voice():
    up = Upstream()
    with TestClient(make(up)) as c:
        r = say(c, language="fr")
        assert r.status_code == 200 and r.content == MP3 and r.headers["content-type"] == "audio/mpeg"
        assert r.headers["x-gateway-provider"] == "speaches" and r.headers["x-gateway-hosting"] == "local"
        assert float(r.headers["x-gateway-cost-eur"]) == 0 and r.headers["cache-control"] == "no-store"
        host, body, _ = up.calls[-1]
        assert host == "speaches" and body == {
            "model": "speaches-ai/Kokoro-82M-v1.0-ONNX",
            "input": "Bonjour tout le monde",
            "voice": "ff_siwis",
            "response_format": "mp3",
        }
        say(c, language="en", voice="af_bella", speed=1.25, response_format="wav")
        _, body, _ = up.calls[-1]
        assert body["voice"] == "af_bella" and body["speed"] == 1.25 and body["response_format"] == "wav"


def test_cloud_fallback_billed_by_character():
    up = Upstream(local_status=503)
    with TestClient(make(up)) as c:
        r = say(c, input="x" * 1000)
        assert r.status_code == 200 and r.headers["x-gateway-provider"] == "openai"
        assert [h for h, _, _ in up.calls] == ["speaches", "openai"]
        assert up.calls[-1][1]["model"] == "tts-1" and up.calls[-1][1]["voice"] == "alloy"
        assert up.calls[-1][2] == "Bearer sk-test"
        assert float(r.headers["x-gateway-cost-eur"]) > 0  # 1000 caractères × 15 $ / million


def test_cloud_only_and_explicit_model():
    up = Upstream()
    with TestClient(make(up, local=False)) as c:
        assert say(c).headers["x-gateway-provider"] == "openai"
        assert say(c, model="openai/tts-1").status_code == 200
        r = say(c, model="openai/gpt-4o-mini")
        assert r.status_code == 400 and "synthèse vocale" in r.json()["error"]["message"]


def test_private_mode_stays_local():
    up = Upstream()
    with TestClient(make(up, local=False)) as c:
        r = c.post(
            "/v1/audio/speech", headers={**MASTER, "X-Gateway-Private": "true"}, json={"input": "Secret médical"}
        )
        assert r.status_code == 503 and "Mode privé" in r.json()["error"]["message"]
        assert up.calls == []  # rien n'est parti au cloud
    up = Upstream()
    with TestClient(make(up)) as c:
        r = c.post("/v1/audio/speech", headers={**MASTER, "X-Gateway-Private": "true"}, json={"input": "Secret"})
        assert r.status_code == 200 and r.headers["x-gateway-provider"] == "speaches"


def test_no_server():
    with TestClient(make(Upstream(), local=False, cloud=False)) as c:
        r = say(c)
        assert r.status_code == 503 and "Aucun serveur de synthèse vocale" in r.json()["error"]["message"]
        st = c.get("/v1/audio/status", headers=MASTER).json()
        assert st["speech"]["available"] is False and st["speech"]["max_chars"] == 4096


def test_status_reports_speech():
    with TestClient(make(Upstream())) as c:
        st = c.get("/v1/audio/status", headers=MASTER).json()
        assert st["speech"] == {"available": True, "local": True, "parallel": 1, "max_chars": 4096}


@pytest.mark.parametrize(
    "body,status",
    [
        ({"input": ""}, 400),
        ({"input": "\x00\x01"}, 400),
        ({"input": 12}, 400),
        ({"input": "x" * 4097}, 413),
        ({"input": "a", "response_format": "ogg"}, 400),
        ({"input": "a", "voice": "../../etc"}, 400),
        ({"input": "a", "speed": 9}, 400),
        ({"input": "a", "speed": True}, 400),
        ({"input": "a", "instructions": "x" * 2001}, 400),
        ({"input": "a", "language": "français"}, 400),
        ({"input": "a", "stream_format": "sse"}, 400),
    ],
)
def test_validation(body, status):
    up = Upstream()
    with TestClient(make(up)) as c:
        r = c.post("/v1/audio/speech", headers=MASTER, json=body)
        assert r.status_code == status, r.text
        assert up.calls == []


@pytest.mark.parametrize("ctype", ["application/json", "text/html; charset=utf-8", "text/plain"])
def test_non_audio_upstream_rejected(ctype):
    up = Upstream(local_type=ctype, local_body=b'{"error": "<script>"}')
    with TestClient(make(up, cloud=False)) as c:
        r = say(c)
        assert r.status_code == 502 and "pas de l'audio" in r.json()["error"]["message"]


def test_empty_and_oversized_audio_rejected(monkeypatch):
    with TestClient(make(Upstream(local_body=b""), cloud=False)) as c:
        assert say(c).status_code == 502
    monkeypatch.setenv("SPEECH_MAX_RESPONSE_BYTES", "100")
    with TestClient(make(Upstream(), cloud=False)) as c:
        r = say(c)
        assert r.status_code == 502 and "trop volumineux" in r.json()["error"]["message"]


def test_control_characters_stripped_and_tts_not_used_for_chat():
    up = Upstream()
    with TestClient(make(up, cloud=False)) as c:
        say(c, input="Bon\x07jour\x00 !")
        assert up.calls[-1][1]["input"] == "Bonjour !"
        models = [m["id"] for m in c.get("/v1/models", headers=MASTER).json()["data"]]
        r = c.post(
            "/v1/chat/completions",
            headers=MASTER,
            json={"model": "auto", "messages": [{"role": "user", "content": "Salut"}]},
        )
        used = r.json()["x_gateway"]["model"]
        assert r.status_code == 200 and used == "llama3.1:8b", (models, used)


def test_budget_key_refuses_unknown_price():
    up = Upstream()
    with TestClient(make(up, local=False)) as c:
        limits = {"budget": {"amount": 1, "currency": "EUR", "period": "month"}}
        key = c.post("/v1/keys", headers=MASTER, json={"name": "budget", "limits": limits})
        assert key.status_code in (200, 201), key.text
        auth = {"Authorization": f"Bearer {key.json()['key']}"}
        r = say(c, headers=auth, model="openai/tts-maison")
        assert r.status_code == 403 and "sans tarif connu" in r.json()["error"]["message"]
        r = say(c, headers=auth)  # automatique : seulement les modèles au tarif connu
        assert r.status_code == 200 and up.calls[-1][1]["model"] == "tts-1"
        r = say(c, model="openai/tts-maison")  # clé maître sans budget : autorisé, prix inconnu signalé
        assert r.status_code == 200


def test_declared_type_follows_received_audio():
    wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 40
    with TestClient(make(Upstream(local_body=wav, local_type="audio/mpeg"), cloud=False)) as c:
        r = say(c)  # « mp3 » demandé, WAV reçu : le navigateur doit savoir ce qu'il lit
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    pcm = b"\x01\x02" * 500
    with TestClient(make(Upstream(local_body=pcm, local_type="application/octet-stream"), cloud=False)) as c:
        r = say(c, response_format="pcm")
        assert r.headers["content-type"].startswith("audio/L16")


def test_csp_allows_blob_audio_only():
    with TestClient(make(Upstream())) as c:
        csp = c.get("/app").headers["content-security-policy"]
        assert "media-src 'self' blob:" in csp and "data:" not in csp.split("media-src")[1].split(";")[0]
