"""/v1/models : indication « chat » (liste « Modèle » du chat : ni embeddings, ni voix, ni dictée)."""

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer gw-master-0123456789abcdefghijklmnop"}
IDS = ["gemma3:12b", "bge-m3:latest", "nomic-embed-text", "whisper-small", "kokoro"]


def test_chat_flag():
    up = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"id": i} for i in IDS]}))
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=up),
        plans=PlanResolver({}, "free", master_key=MASTER["Authorization"][7:]),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        flags = {m["id"]: m["metadata"]["chat"] for m in c.get("/v1/models", headers=MASTER).json()["data"]}
    assert flags["ollama/gemma3:12b"] is True
    assert not any(flags[f"ollama/{i}"] for i in IDS[1:])
