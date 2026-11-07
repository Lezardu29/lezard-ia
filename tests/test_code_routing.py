"""Demandes de code routées vers le modèle spécialisé ; la gateway ne se détecte pas elle-même."""

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.classifier import classify
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.providers.base import GATEWAY_HEADER, instance_tag
from gateway.registry import ModelRegistry
from gateway.router import LatencyTracker, route
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"


def task(text: str) -> str:
    return classify({"messages": [{"role": "user", "content": text}]}).task


@pytest.mark.parametrize(
    "text",
    [
        "Crée-moi un jeu du serpent en HTML",
        "Fais une page web avec un formulaire de contact",
        "Crée une application de liste de tâches",
        "Peux-tu me créer un bot Discord ?",
        "Développe un site vitrine pour une boulangerie",
        "Écris une macro Excel qui colore les doublons",
        "Corrige mon composant Vue.js",
        "Un exemple de pointeurs en C++",
        "Write a game in C#",
    ],
)
def test_software_requests_are_code(text):
    assert task(text) == "code"


@pytest.mark.parametrize(
    "text",
    [
        "Fais un jeu de mots sur les chats",
        "Raconte-moi une histoire de pirates",
        "Quel est ton point de vue sur le télétravail ?",
        "Bonjour, comment vas-tu ?",
    ],
)
def test_ordinary_requests_are_not_code(text):
    assert task(text) != "code"


REG = ModelRegistry()
GEMMA = REG.get("ollama", "gemma4:e4b", "local")  # lu comme 4B : supposé un peu plus rapide
CODER = REG.get("ollama", "qwen2.5-coder:7b", "local")


@pytest.mark.parametrize("mode", ["speed", "balanced", "economy"])
def test_code_goes_to_coder_in_every_mode(mode):
    cls = classify({"messages": [{"role": "user", "content": "Crée-moi un jeu du serpent en HTML"}]})
    d = route(cls, mode, [GEMMA, CODER])
    assert d.ref == CODER.ref, d.reason
    if mode == "speed":
        assert "spécialisé code" in d.reason


def test_speed_mode_unchanged_for_chat_and_for_a_much_slower_coder():
    chat = classify({"messages": [{"role": "user", "content": "Bonjour, comment vas-tu ?"}]})
    assert route(chat, "speed", [GEMMA, CODER]).ref == GEMMA.ref
    code = classify({"messages": [{"role": "user", "content": "Écris un script Python"}]})
    lat = LatencyTracker()
    lat.observe(GEMMA.ref, 300)
    lat.observe(CODER.ref, 3000)  # bien plus lent mesuré : la vitesse prime
    assert route(code, "speed", [GEMMA, CODER], lat).ref == GEMMA.ref


class Servers:
    """Ollama sur :11434 ; « vLLM » sur :8000 = une gateway Smart AI (celle-ci, ou une autre installation)."""

    def __init__(self):
        self.gateway_id = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.port == 8000:
            return httpx.Response(
                200, json={"data": [{"id": "ollama/gemma4:e4b"}]}, headers={GATEWAY_HEADER: self.gateway_id or ""}
            )
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gemma4:e4b"}, {"id": "qwen2.5-coder:7b"}]})
        return httpx.Response(404)


def gateway(servers: Servers, store: DocumentStore):
    return create_app(
        [
            ProviderConfig("ollama", "openai", "http://localhost:11434/v1", "local"),
            ProviderConfig("vllm", "openai", "http://localhost:8000/v1", "local"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(servers)),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=store,
    )


def test_gateway_does_not_detect_itself():
    store, servers = DocumentStore(":memory:"), Servers()
    servers.gateway_id = instance_tag(store.instance_id())  # :8000 répond avec NOTRE empreinte
    with TestClient(gateway(servers, store)) as c:
        r = c.get("/v1/models", headers={"Authorization": f"Bearer {MASTER_KEY}"})
        ids = [m["id"] for m in r.json()["data"]]
        assert r.headers[GATEWAY_HEADER] == servers.gateway_id
        assert "ollama/qwen2.5-coder:7b" in ids
        assert not any(i.startswith("vllm/") for i in ids), ids
        assert c.get("/health").headers[GATEWAY_HEADER] == servers.gateway_id


def test_another_gateway_is_still_a_valid_provider():
    store, servers = DocumentStore(":memory:"), Servers()
    servers.gateway_id = "autre-installation"
    with TestClient(gateway(servers, store)) as c:
        ids = [m["id"] for m in c.get("/v1/models", headers={"Authorization": f"Bearer {MASTER_KEY}"}).json()["data"]]
        assert "vllm/ollama/gemma4:e4b" in ids


def test_instance_id_is_stable():
    store = DocumentStore(":memory:")
    assert store.instance_id() == store.instance_id() and len(store.instance_id()) == 24
