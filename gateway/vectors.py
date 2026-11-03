"""Recherche hybride dans les documents (17.1) : BM25 (mots) + embeddings (sens), fusionnés par rangs (RRF).

- À l'envoi d'un document, ses passages sont vectorisés EN TÂCHE DE FOND par un modèle d'embeddings LOCAL
  (Ollama nomic-embed-text, bge-m3…) : les documents ne sortent pas du réseau. Le cloud n'est utilisé que si le
  propriétaire l'a explicitement permis (DOCUMENT_EMBEDDINGS=cloud) ; DOCUMENT_EMBEDDINGS=off coupe tout.
  DOCUMENT_EMBEDDINGS_MODEL=fournisseur/modèle fixe le modèle ; sinon celui de la catégorie « Embeddings »
  (embedding_choice.py : fixé dans l'administration, ou le meilleur qui tourne bien sur ce PC).
- Vecteurs float32 normalisés, en BLOB dans chunk_vectors ; le modèle est mémorisé par document. Si le modèle
  disponible change, les documents sont revectorisés en tâche de fond ; en attendant (ou sans modèle), BM25 seul,
  comme avant : la recherche ne mélange jamais des vecteurs de deux modèles.
- Pas de base vectorielle externe (14.5) : produit scalaire en NumPy s'il est installé, sinon en Python pur ;
  suffisant pour quelques milliers de passages.
- Fusion : Reciprocal Rank Fusion, score = Σ 1 / (60 + rang) sur les deux classements.
"""

from __future__ import annotations

import array
import asyncio
import math
import os
import sys
from typing import TYPE_CHECKING

from .embeddings import _vector, is_embedding
from .providers.base import ProviderError

if TYPE_CHECKING:
    from .core import Gateway
    from .registry import ModelInfo
    from .store import DocumentStore

RRF_K = 60
BATCH = 32  # passages par appel au serveur d'embeddings
MAX_CHARS = 8000  # par passage envoyé (les modèles tronquent de toute façon)
QUERY_TIMEOUT = 15  # s : au-delà, la question se contente de BM25

try:  # optionnel : accélère, mais jamais requis
    import numpy as _np
except ImportError:  # pragma: no cover - dépend de l'installation
    _np = None


def mode() -> str:
    m = os.getenv("DOCUMENT_EMBEDDINGS", "local").strip().lower()
    return m if m in ("local", "cloud", "off") else "local"


def pack(vec) -> bytes:
    """Vecteur normalisé (norme 1) en float32 petit-boutiste : le cosinus devient un produit scalaire."""
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    a = array.array("f", (x / norm for x in vec))
    if sys.byteorder != "little":  # pragma: no cover
        a.byteswap()
    return a.tobytes()


def unpack(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    if sys.byteorder != "little":  # pragma: no cover
        a.byteswap()
    return a


def cosine_rank(query_vec, blobs: list[bytes]) -> list[int]:
    """Indices de `blobs` du plus proche au plus lointain (vecteurs déjà normalisés)."""
    q = unpack(pack(query_vec))
    if not blobs:
        return []
    if _np is not None:
        dims = len(q)
        m = _np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(blobs), dims)
        scores = m @ _np.frombuffer(q.tobytes(), dtype="<f4")
        return [int(i) for i in _np.argsort(-scores, kind="stable")]
    scores = [sum(a * b for a, b in zip(q, unpack(v))) for v in blobs]
    return sorted(range(len(blobs)), key=lambda i: -scores[i])


def rrf(*rankings: list, k: int = RRF_K) -> list:
    """Fusion de classements (listes d'identifiants, meilleur d'abord) : ordre stable à égalité."""
    score: dict = {}
    first: dict = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            score[key] = score.get(key, 0.0) + 1.0 / (k + rank + 1)
            first.setdefault(key, len(first))
    return sorted(score, key=lambda key: (-score[key], first[key]))


def current_model(gw: Gateway) -> ModelInfo | None:
    """Modèle d'embeddings des documents : local d'abord (cloud seulement si DOCUMENT_EMBEDDINGS=cloud)."""
    if mode() == "off":
        return None
    found = []
    for name, ids in gw.discovered.items():
        p = gw.providers.get(name)
        if not p or p.cfg.kind != "openai" or p.cfg.owner:  # ponts : chat seulement
            continue
        if p.hosting != "local" and mode() != "cloud":
            continue
        found += [gw.registry.get(name, i, p.hosting) for i in ids if is_embedding(i)]
    # pas de filtre sur la santé : un modèle en pause passagère ne doit pas en faire choisir un autre (tout serait
    # revectorisé) ; la question se contente alors de BM25 (semantic_order renvoie None)
    pinned = os.getenv("DOCUMENT_EMBEDDINGS_MODEL", "").strip()
    if pinned:
        return next((m for m in found if m.ref == pinned), None)  # indisponible : BM25, jamais un autre
    return gw.embedding_choice.pick(found)


async def embed_texts(gw: Gateway, info: ModelInfo, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    provider = gw.providers[info.provider]
    for i in range(0, len(texts), BATCH):
        part = [t[:MAX_CHARS] or " " for t in texts[i : i + BATCH]]
        result = await provider.embed({"model": info.id, "input": part, "encoding_format": "float"})
        items = sorted(result.get("data") or [], key=lambda d: d.get("index", 0) if isinstance(d, dict) else 0)
        if len(items) != len(part):
            raise ProviderError(502, f"{len(items)} vecteur(s) reçu(s) pour {len(part)} passage(s).", info.provider)
        out += [_vector(d) for d in items]
    return out


class VectorIndex:
    """Vectorisation en tâche de fond, une à la fois (un petit serveur local n'est pas submergé)."""

    def __init__(self, gw: Gateway, store: DocumentStore):
        self.gw, self.store = gw, store
        self.queue: asyncio.Queue | None = None
        self.task: asyncio.Task | None = None
        self.pending: set[str] = set()
        self.errors: dict[str, str] = {}
        self.pause_poll = 30.0  # s : vérification de la fin du mode économie

    def start(self) -> None:
        self.queue = asyncio.Queue()
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def schedule(self, owner: str, doc_id: str) -> None:
        if self.queue is None or doc_id in self.pending or mode() == "off":
            return
        self.pending.add(doc_id)
        self.queue.put_nowait((owner, doc_id))

    def refresh(self) -> int:
        """Documents sans vecteurs, ou vectorisés par un autre modèle que le modèle actuel : à (re)faire."""
        info = current_model(self.gw)
        if info is None:
            return 0
        todo = self.store.documents_to_vectorize(info.ref)
        for owner, doc_id in todo:
            self.schedule(owner, doc_id)
        return len(todo)

    async def idle(self) -> None:
        """Attend la fin des vectorisations en cours (tests, arrêt propre)."""
        if self.queue is not None:
            await self.queue.join()

    async def _run(self) -> None:
        from .core import log

        while True:
            owner, doc_id = await self.queue.get()
            try:
                while self.gw.local.indexing_paused():  # mode économie : la vectorisation attend sa fin
                    await self.gw.local.eco_wait(self.pause_poll)
                await self.vectorize(owner, doc_id)
                self.errors.pop(doc_id, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # serveur arrêté, modèle retiré… : BM25 seul
                self.errors[doc_id] = str(getattr(e, "message", e))[:200]
                log.warning("Vectorisation du document %s impossible : %s", doc_id, self.errors[doc_id])
            finally:
                self.pending.discard(doc_id)
                self.queue.task_done()

    async def vectorize(self, owner: str, doc_id: str) -> bool:
        info = current_model(self.gw)
        if info is None:
            return False
        chunks = await asyncio.to_thread(self.store.chunks, owner, doc_id)
        if not chunks:  # supprimé entre-temps
            return False
        vectors = await embed_texts(self.gw, info, [c["text"] for c in chunks])
        dims = {len(v) for v in vectors}
        if len(dims) != 1:
            raise ProviderError(502, "Vecteurs de tailles différentes.", info.provider)
        await asyncio.to_thread(
            self.store.set_vectors,
            owner,
            doc_id,
            info.ref,
            dims.pop(),
            [(c["index"], pack(v)) for c, v in zip(chunks, vectors)],
        )
        return True

    async def semantic_order(self, pool: list[dict], query: str) -> list[int] | None:
        """Positions de `pool` par proximité de sens avec `query`, pour les passages vectorisés par le modèle
        ACTUEL ; None si impossible (pas de modèle, rien de vectorisé, serveur injoignable)."""
        info = current_model(self.gw)
        if info is None or not query.strip():
            return None
        doc_ids = sorted({c["doc_id"] for c in pool})
        stored = await asyncio.to_thread(self.store.vectors, doc_ids, info.ref)
        positions = [i for i, c in enumerate(pool) if (c["doc_id"], c["index"]) in stored]
        if not positions:
            return None
        try:
            qvec = (await asyncio.wait_for(embed_texts(self.gw, info, [query]), QUERY_TIMEOUT))[0]
        except (TimeoutError, ProviderError):
            return None
        blobs = [stored[(pool[i]["doc_id"], pool[i]["index"])] for i in positions]
        if len(blobs[0]) != 4 * len(qvec):
            return None
        return [positions[j] for j in cosine_rank(qvec, blobs)]

    def status(self, doc: dict) -> dict:
        info = current_model(self.gw)
        if doc["id"] in self.pending:
            state = "paused" if self.gw.local.indexing_paused() else "pending"
        elif doc.get("embed_model") and info and doc["embed_model"] == info.ref:
            state = "ready"
        elif doc["id"] in self.errors:
            state = "error"
        elif info is None:
            state = "off" if mode() == "off" else "no_model"
        else:
            state = "stale" if doc.get("embed_model") else "pending"
        out = {"status": state, "model": doc.get("embed_model")}
        if state == "error":
            out["error"] = self.errors[doc["id"]]
        return out
