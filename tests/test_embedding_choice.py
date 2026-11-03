"""Catégorie « Embeddings » : un seul modèle, le meilleur qui tourne bien sur ce PC, ou celui fixé par le
propriétaire ; le même pour la recherche dans les documents et pour « auto » de /v1/embeddings."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import core
from gateway.config import ProviderConfig
from gateway.embedding_choice import EmbeddingChoice, profile
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.registry import ModelInfo
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
ACCOUNT = {"Authorization": "Bearer compte-0123456789abcdef"}
GB = 1e9


def info(model, provider="ollama", hosting="local"):
    return ModelInfo(model, provider, hosting, 8192, 0.0, 0.0, False, False, 0.5, 100)


def chooser(vram_gb=12.0, ram_gb=16.0):
    c = EmbeddingChoice()
    c.budget = {"gpu": "RTX", "vram": vram_gb * GB, "vram_busy": 0, "ram": ram_gb * GB, "disk_free": None}
    c.this_pc = lambda name: name == "ollama"
    return c


def test_best_quality_that_fits_this_pc():
    bge, nomic, mini = info("bge-m3:latest"), info("nomic-embed-text:latest"), info("all-minilm")
    cloud = info("text-embedding-3-large", "openai", "cloud")
    c = chooser()
    assert c.rank([nomic, mini, cloud, bge]) == [bge, nomic, mini, cloud]  # local d'abord, puis la qualité
    a = c.assess(nomic)
    assert a["fit"] == "vram" and a["multilingual"] is False and a["score"] == pytest.approx(0.57)  # anglais : malus
    assert c.assess(cloud)["fit"] == "cloud"
    # petite machine : BGE-M3 (1,3 Go) déborde en RAM, plus lent mais bien meilleur en français → gardé devant ;
    # trop gros pour la machine → écarté tant qu'il reste un autre choix
    small = chooser(vram_gb=1.0, ram_gb=4)
    assert small.assess(bge)["fit"] == "offload" and small.rank([nomic, bge])[0] is bge
    assert chooser(vram_gb=0.5, ram_gb=0.5).rank([bge, nomic]) == [nomic, bge]
    # serveur d'une autre machine du réseau : son matériel n'est pas connu ici, pas de malus
    far = info("bge-m3:latest", "salon")
    assert chooser(vram_gb=0.5, ram_gb=0.5).assess(far)["fit"] == "unknown"


def test_language_and_profiles(monkeypatch):
    assert profile("qwen3-embedding:8b")["memory_gb"] == 6.5 and profile("inconnu")["known"] is False
    monkeypatch.setenv("EMBEDDINGS_LANGUAGE", "en")
    assert chooser().assess(info("nomic-embed-text"))["score"] == pytest.approx(0.72)  # documents en anglais


def test_pinned_model_or_nothing():
    c = chooser()
    bge, nomic = info("bge-m3:latest"), info("nomic-embed-text:latest")
    c.pinned = "ollama/nomic-embed-text:latest"
    assert c.pick([bge, nomic]) is nomic
    c.pinned = "ollama/absent"
    assert c.pick([bge, nomic]) is None  # jamais un autre modèle à sa place (vecteurs incompatibles)


class Upstream:
    def __init__(self):
        self.embedded: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            ids = ["llama3.1:8b", "nomic-embed-text:latest", "bge-m3:latest"]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        if request.url.path.endswith("/embeddings"):
            body = json.loads(request.content)
            self.embedded.append(body["model"])
            n = len(body["input"]) if isinstance(body["input"], list) else 1
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(n)]})
        return httpx.Response(404)


class FakeScanner:
    async def snapshot(self, providers, client):
        return {
            "gpus": [{"name": "RTX 5070", "vram_total": 12 * GB, "vram_used": 1 * GB}],
            "ram": {"available": 16 * GB},
            "disk": {},
            "models_vram": 0,
        }


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(core, "HardwareScanner", FakeScanner)
    up = Upstream()
    application = create_app(
        [ProviderConfig("ollama", "openai", "http://localhost:11434/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({"compte-0123456789abcdef": "free"}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )
    return application, up


def test_admin_category_auto_then_pinned(app):
    application, up = app
    with TestClient(application) as c:
        assert c.get("/v1/admin/embeddings", headers=ACCOUNT).status_code == 403
        st = c.get("/v1/admin/embeddings", headers=MASTER).json()
        assert st["mode"] == "auto" and st["chosen"] == "ollama/bge-m3:latest" and st["gpu"] == "RTX 5070"
        assert [m["ref"] for m in st["models"]] == ["ollama/bge-m3:latest", "ollama/nomic-embed-text:latest"]
        best = st["models"][0]
        assert best["chosen"] and best["fit"] == "vram" and best["this_pc"] and best["label"] == "BGE-M3"
        r = c.post("/v1/embeddings", headers=MASTER, json={"input": "Bonjour"})
        assert r.json()["model"] == "ollama/bge-m3:latest"  # « auto » : le même modèle que les documents
        doc = c.post("/v1/documents", headers=MASTER, json={"filename": "a.txt", "content": "Bonjour"}).json()
        for _ in range(100):
            if c.get(f"/v1/documents/{doc['id']}", headers=MASTER).json()["vectors"]["status"] == "ready":
                break
        assert c.get(f"/v1/documents/{doc['id']}", headers=MASTER).json()["embed_model"] == "ollama/bge-m3:latest"
        # fixé à la main : documents revectorisés, « auto » suit
        st = c.put("/v1/admin/embeddings", headers=MASTER, json={"model": "ollama/nomic-embed-text:latest"}).json()
        assert st["mode"] == "pinned" and st["chosen"] == "ollama/nomic-embed-text:latest"
        assert (
            c.post("/v1/embeddings", headers=MASTER, json={"input": "x"}).json()["model"]
            == "ollama/nomic-embed-text:latest"
        )
        for bad, code in (({"model": "ollama/llama3.1:8b"}, 404), ({"model": 3}, 404), ({}, 400), ({"x": 1}, 400)):
            assert c.put("/v1/admin/embeddings", headers=MASTER, json=bad).status_code == code, bad
        st = c.put("/v1/admin/embeddings", headers=MASTER, json={"model": None}).json()
        assert st["mode"] == "auto" and st["chosen"] == "ollama/bge-m3:latest"
        entries = c.get("/v1/admin/audit", headers=MASTER).json()["data"]
        assert [e["target"] for e in entries if e["action"] == "embeddings.choice" and e["status"] == 200] == [
            "auto",
            "ollama/nomic-embed-text:latest",
        ]
        models = c.get("/v1/embeddings/models", headers=MASTER).json()["data"]
        assert models[0]["id"] == "ollama/bge-m3:latest"


def test_env_pin_wins(app, monkeypatch):
    application, _ = app
    monkeypatch.setenv("DOCUMENT_EMBEDDINGS_MODEL", "ollama/nomic-embed-text:latest")
    with TestClient(application) as c:
        st = c.get("/v1/admin/embeddings", headers=MASTER).json()
        assert st["mode"] == "env" and st["chosen"] == "ollama/nomic-embed-text:latest"
