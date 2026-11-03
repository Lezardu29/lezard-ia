"""Santé des providers pour le fallback automatique (tâche 2.5).

Après un échec, la cible est mise en « cooldown » et exclue du routage le temps indiqué :
- 429           -> le modèle, durée Retry-After (sinon FALLBACK_RATE_LIMIT_COOLDOWN, 30 s)
- 404           -> le modèle (absent de ce provider), 5 min
- 401           -> tout le provider (clé invalide), 5 min
- 403           -> le modèle seul, 1 h : refus propre au modèle (OpenRouter : « only available on agentic
                   harnesses ») ; une clé refusée partout répond 403 à chaque modèle, chacun est alors écarté
- 5xx / réseau  -> tout le provider (panne, serveur local hors-ligne), FALLBACK_OFFLINE_COOLDOWN, 30 s
Un succès lève immédiatement le cooldown.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from .providers.base import LocalUnavailable, ProviderError

RETRYABLE = {401, 403, 404, 408, 409, 429}


def is_retryable(e: ProviderError) -> bool:
    """Erreur qui justifie d'essayer un autre modèle (pas une requête invalide)."""
    return e.status in RETRYABLE or e.status >= 500


def max_attempts() -> int:
    return int(os.getenv("FALLBACK_MAX_ATTEMPTS", "3"))


class HealthTracker:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.on_failure = None
        self.clock = clock
        self._until: dict[str, tuple[float, str]] = {}  # "provider" ou "provider/modèle"

    def mark_failure(self, ref: str, e: ProviderError) -> None:
        provider = ref.split("/", 1)[0]
        if isinstance(e, LocalUnavailable):  # PC occupé, file pleine (18.4) : le serveur n'est pas en panne
            return
        if self.on_failure:  # compteur de /metrics (18.5)
            self.on_failure(ref, e.status)
        if e.status == 429:
            key = ref
            cd = e.retry_after or float(os.getenv("FALLBACK_RATE_LIMIT_COOLDOWN", "30"))
        elif e.status == 404:
            key, cd = ref, 300.0
        elif e.status == 401:
            key, cd = provider, 300.0
        elif e.status == 403:  # sans cela, un seul modèle réservé coupait TOUS les modèles du fournisseur 5 min
            key, cd = ref, 3600.0
        elif is_retryable(e):
            key, cd = provider, float(os.getenv("FALLBACK_OFFLINE_COOLDOWN", "30"))
        else:
            return  # requête invalide : pas la faute du provider
        self._until[key] = (self.clock() + cd, f"HTTP {e.status}")

    def mark_success(self, ref: str) -> None:
        self._until.pop(ref, None)
        self._until.pop(ref.split("/", 1)[0], None)

    def _active(self, key: str) -> float:
        entry = self._until.get(key)
        if not entry:
            return 0.0
        left = entry[0] - self.clock()
        if left <= 0:
            del self._until[key]
            return 0.0
        return left

    def available(self, ref: str) -> bool:
        return not (self._active(ref) or self._active(ref.split("/", 1)[0]))

    def cooldowns(self) -> dict[str, dict]:
        return {
            k: {"remaining_s": round(left, 1), "reason": self._until[k][1]}
            for k in list(self._until)
            if (left := self._active(k))
        }
