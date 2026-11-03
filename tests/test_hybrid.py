"""Recherche hybride BM25 + embeddings (17.1)."""

import json
import math
import time
import unicodedata

import httpx
from fastapi.testclient import TestClient

from gateway import vectors
from gateway.config import ProviderConfig
from gateway.documents import hybrid_order, select_context
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

ACCOUNT_KEY = "ct-compte-0123456789abcdefghij"
ACCOUNT = {"Authorization": f"Bearer {ACCOUNT_KEY}"}
FILLERS = [
    "Le service comptable clôture les factures fournisseurs le vingt-cinq de chaque mois, après validation par la "
    "direction financière et rapprochement bancaire.",
    "La cantine propose chaque jour un menu végétarien, une entrée froide et un dessert de saison ; les tickets se "
    "rechargent à l'accueil du bâtiment B.",
    "Les badges perdus doivent être déclarés au poste de sécurité, qui désactive l'ancien et en remet un nouveau contre "
    "une caution de dix euros.",
    "La salle de réunion Atlantique accueille quinze personnes ; la réservation passe par l'agenda partagé, au plus "
    "tard la veille avant midi.",
    "Le réseau Wi-Fi invité change de mot de passe chaque lundi ; il est affiché sur l'écran du hall et communiqué par "
    "l'assistante d'équipe.",
    "Les congés d'été se posent avant le premier avril dans l'outil RH ; au-delà, ils sont accordés selon les besoins "
    "du service.",
]
TARGET = (
    "L'automobile du directeur stationne au niveau moins deux du parking souterrain, emplacement quarante-deux, "
    "près de l'ascenseur."
)
DOCUMENT = "\n\n".join(FILLERS * 2 + [TARGET])  # le passage utile tout à la fin, noyé
QUESTION = "Où se trouve la voiture ?"  # aucun mot en commun avec le passage utile
# sens des mots pour le faux modèle : synonymes sur le même axe
CONCEPTS = {
    "voiture": 0,
    "automobile": 0,
    "vehicule": 0,
    "auto": 0,
    "parking": 1,
    "stationne": 1,
    "factures": 2,
    "comptable": 2,
    "cantine": 3,
    "menu": 3,
    "badges": 4,
    "securite": 4,
    "reunion": 5,
    "salle": 5,
    "wifi": 6,
    "reseau": 6,
    "conges": 7,
    "rh": 7,
}
DIMS = 12


def fake_vector(text: str) -> list[float]:
    t = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    v = [0.0] * DIMS
    for w in "".join(c if c.isalnum() else " " for c in t).split():
        if w in CONCEPTS:
            v[CONCEPTS[w]] += 1.0
    v[DIMS - 1] = 0.05  # jamais nul
    return v


class Servers:
    """Faux Ollama local (chat + embeddings) et, au besoin, faux OpenAI (embeddings cloud)."""

    def __init__(self, local_embed=True, fail_embed=False):
        self.local_embed, self.fail_embed = local_embed, fail_embed
        self.embed_calls: list[tuple[str, str, int]] = []
        self.system: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path.endswith("/models"):
            ids = ["llama3.1:8b"] + (["nomic-embed-text", "bge-m3"] if self.local_embed else [])
            if host == "openai":
                ids = ["gpt-4o-mini", "text-embedding-3-small"]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        body = json.loads(request.content)
        if path.endswith("/embeddings"):
            self.embed_calls.append((host, body["model"], len(body["input"])))
            if self.fail_embed:
                return httpx.Response(500, text="panne")
            return httpx.Response(
                200,
                json={
                    "data": [{"index": i, "embedding": fake_vector(t)} for i, t in enumerate(body["input"])],
                    "usage": {"prompt_tokens": 1},
                },
            )
        self.system += [m["content"] for m in body["messages"] if m["role"] == "system"]
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1},
            },
        )


def make(servers, cloud=False, store=None):
    configs = [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")]
    if cloud:
        configs.append(ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="sk-test"))
    app = create_app(
        configs,
        httpx.AsyncClient(transport=httpx.MockTransport(servers)),
        plans=PlanResolver({ACCOUNT_KEY: "pro"}, "free"),
        store=store or DocumentStore(":memory:"),
    )
    return TestClient(app)


def upload(c) -> dict:
    r = c.post(
        "/v1/documents",
        headers=ACCOUNT,
        data={"chunk_size": "64", "chunk_overlap": "0"},
        files={"file": ("reglement.txt", DOCUMENT.encode(), "text/plain")},
    )
    assert r.status_code == 201, r.text
    return r.json()


def wait_vectors(c, doc_id, expected="ready", timeout=10.0) -> dict:
    end = time.time() + timeout
    while True:
        v = c.get(f"/v1/documents/{doc_id}", headers=ACCOUNT).json()["vectors"]
        if v["status"] == expected or time.time() > end:
            return v
        time.sleep(0.05)


def ask(c, doc_id) -> dict:
    r = c.post(
        "/v1/chat/completions",
        headers=ACCOUNT,
        json={
            "model": "ollama/llama3.1:8b",
            "documents": [doc_id],
            "documents_budget": 80,
            "messages": [{"role": "user", "content": QUESTION}],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["x_gateway"]["documents"]


def test_synonym_found_by_hybrid_missed_by_bm25(monkeypatch):
    """Critère d'acceptation : « voiture » / « automobile » trouvé en hybride, raté en BM25 seul."""
    s = Servers()
    with make(s) as c:
        doc = upload(c)
        assert doc["n_chunks"] > 4
        v = wait_vectors(c, doc["id"])
        assert v == {"status": "ready", "model": "ollama/bge-m3"}  # local, ordre stable
        assert s.embed_calls[0][:2] == ("ollama", "bge-m3")
        meta = ask(c, doc["id"])
        assert meta["retrieval"] == "hybrid" and meta["truncated"]
        assert "emplacement quarante-deux" in s.system[-1]
        s.system.clear()
        monkeypatch.setenv("DOCUMENT_EMBEDDINGS", "off")  # repli : BM25 seul
        meta = ask(c, doc["id"])
        assert meta["retrieval"] == "bm25" and "emplacement quarante-deux" not in s.system[-1]


def test_no_embedding_model_means_bm25_as_before():
    s = Servers(local_embed=False)
    with make(s) as c:
        doc = upload(c)
        assert wait_vectors(c, doc["id"], "no_model")["status"] == "no_model"
        assert ask(c, doc["id"])["retrieval"] == "bm25"
    assert s.embed_calls == []


def test_documents_never_sent_to_cloud_without_explicit_setting(monkeypatch):
    s = Servers(local_embed=False)
    with make(s, cloud=True) as c:
        doc = upload(c)
        assert wait_vectors(c, doc["id"], "no_model")["status"] == "no_model"
        ask(c, doc["id"])
        assert s.embed_calls == []  # rien n'est sorti
        monkeypatch.setenv("DOCUMENT_EMBEDDINGS", "cloud")  # choix explicite du propriétaire
        assert c.app.state is not None
        other = c.post("/v1/documents", headers=ACCOUNT, files={"file": ("x.txt", b"Une automobile.", "text/plain")})
        assert wait_vectors(c, other.json()["id"])["model"] == "openai/text-embedding-3-small"
        assert any(h == "openai" for h, _, _ in s.embed_calls)


def test_search_endpoint_is_hybrid(monkeypatch):
    s = Servers()
    with make(s) as c:
        doc = upload(c)
        wait_vectors(c, doc["id"])
        r = c.post(f"/v1/documents/{doc['id']}/search", headers=ACCOUNT, json={"query": QUESTION, "top_k": 2}).json()
        assert r["retrieval"] == "hybrid" and "automobile" in r["data"][0]["text"].lower()
        monkeypatch.setenv("DOCUMENT_EMBEDDINGS", "off")
        r = c.post(f"/v1/documents/{doc['id']}/search", headers=ACCOUNT, json={"query": QUESTION, "top_k": 2}).json()
        assert r["retrieval"] == "bm25" and r["data"] == []  # aucun mot en commun


def test_model_change_triggers_background_revectorisation(monkeypatch):
    s = Servers()
    store = DocumentStore(":memory:")
    with make(s, store=store) as c:
        doc = upload(c)
        assert wait_vectors(c, doc["id"])["model"] == "ollama/bge-m3"
        monkeypatch.setenv("DOCUMENT_EMBEDDINGS_MODEL", "ollama/nomic-embed-text")  # autre modèle
        status = c.get(f"/v1/documents/{doc['id']}", headers=ACCOUNT).json()["vectors"]
        assert status["status"] == "stale"
        assert ask(c, doc["id"])["retrieval"] == "bm25"  # jamais deux modèles mêlés
        # la relance périodique (toutes les 5 min) revectorise ; on l'appelle directement
        idx = next(v for v in _holders(c) if isinstance(v, vectors.VectorIndex))
        assert idx.refresh() >= 1
        assert wait_vectors(c, doc["id"])["model"] == "ollama/nomic-embed-text"
        assert ask(c, doc["id"])["retrieval"] == "hybrid"


def _holders(c):
    """Objets partagés de l'application (fermeture de create_app) : l'index des vecteurs."""
    for route in c.app.router.routes:
        endpoint = getattr(route, "endpoint", None)
        for cell in getattr(endpoint, "__closure__", None) or ():
            v = cell.cell_contents
            if isinstance(v, dict) and "vectors" in v:
                return list(v.values())
        for sub in getattr(getattr(route, "original_router", None), "routes", []):
            for cell in getattr(getattr(sub, "endpoint", None), "__closure__", None) or ():
                v = cell.cell_contents
                if isinstance(v, dict) and "vectors" in v:
                    return list(v.values())
    return []


def test_embedding_server_down_keeps_bm25():
    s = Servers(fail_embed=True)
    with make(s) as c:
        doc = upload(c)
        v = wait_vectors(c, doc["id"], "error")
        assert v["status"] == "error"
        assert ask(c, doc["id"])["retrieval"] == "bm25"


def test_rrf_cosine_and_pack():
    assert vectors.rrf(["a", "b", "c"], ["c", "a"])[:2] == ["a", "c"]
    blobs = [vectors.pack(v) for v in ([1, 0, 0], [0, 1, 0], [0.7, 0.7, 0])]
    assert vectors.cosine_rank([0, 1, 0.1], blobs)[0] == 1
    assert math.isclose(sum(x * x for x in vectors.unpack(vectors.pack([3, 4]))), 1.0, rel_tol=1e-6)
    saved, vectors._np = vectors._np, None  # Python pur, sans NumPy
    try:
        assert vectors.cosine_rank([0, 1, 0.1], blobs)[0] == 1
    finally:
        vectors._np = saved


def test_stopwords_do_not_drown_the_answer():
    chunks = [
        {"text": t, "tokens": 30, "index": i, "doc_order": 0}
        for i, t in enumerate(["La cantine se trouve au rez-de-chaussée.", "La salle se réserve la veille.", TARGET])
    ]
    order = hybrid_order(QUESTION, chunks, semantic=[2, 0, 1])
    assert order[0]["text"] == TARGET or order[1]["text"] == TARGET
    picked, truncated = select_context(chunks, QUESTION, 60, semantic=[2, 0, 1])
    assert truncated and any(c["text"] == TARGET for c in picked)
