"""Fenêtre de contexte réelle des modèles locaux (10.6)."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.local_context import item_context, ollama_window
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
SHOW = {  # réponses de /api/show (extraits réels : model_info + parameters)
    "llama3.1:8b": {
        "modelfile": "...",
        "parameters": 'stop "<|eot_id|>"',
        "model_info": {"general.architecture": "llama", "llama.context_length": 131072},
    },
    "tinyllama": {"modelfile": "...", "model_info": {"llama.context_length": 2048}},
    "qwen-long": {
        "modelfile": "...",
        "parameters": "num_ctx 65536\ntemperature 0.7",
        "model_info": {"qwen2.context_length": 131072},
    },
}


@pytest.fixture(autouse=True)
def probe_on(monkeypatch):
    monkeypatch.setenv("LOCAL_CONTEXT_PROBE", "true")
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)


def test_ollama_effective_window(monkeypatch):
    assert ollama_window(SHOW["llama3.1:8b"]) == (4096, 131072)  # défaut du serveur Ollama, pas 128k
    assert ollama_window(SHOW["tinyllama"]) == (2048, 2048)  # plafonné par l'entraînement
    assert ollama_window(SHOW["qwen-long"]) == (65536, 131072)  # num_ctx du Modelfile
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "32768")  # serveur réglé (même .env via lancer.ps1)
    assert ollama_window(SHOW["llama3.1:8b"]) == (32768, 131072)
    assert ollama_window(SHOW["tinyllama"]) == (2048, 2048)


def test_item_context():
    assert item_context({"id": "x", "max_model_len": 32768}) == 32768  # vLLM
    assert item_context({"id": "x", "loaded_context_length": 16384, "max_context_length": 131072}) == 16384
    assert item_context({"id": "x", "context_length": True}) is None and item_context({"id": "x"}) is None


class Servers:
    def __init__(self, loaded=None):
        self.shows = 0
        self.loaded = loaded or []
        self.native = []  # corps reçus par /api/chat (API native d'Ollama)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "ollama":
            if path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": m} for m in SHOW]})
            if path == "/api/ps":  # rien de chargé, sauf si le test le décide
                return httpx.Response(200, json={"models": self.loaded})
            if path == "/api/show":
                self.shows += 1
                return httpx.Response(200, json=SHOW[json.loads(request.content)["model"]])
            if path == "/api/chat":
                self.native.append(json.loads(request.content))
                return httpx.Response(
                    200,
                    json={
                        "message": {"role": "assistant", "content": "ok"},
                        "done": True,
                        "done_reason": "stop",
                        "prompt_eval_count": 1,
                        "eval_count": 1,
                    },
                )
        if host == "lmstudio":
            if path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "mistral-nemo"}]})
            if path == "/api/v0/models":
                return httpx.Response(
                    200,
                    json={
                        "data": [{"id": "mistral-nemo", "max_context_length": 131072, "loaded_context_length": 16384}]
                    },
                )
        if host == "vllm" and path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen2.5-32b", "max_model_len": 32768}]})
        if path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )
        return httpx.Response(404)


def make(servers):
    return create_app(
        [
            ProviderConfig("ollama", "openai", "http://ollama:11434/v1", "local"),
            ProviderConfig("lmstudio", "openai", "http://lmstudio:1234/v1", "local"),
            ProviderConfig("vllm", "openai", "http://vllm:8000/v1", "local"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(servers)),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )


def windows(c):
    return {m["id"]: m["metadata"]["context_window"] for m in c.get("/v1/models", headers=MASTER).json()["data"]}


def chat(c, model, words, **kw):
    return c.post(
        "/v1/chat/completions",
        headers=MASTER,
        json={"model": model, "max_tokens": 500, "messages": [{"role": "user", "content": "mot " * words}], **kw},
    )


def test_windows_measured_and_used_for_routing():
    s = Servers()
    with TestClient(make(s)) as c:
        assert windows(c) == {
            "ollama/llama3.1:8b": 4096,
            "ollama/tinyllama": 2048,
            "ollama/qwen-long": 65536,
            "lmstudio/mistral-nemo": 16384,
            "vllm/qwen2.5-32b": 32768,
        }
        # ≈ 10 000 tokens : seuls les modèles dont la fenêtre suffit sont candidats
        r = chat(c, "auto", 10_000)
        assert r.status_code == 200, r.text
        routing = r.json()["x_gateway"]["routing"]
        assert {routing["ref"], *routing["alternatives"]} == {
            "ollama/qwen-long",
            "lmstudio/mistral-nemo",
            "vllm/qwen2.5-32b",
        }
        # modèle explicite trop petit, découpage refusé : erreur claire plutôt qu'une réponse sur un texte tronqué
        r = chat(c, "ollama/tinyllama", 3_000, split="off")
        assert r.status_code == 400 and "fenêtre de 2048 tokens" in r.json()["error"]["message"]
        assert chat(c, "ollama/tinyllama", 3_000).json()["x_gateway"]["split"]["parts"] > 1  # découpé (13.4)
        assert chat(c, "ollama/tinyllama", 200).status_code == 200
        # mesure faite une fois par modèle ; refaite sur détection forcée (page Modèles)
        assert s.shows == 3
        chat(c, "auto", 10)
        assert s.shows == 3
        c.get("/v1/admin/providers", headers=MASTER)
        assert s.shows == 6


def test_ollama_configured_for_long_context(monkeypatch):
    """Avant 10.6, llama3.1 (128k) était supposé à 8 192 tokens et écarté d'un prompt de 20 000 tokens."""
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "131072")
    s = Servers()
    with TestClient(make(s)) as c:
        assert windows(c)["ollama/llama3.1:8b"] == 131072
        r = chat(c, "ollama/llama3.1:8b", 20_000)
        assert r.status_code == 200, r.text
        # fenêtre imposée à Ollama : l'application Ollama lance son serveur à 4 096 et couperait la réponse
        assert [b["options"]["num_ctx"] for b in s.native] == [131072]


def test_default_window_not_imposed(monkeypatch):
    """OLLAMA_CONTEXT_LENGTH non réglé : rien à imposer, la requête part par l'API compatible OpenAI."""
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    s = Servers()
    with TestClient(make(s)) as c:
        assert chat(c, "ollama/llama3.1:8b", 10).status_code == 200
        assert s.native == []


def test_loaded_model_uses_exact_runtime_window():
    """Modèle déjà chargé : /api/ps donne la fenêtre réelle (ex. choisie par Ollama selon la mémoire vidéo)."""
    s = Servers(loaded=[{"name": "llama3.1:8b", "context_length": 32768}])
    with TestClient(make(s)) as c:
        w = windows(c)
        assert w["ollama/llama3.1:8b"] == 32768 and w["ollama/tinyllama"] == 2048
        assert s.shows == 3  # chargé aussi : /api/show pour ses capacités (16.2),
        # mais la fenêtre reste celle de /api/ps


def test_unreachable_native_api_keeps_default():
    """Serveur qui n'expose que l'API OpenAI : fenêtre supposée (8 192), jamais de refus sur une supposition."""

    def openai_only(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
        if request.url.path.endswith("/chat/completions"):
            return Servers()(request)
        return httpx.Response(404)

    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama:11434/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(openai_only)),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        assert windows(c) == {"ollama/llama3.1:8b": 8192}
        assert chat(c, "ollama/llama3.1:8b", 12_000).status_code == 200
