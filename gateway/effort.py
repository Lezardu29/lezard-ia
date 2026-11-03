"""Niveaux d'effort de raisonnement (tâche 2.3).

- low    : modèle rapide, aucune réflexion
- medium : traitement standard
- high   : reasoning tokens natifs (OpenAI reasoning_effort, Anthropic extended thinking),
           ou chaîne de pensée par instruction si le modèle n'a pas de mode natif
- auto   : déduit de la complexité estimée par le classifieur
"""

from __future__ import annotations

import os

from .classifier import Classification
from .registry import ModelInfo

EFFORTS = ("low", "medium", "high")
ALIASES = {
    "bas": "low",
    "faible": "low",
    "minimal": "low",
    "none": "low",
    "rapide": "low",
    "moyen": "medium",
    "standard": "medium",
    "normal": "medium",
    "élevé": "high",
    "eleve": "high",
    "haut": "high",
    "max": "high",
    "maximal": "high",
}

# Effets sur le routage
FLOOR_DELTA = {"low": -0.2, "medium": 0.0, "high": 0.1}
REASONING_BONUS = {"low": -0.1, "medium": 0.0, "high": 0.1}  # modèles à raisonnement natif
BALANCED_WEIGHTS = {  # (qualité, coût, vitesse)
    "low": (0.3, 0.2, 0.5),
    "medium": (0.5, 0.25, 0.25),
    "high": (0.7, 0.15, 0.15),
}

COT_PROMPT = (
    "Avant de répondre, raisonne étape par étape de façon rigoureuse : "
    "décompose le problème, vérifie chaque hypothèse et chaque calcul, "
    "puis donne une réponse finale claire."
)


class EffortError(ValueError):
    pass


def thinking_budget() -> int:
    return int(os.getenv("THINKING_BUDGET_TOKENS", "8000"))


def normalize_effort(value: str | None, cls: Classification | None = None) -> str:
    v = (value or "medium").strip().lower()
    if v == "auto":
        return {"low": "low", "medium": "medium", "high": "high"}[cls.complexity] if cls else "medium"
    v = ALIASES.get(v, v)
    if v not in EFFORTS:
        raise EffortError(f"Effort inconnu '{value}'. Valeurs : low, medium, high, auto.")
    return v


def apply_effort(payload: dict, info: ModelInfo, kind: str, effort: str) -> dict:
    """Adapte la requête upstream au niveau d'effort et aux capacités du modèle."""
    out = {k: v for k, v in payload.items() if k not in ("reasoning_effort", "effort", "thinking")}
    if info.reasoning:
        if kind == "anthropic":
            if effort == "high":
                out["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget()}
        else:
            out["reasoning_effort"] = effort
    elif effort == "high":
        msgs = list(out.get("messages", []))
        if msgs and msgs[0].get("role") in ("system", "developer") and isinstance(msgs[0].get("content"), str):
            msgs[0] = {**msgs[0], "content": f"{msgs[0]['content']}\n\n{COT_PROMPT}"}
        else:
            msgs.insert(0, {"role": "system", "content": COT_PROMPT})
        out["messages"] = msgs
    return out
