"""Mémoire du contrôle du PC : préférences de l'utilisateur, gardées en base (par compte, ou par clé virtuelle) et
rappelées au modèle au début de chaque tâche sur le PC (« j'utilise Firefox, pas Edge » : commande /retiens du chat).

Le modèle qui pilote le PC n'écrit rien en mémoire : la connaissance du PC (applications utilisées, comment les ouvrir
et s'y repérer) vient du guide des applications (app_guides.py), écrit par une IA forte d'après l'inventaire du PC.
Les leçons notées automatiquement par les anciennes versions restent listées par /memoire (pour les effacer), mais ne
sont plus rappelées au modèle.
"""

from __future__ import annotations

import os

KINDS = ("preference", "lesson")
MAX_TEXT = 300
KEEP = int(os.getenv("TOOLS_PC_MEMORY_MAX", "300"))  # souvenirs par compte, les plus anciens s'effacent
MAX_PREFS = 30  # rappelées au modèle


class InvalidMemory(ValueError):
    pass


def clean(kind, text) -> str:
    if kind not in KINDS:
        raise InvalidMemory("'kind' : preference (préférence de l'utilisateur) ou lesson (leçon).")
    if not isinstance(text, str) or not text.strip():
        raise InvalidMemory("'text' requis : une phrase courte.")
    text = " ".join(text.split())
    if len(text) > MAX_TEXT:
        raise InvalidMemory(f"'text' : {MAX_TEXT} caractères au plus.")
    return text


def recall(store, owner: str) -> list[str]:
    """Préférences à rappeler au modèle, les plus récentes d'abord."""
    return [r["text"] for r in store.list_memory(owner) if r["kind"] == "preference"][:MAX_PREFS]


def guide(prefs: list[str]) -> str:
    """Suite du guide du contrôle du PC (message système)."""
    if not prefs:
        return ""
    return "Préférences de l'utilisateur, à suivre sauf si sa demande dit autre chose :\n" + "\n".join(
        f"- {p}" for p in prefs
    )
