"""Serveur local absent au démarrage (Ollama encore en démarrage ou en mise à jour à l'allumage du PC) : resondé en
tâche de fond et routable dès qu'il répond, sans passer par l'administration."""

import time

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer gw-master-0123456789abcdefghijklmnop"}


class LateOllama:
    def __init__(self):
        self.up = False
        self.probes = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            self.probes += 1
            if not self.up:
                raise httpx.ConnectError("refusé")
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )


def models(c):
    return [m["id"] for m in c.get("/v1/models", headers=MASTER).json()["data"]]


def test_local_server_started_late_becomes_routable(monkeypatch):
    monkeypatch.setenv("REDISCOVER_INTERVAL", "0.05")
    up = LateOllama()
    app = create_app(
        [
            ProviderConfig("ollama", "openai", "http://127.0.0.1:11434/v1", "local"),
            ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="o"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free", master_key="gw-master-0123456789abcdefghijklmnop"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        assert "ollama/llama3.1:8b" not in models(c)
        up.up = True  # Ollama a fini de démarrer
        deadline = time.monotonic() + 10
        while "ollama/llama3.1:8b" not in models(c) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "ollama/llama3.1:8b" in models(c)
        r = c.post(
            "/v1/chat/completions",
            headers=MASTER,
            json={"model": "auto", "messages": [{"role": "user", "content": "Salut"}]},
        )
        assert r.status_code == 200 and r.json()["x_gateway"]["provider"] == "ollama"
        settled = up.probes
        time.sleep(0.4)  # tout répond : plus aucune sonde de fond
        assert up.probes == settled
