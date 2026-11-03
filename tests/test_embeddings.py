"""Embeddings (16.1) : POST /v1/embeddings au format OpenAI."""

import base64
import json
import struct

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import embeddings
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.model_catalog import recommend
from gateway.plans import PlanResolver
from gateway.router import candidates as chat_candidates
from gateway.store import DocumentStore

ACCOUNT_KEY = "ct-compte-0123456789abcdefghij"
ACCOUNT = {"Authorization": f"Bearer {ACCOUNT_KEY}"}


class Servers:
    """Faux Ollama (local) et faux OpenAI (cloud) ; vecteur = [longueur du texte, n° du modèle, 0.5]."""

    def __init__(self, local=("nomic-embed-text", "llama3.1:8b", "bge-m3"), local_fails=False, cloud=True):
        self.local, self.local_fails, self.cloud = list(local), local_fails, cloud
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path.endswith("/models"):
            ids = self.local if host == "ollama" else ["gpt-4o-mini", "text-embedding-3-small", "text-embedding-weird"]
            if host == "openai" and not self.cloud:
                return httpx.Response(401, json={"error": {"message": "no key"}})
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        body = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            self.calls.append((host, body))
            if host == "ollama" and self.local_fails:
                return httpx.Response(500, json={"error": {"message": "modèle planté"}})
            items = body["input"]
            data = [
                {"object": "embedding", "index": i, "embedding": [float(len(x)), 1.0 if host == "ollama" else 2.0, 0.5]}
                for i, x in enumerate(items)
            ]
            data.reverse()  # ordre quelconque : remis par index
            usage = {"prompt_tokens": 7 * len(items), "total_tokens": 7 * len(items)} if host == "openai" else {}
            return httpx.Response(200, json={"object": "list", "data": data, "usage": usage})
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )


def make(servers=None):
    servers = servers or Servers()
    configs = [
        ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
        ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="sk-test-cloud-0123456789"),
    ]
    app = create_app(
        configs,
        httpx.AsyncClient(transport=httpx.MockTransport(servers)),
        plans=PlanResolver({ACCOUNT_KEY: "pro"}, "free"),
        store=DocumentStore(":memory:"),
    )
    return TestClient(app), servers


def key(c, **extra):
    r = c.post("/v1/keys", headers=ACCOUNT, json={"name": "rag", **extra})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['key']}"}, r.json()["id"]


def test_local_first_free_and_openai_format():
    c, s = make()
    with c:
        r = c.post("/v1/embeddings", headers=ACCOUNT, json={"input": ["abc", "défghij"], "model": "auto"})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["object"] == "list" and out["model"] == "ollama/bge-m3"  # local, ordre alphabétique
        assert [d["index"] for d in out["data"]] == [0, 1] and out["data"][0]["object"] == "embedding"
        assert [d["embedding"][0] for d in out["data"]] == [3.0, 7.0]  # remis dans l'ordre des entrées
        assert out["x_gateway"]["cost_eur"] == 0 and out["x_gateway"]["hosting"] == "local"
        assert out["x_gateway"]["usage_estimated"] and out["usage"]["prompt_tokens"] > 0
        assert s.calls[0][1]["encoding_format"] == "float"
        # texte seul, et nom sans fournisseur (Open WebUI)
        one = c.post("/v1/embeddings", headers=ACCOUNT, json={"input": "abc", "model": "nomic-embed-text"}).json()
        assert one["model"] == "ollama/nomic-embed-text" and len(one["data"]) == 1


def test_base64_encoding_done_by_gateway():
    c, _ = make()
    with c:
        out = c.post("/v1/embeddings", headers=ACCOUNT, json={"input": "abcd", "encoding_format": "base64"}).json()
        raw = base64.b64decode(out["data"][0]["embedding"])
        assert list(struct.unpack("<3f", raw)) == [4.0, 1.0, 0.5]


def test_cloud_billed_and_counted_in_budget():
    c, s = make()
    with c:
        out = c.post(
            "/v1/embeddings",
            headers=ACCOUNT,
            json={"input": ["a", "b"], "model": "openai/text-embedding-3-small", "dimensions": 256},
        ).json()
        assert out["model"] == "openai/text-embedding-3-small" and out["usage"]["prompt_tokens"] == 14
        assert out["x_gateway"]["cost_usd"] == pytest.approx(14 * 0.02 / 1e6)
        assert s.calls[-1][1]["dimensions"] == 256
        # clé avec un budget minuscule : estimation au-delà → refus avant l'appel (10.4)
        h, _ = key(c, limits={"budget": [{"amount": 0.000001, "period": "month"}]})
        calls = len(s.calls)
        r = c.post("/v1/embeddings", headers=h, json={"input": "x" * 400_000, "model": "openai/text-embedding-3-small"})
        assert r.status_code == 429 and "Budget" in r.text and len(s.calls) == calls
        # modèle cloud sans tarif connu : interdit aux clés ayant un budget (8.3)
        r = c.post("/v1/embeddings", headers=h, json={"input": "x", "model": "openai/text-embedding-weird"})
        assert r.status_code == 403 and "tarif" in r.text


def test_no_silent_switch_to_another_model():
    """Vecteurs de deux modèles incompatibles : panne = erreur, jamais un autre modèle."""
    c, s = make(Servers(local=("nomic-embed-text",), local_fails=True))
    with c:
        r = c.post("/v1/embeddings", headers=ACCOUNT, json={"input": "x"})
        assert r.status_code >= 500 and [h for h, _ in s.calls] == ["ollama"]  # le cloud n'est pas appelé
        r = c.post("/v1/embeddings", headers=ACCOUNT, json={"input": "x", "model": "text-embedding-ada-002"})
        assert r.status_code == 404 and "pas remplacé" in r.text and "ollama/nomic-embed-text" in r.text


def test_auto_choice_change_is_flagged():
    """« auto » qui change de modèle pour un même compte (serveur arrêté, modèle ajouté) : signalé."""
    from gateway.plans import PlanContext

    embeddings._last_auto.clear()
    ctx, other = PlanContext("pro", "acct:a"), PlanContext("pro", "acct:b")
    assert embeddings._remember_auto(ctx, "ollama/nomic-embed-text") is False  # premier choix
    assert embeddings._remember_auto(ctx, "ollama/nomic-embed-text") is False
    assert embeddings._remember_auto(other, "openai/text-embedding-3-small") is False  # autre compte
    assert embeddings._remember_auto(ctx, "openai/text-embedding-3-small") is True
    c, _ = make(Servers(local=("nomic-embed-text",)))
    with c:
        embeddings._last_auto.clear()
        first = c.post("/v1/embeddings", headers=ACCOUNT, json={"input": "x"}).json()
        assert "model_changed" not in first["x_gateway"]
        embeddings._last_auto[next(iter(embeddings._last_auto))] = "ollama/ancien-embed"
        again = c.post("/v1/embeddings", headers=ACCOUNT, json={"input": "x"}).json()
        assert again["x_gateway"]["model_changed"] is True
        explicit = c.post("/v1/embeddings", headers=ACCOUNT, json={"input": "x", "model": "nomic-embed-text"}).json()
        assert "model_changed" not in explicit["x_gateway"]  # modèle choisi : rien à signaler


def test_private_mode_and_permission():
    c, s = make(Servers(local=("llama3.1:8b",)))  # aucun modèle d'embeddings local
    with c:
        assert c.post("/v1/embeddings", headers=ACCOUNT, json={"input": "x"}).json()["model"].startswith("openai/")
        calls = len(s.calls)
        r = c.post("/v1/embeddings", headers=ACCOUNT, json={"input": "secret", "private": True})
        assert r.status_code == 503 and "Mode privé" in r.text and len(s.calls) == calls
        r = c.post(
            "/v1/embeddings",
            headers=ACCOUNT,
            json={"input": "secret", "model": "openai/text-embedding-3-small", "private": True},
        )
        assert r.status_code == 403 and len(s.calls) == calls
        h, _ = key(c, permissions={"embeddings": False})
        r = c.post("/v1/embeddings", headers=h, json={"input": "x"})
        assert r.status_code == 403 and "embeddings" in r.text
        assert c.get("/v1/embeddings/models", headers=h).json()["data"] == []
        assert (
            c.post("/v1/keys", headers=ACCOUNT, json={"name": "x", "permissions": {"embeddings": "non"}}).status_code
            == 400
        )
        h2, _ = key(c, permissions={"hosting": "local"})
        assert c.post("/v1/embeddings", headers=h2, json={"input": "x"}).status_code == 503


def test_input_validation_and_limits(monkeypatch):
    c, s = make()
    with c:
        for bad in (
            {},
            {"input": ""},
            {"input": []},
            {"input": [""]},
            {"input": [{"x": 1}]},
            {"input": [True]},
            {"input": "x", "encoding_format": "hex"},
            {"input": "x", "dimensions": 0},
            {"input": "x", "dimensions": "8"},
            {"input": "x", "model": 3},
        ):
            assert c.post("/v1/embeddings", headers=ACCOUNT, json=bad).status_code == 400, bad
        monkeypatch.setenv("EMBEDDINGS_MAX_INPUTS", "10")
        assert c.post("/v1/embeddings", headers=ACCOUNT, json={"input": ["a"] * 11}).status_code == 413
        assert c.post("/v1/embeddings", headers=ACCOUNT, json={"input": "x" * 1_300_000}).status_code == 413
        # listes de tokens (format OpenAI, envoyé par LangChain) : transmises telles quelles
        out = c.post("/v1/embeddings", headers=ACCOUNT, json={"input": [[1, 2, 3], [4, 5]]})
        assert out.status_code == 200 and s.calls[-1][1]["input"] == [[1, 2, 3], [4, 5]]
        assert c.post("/v1/embeddings", headers=ACCOUNT, json={"input": [1, 2, 3]}).status_code == 200


def test_models_list_and_chat_exclusion():
    c, _ = make()
    with c:
        listed = [m["id"] for m in c.get("/v1/embeddings/models", headers=ACCOUNT).json()["data"]]
        assert listed == [
            "ollama/bge-m3",
            "ollama/nomic-embed-text",
            "openai/text-embedding-3-small",
            "openai/text-embedding-weird",
        ]
        # le chat ne route jamais vers un modèle d'embeddings, même sans « embed » dans le nom (bge-m3)
        r = c.post(
            "/v1/chat/completions",
            headers=ACCOUNT,
            json={"model": "auto", "mode": "economy", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.json()["x_gateway"]["model"] == "llama3.1:8b"
    from gateway.registry import ModelRegistry

    models = chat_candidates(
        ModelRegistry(),
        {"ollama": ["bge-m3", "nomic-embed-text", "llama3.1:8b", "multilingual-e5-large", "all-minilm"]},
        {"ollama": "local"},
    )
    assert [m.id for m in models] == ["llama3.1:8b"]
    # les modèles de conversation courants restent au chat (aucun mot-clé trop large)
    chat = [
        "gemma4:e4b",
        "gemma3n:e2b",
        "gemma3:12b",
        "qwen2.5-coder:14b",
        "qwen2.5:7b",
        "deepseek-r1:8b",
        "phi3.5",
        "mistral:7b",
        "llama3.2:3b",
        "qwen3:30b-a3b",
        "gpt-oss:20b",
        "command-r7b",
    ]
    kept = chat_candidates(ModelRegistry(), {"ollama": chat}, {"ollama": "local"})
    assert sorted(m.id for m in kept) == sorted(chat) and not any(embeddings.is_embedding(n) for n in chat)


def test_catalog_recommends_embedding_model():
    no_gpu = recommend({"gpus": [], "ram": {"available": 8e9}, "disk": {"free": 1e11}}, context=4096)
    assert "embeddings" in no_gpu["uses"] and no_gpu["best"]["embeddings"] == "nomic-embed-text"  # le plus léger
    gpu = recommend(
        {"gpus": [{"name": "RTX", "vram_total": 12e9}], "ram": {"available": 8e9}, "disk": {"free": 1e11}}, context=4096
    )
    assert gpu["best"]["embeddings"] == "bge-m3"  # multilingue, en VRAM
