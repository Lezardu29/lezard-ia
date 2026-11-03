"""Taille maximale des corps de requête (9.1)."""

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore


class Upstream:
    calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
        self.calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("GATEWAY_MAX_BODY_BYTES", "2000")
    monkeypatch.setenv("DOCUMENT_MAX_BYTES", "3000")  # upload : 3000 × 4/3 + 1 Mo
    up = Upstream()
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        yield c, up


def chat(text: str) -> dict:
    return {"model": "ollama/llama3.1:8b", "messages": [{"role": "user", "content": text}]}


def test_small_request_passes(api):
    c, up = api
    assert c.post("/v1/chat/completions", json=chat("Bonjour")).status_code == 200 and up.calls == 1


def test_declared_length_rejected_without_reading(api):
    c, up = api
    r = c.post("/v1/chat/completions", json=chat("x" * 5000))
    assert r.status_code == 413 and "2000 octets" in r.json()["error"]["message"] and up.calls == 0


def test_chunked_body_without_length_rejected(api):
    """Sans Content-Length, les octets sont comptés à la lecture : impossible de contourner la limite."""
    c, up = api
    payload = json.dumps(chat("x" * 5000)).encode()

    def chunks():
        for i in range(0, len(payload), 500):
            yield payload[i : i + 500]

    r = c.post("/v1/chat/completions", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and up.calls == 0


def test_invalid_content_length(api):
    c, _ = api
    r = c.post(
        "/v1/chat/completions", content=b"{}", headers={"Content-Length": "abc", "Content-Type": "application/json"}
    )
    assert r.status_code == 400


def test_document_upload_has_its_own_limit(api):
    c, _ = api
    # 2 900 octets en base64 (~3 900 octets JSON) : au-delà de la limite générale, accepté pour l'upload
    data = base64.b64encode(b"Une ligne de texte.\n" * 145).decode()
    r = c.post("/v1/documents", json={"filename": "notes.txt", "content_base64": data})
    assert r.status_code == 201, r.text
    # fichier décodé trop gros : refusé par le contrôle du document lui-même
    data = base64.b64encode(b"x" * 4000).decode()
    assert c.post("/v1/documents", json={"filename": "gros.txt", "content_base64": data}).status_code == 413
    # au-delà de la limite d'upload elle-même : coupé par le middleware
    r = c.post("/v1/documents", content=b"x" * (2 * 1024 * 1024), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
