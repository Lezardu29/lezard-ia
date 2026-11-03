"""Catégorie « Embeddings » : un seul modèle sert la recherche dans les documents (17.1) et « auto » de
/v1/embeddings (16.1) — le meilleur des modèles détectés qui tourne bien sur ce PC.

Classement :
- qualité connue du modèle (tableau ci-dessous, d'après les classements publics MTEB / MMTEB, arrondis), un modèle
  surtout anglais étant pénalisé quand la langue des documents n'est pas l'anglais (EMBEDDINGS_LANGUAGE, défaut fr) ;
- place sur CE PC (serveur local de cette machine) : tient en mémoire vidéo → aucun malus ; mémoire vidéo + RAM
  (plus lent) → malus ; trop gros → écarté tant qu'il reste un autre choix. Un serveur d'une autre machine du réseau
  n'est pas pénalisé (son matériel n'est pas connu ici) ;
- local d'abord ; à égalité, le plus léger.
Le choix ne dépend que des modèles installés et du matériel : il ne bascule pas au gré d'une panne passagère (tous
les documents seraient revectorisés). Le propriétaire peut le fixer (administration, page Modèles) ;
DOCUMENT_EMBEDDINGS_MODEL / EMBEDDINGS_DEFAULT_MODEL dans .env restent prioritaires.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from .model_catalog import fit

if TYPE_CHECKING:
    from .registry import ModelInfo

SETTING = "embeddings_choice"
GB = 1e9
# motif du nom → (nom affiché, qualité 0–1, multilingue, mémoire en Go pour l'exécuter)
KNOWN = (
    ("qwen3-embedding", "Qwen3 Embedding", 0.93, True, None),  # taille d'après le nom (0.6b, 4b, 8b)
    ("gemini-embedding", "Gemini Embedding", 0.93, True, 0),
    ("text-embedding-3-large", "OpenAI text-embedding-3-large", 0.91, True, 0),
    ("bge-m3", "BGE-M3", 0.90, True, 1.3),
    ("snowflake-arctic-embed2", "Snowflake Arctic Embed 2", 0.88, True, 1.3),
    ("multilingual-e5-large", "Multilingual E5 Large", 0.87, True, 1.2),
    ("embeddinggemma", "EmbeddingGemma", 0.86, True, 0.7),
    ("text-embedding-3-small", "OpenAI text-embedding-3-small", 0.84, True, 0),
    ("mistral-embed", "Mistral Embed", 0.83, True, 0),
    ("mxbai-embed-large", "mxbai-embed-large", 0.80, False, 0.7),
    ("granite-embedding", "Granite Embedding", 0.76, True, 0.6),
    ("snowflake-arctic-embed", "Snowflake Arctic Embed", 0.76, False, 0.7),
    ("text-embedding-ada", "OpenAI ada-002", 0.75, True, 0),
    ("nomic-embed-text", "Nomic Embed Text", 0.72, False, 0.3),
    ("paraphrase-multilingual", "Paraphrase Multilingual", 0.62, True, 0.6),
    ("all-minilm", "all-MiniLM", 0.50, False, 0.1),
)
UNKNOWN_QUALITY = 0.6
ENGLISH_ONLY_PENALTY = 0.15
OFFLOAD_PENALTY = 0.25
_PARAMS = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)


def language() -> str:
    return (os.getenv("EMBEDDINGS_LANGUAGE", "fr").strip().lower() or "fr")[:2]


def profile(model_id: str) -> dict:
    m = model_id.lower()
    for pattern, label, quality, multilingual, memory in KNOWN:
        if pattern in m:
            if memory is None:  # Qwen3 Embedding 0.6b / 4b / 8b : poids Q4 + marge
                size = _PARAMS.search(m.replace(":", " ").replace("-", " "))
                memory = round((float(size.group(1)) if size else 0.6) * 0.75 + 0.5, 1)
            return {
                "label": label,
                "quality": quality,
                "multilingual": multilingual,
                "memory_gb": memory,
                "known": True,
            }
    return {"label": model_id, "quality": UNKNOWN_QUALITY, "multilingual": None, "memory_gb": None, "known": False}


class EmbeddingChoice:
    setting = SETTING  # réglage où le choix fixé est gardé
    english_penalty = ENGLISH_ONLY_PENALTY
    offload_penalty = OFFLOAD_PENALTY

    def __init__(self):
        self.store = None
        self.budget: dict | None = None  # mémoire disponible sur ce PC (model_catalog.budget), relevée à part
        self.this_pc = lambda provider: False  # serveur de cette machine ? (fourni par la gateway)
        self.pinned: str | None = None

    def attach(self, store) -> None:
        self.store = store
        self.pinned = (store.get_setting(self.setting) or {}).get("model")

    def set_pinned(self, ref: str | None) -> None:
        self.pinned = ref
        if self.store:
            self.store.set_setting(self.setting, {"model": ref})

    def profile_of(self, info: ModelInfo) -> dict:
        return profile(info.id)

    def language(self) -> str:
        return language()

    def place(self, info: ModelInfo, p: dict) -> tuple[str, float]:
        """Où le modèle tourne (vram, offload, too_big, cloud, unknown : autre machine), et son malus."""
        if info.hosting != "local":
            return "cloud", 0.0
        if self.budget and self.this_pc(info.provider) and p["memory_gb"]:
            where = fit(p["memory_gb"] * GB, self.budget)
            return where, self.offload_penalty if where == "offload" else 0.0
        return "unknown", 0.0

    def assess(self, info: ModelInfo) -> dict:
        p = self.profile_of(info)
        score = p["quality"]
        if p["multilingual"] is False and self.language() != "en":
            score -= self.english_penalty
        place, penalty = self.place(info, p)
        return {**p, "fit": place, "score": round(score - penalty, 3)}

    def rank(self, models: list[ModelInfo]) -> list[ModelInfo]:
        """Meilleur d'abord : local, puis note, puis le plus léger ; les trop gros en dernier."""
        rated = [(m, self.assess(m)) for m in models]

        def key(pair):
            m, a = pair
            return (a["fit"] == "too_big", m.hosting != "local", -a["score"], a["memory_gb"] or 0, m.ref)

        return [m for m, _ in sorted(rated, key=key)]

    def pick(self, models: list[ModelInfo]) -> ModelInfo | None:
        """Le modèle fixé s'il est là (sinon aucun : jamais un autre à sa place), sinon le meilleur."""
        if self.pinned:
            return next((m for m in models if m.ref == self.pinned), None)
        ranked = self.rank(models)
        return ranked[0] if ranked else None
