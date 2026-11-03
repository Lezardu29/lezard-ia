"""Pièces jointes du chat (19.2) : images vérifiées côté serveur, documents injectés, conversations enregistrées."""

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.conversations import fallback_title
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

ACCOUNT_KEY = "ct-compte-0123456789abcdefghij"
ACCOUNT = {"Authorization": f"Bearer {ACCOUNT_KEY}"}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000") + b"\x00" * 16
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24


def data_url(kind, raw):
    return f"data:image/{kind};base64,{base64.b64encode(raw).decode()}"


class Upstream:
    def __init__(self):
        self.bodies = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}, {"id": "llava:7b"}]})
        self.bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Vu."}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )


@pytest.fixture
def client():
    up = Upstream()
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({ACCOUNT_KEY: "pro"}, "free"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        yield c, up


def image_message(url, text="Que vois-tu ?"):
    return {
        "model": "auto",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": url}}],
            }
        ],
    }


def test_image_goes_to_a_vision_model(client):
    c, up = client
    r = c.post("/v1/chat/completions", headers=ACCOUNT, json=image_message(data_url("png", PNG)))
    assert r.status_code == 200, r.text
    assert up.bodies[-1]["model"] == "llava:7b"  # local et vision
    assert up.bodies[-1]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    for kind, raw in (("jpeg", JPEG), ("jpg", JPEG), ("webp", WEBP)):
        assert (
            c.post("/v1/chat/completions", headers=ACCOUNT, json=image_message(data_url(kind, raw))).status_code == 200
        )


@pytest.mark.parametrize(
    "url, status, expected",
    [
        (data_url("png", JPEG), 415, "pas une image PNG"),  # type annoncé trompeur
        (data_url("webp", b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 8), 415, "pas une image WEBP"),
        ("data:image/svg+xml;base64," + base64.b64encode(b"<svg onload=alert(1)>").decode(), 415, "PNG, JPEG"),
        ("data:text/html;base64,PGgxPg==", 415, "PNG, JPEG"),
        ("file:///etc/passwd", 400, "http(s)"),
        ("javascript:alert(1)", 400, "http(s)"),
        ("data:image/png;base64,***", 400, "base64 invalide"),
    ],
)
def test_images_checked_server_side(client, url, status, expected):
    c, up = client
    r = c.post("/v1/chat/completions", headers=ACCOUNT, json=image_message(url))
    assert r.status_code == status and expected in r.json()["error"]["message"], r.text
    assert up.bodies == []


def test_image_size_limit(client, monkeypatch):
    c, _ = client
    monkeypatch.setenv("CHAT_IMAGE_MAX_BYTES", "1000")
    r = c.post("/v1/chat/completions", headers=ACCOUNT, json=image_message(data_url("png", PNG + b"\x00" * 2000)))
    assert r.status_code == 413 and "trop volumineuse" in r.text
    assert (
        c.post("/v1/chat/completions", headers=ACCOUNT, json=image_message("https://exemple.org/chat.png")).status_code
        == 200
    )  # image en ligne : non téléchargée


def test_document_attached_in_chat_is_injected(client):
    c, up = client
    doc = c.post(
        "/v1/documents",
        headers=ACCOUNT,
        files={"file": ("contrat.txt", b"Le loyer mensuel est de 830 euros.", "text/plain")},
    ).json()
    r = c.post(
        "/v1/chat/completions",
        headers=ACCOUNT,
        json={
            "model": "auto",
            "documents": [doc["id"]],
            "messages": [{"role": "user", "content": "Quel est le loyer ?"}],
        },
    )
    assert r.status_code == 200, r.text
    system = [m["content"] for m in up.bodies[-1]["messages"] if m["role"] == "system"]
    assert any("830 euros" in s for s in system)
    assert r.json()["x_gateway"]["documents"]["chunks_used"] == 1
    bad = c.post("/v1/documents", headers=ACCOUNT, files={"file": ("photo.bin", b"\x00\x01binaire", "application/x")})
    assert bad.status_code == 415  # types vérifiés côté serveur


def test_conversation_keeps_images_and_attachments(client):
    c, _ = client
    doc = c.post(
        "/v1/documents", headers=ACCOUNT, files={"file": ("notes.md", b"# Notes\nTexte.", "text/markdown")}
    ).json()
    kept = c.post(
        "/v1/documents", headers=ACCOUNT, files={"file": ("projet.md", b"# Projet\nTexte.", "text/markdown")}
    ).json()
    project = c.post("/v1/projects", headers=ACCOUNT, json={"name": "P"}).json()
    assert c.post(f"/v1/projects/{project['id']}/documents/{kept['id']}", headers=ACCOUNT).status_code == 200
    msgs = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": data_url("png", PNG)}}],
            "attachments": [
                {"id": doc["id"], "filename": "notes.md", "kind": "text", "size_bytes": 14},
                {"id": kept["id"], "filename": "projet.md", "kind": "text", "size_bytes": 15},
            ],
        },
        {"role": "assistant", "content": "Vu."},
    ]
    conv = c.post("/v1/conversations", headers=ACCOUNT, json={"messages": msgs}).json()
    loaded = c.get(f"/v1/conversations/{conv['id']}", headers=ACCOUNT).json()["messages"]
    assert loaded[0]["content"][0]["image_url"]["url"] == data_url("png", PNG)
    assert [a["filename"] for a in loaded[0]["attachments"]] == ["notes.md", "projet.md"]
    # une image trompeuse est refusée aussi à l'enregistrement
    bad = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_url("png", JPEG)}}]}]
    assert c.post("/v1/conversations", headers=ACCOUNT, json={"messages": bad}).status_code == 415
    other = [{"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}]
    assert c.post("/v1/conversations", headers=ACCOUNT, json={"messages": other}).status_code == 400
    # supprimer la conversation supprime ses documents, sauf ceux d'un projet
    assert c.delete(f"/v1/conversations/{conv['id']}", headers=ACCOUNT).status_code == 200
    ids = [d["id"] for d in c.get("/v1/documents", headers=ACCOUNT).json()["data"]]
    assert doc["id"] not in ids and kept["id"] in ids


def test_fallback_title_uses_text_parts():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:,"}},
                {"type": "text", "text": "Que représente ce schéma ?"},
            ],
        }
    ]
    assert fallback_title(msgs) == "Que représente ce schéma ?"
    assert (
        fallback_title([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}])
        == "Nouvelle conversation"
    )
