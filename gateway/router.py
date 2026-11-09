"""Moteur de routage par priorité utilisateur (tâche 2.2).

Modes :
- speed     : TTFT le plus bas (latence observée si disponible, sinon a priori)
- economy   : coût minimal, priorité absolue aux modèles locaux gratuits
- balanced  : compromis qualité / coût / vitesse

Étapes : candidats en ligne -> filtres durs (contexte, vision, JSON, tools)
-> plancher de qualité selon la complexité -> score selon le mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .classifier import Classification
from .effort import BALANCED_WEIGHTS, FLOOR_DELTA, REASONING_BONUS
from .registry import ModelInfo, ModelRegistry

MODES = ("speed", "economy", "balanced")
MODE_ALIASES = {
    "vitesse": "speed",
    "rapide": "speed",
    "fast": "speed",
    "eco": "economy",
    "éco": "economy",
    "économique": "economy",
    "économie": "economy",
    "équilibre": "balanced",
    "economique": "economy",
    "cheap": "economy",
    "equilibre": "balanced",
    "équilibré": "balanced",
    "auto": "balanced",
}
QUALITY_FLOOR = {"low": 0.3, "medium": 0.55, "high": 0.75}
SPEED_FLOOR_RELAX = 0.15  # le mode vitesse accepte un modèle un peu moins bon
SPEED_TIE = 1.3  # mode vitesse : jusqu'à 30 % plus lent = vitesse comparable
CODE_MODELS = ("coder", "code", "devstral", "codestral")
DEFAULT_OUTPUT_TOKENS = 500
# modèles d'embeddings (16.1) : nomic-embed-text, bge-m3, gte-qwen2, multilingual-e5, all-minilm…
EMBED_KEYWORDS = ("embed", "bge", "gte-", "e5-", "minilm")
NON_CHAT = (
    *EMBED_KEYWORDS,
    *("whisper", "tts", "kokoro", "piper", "orpheus", "dall-e", "moderation", "rerank"),
    # filtres de sécurité : ils classent un texte (« User Safety: safe »), ils ne conversent pas
    *("content-safety", "llama-guard", "shieldgemma"),
)


class RoutingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def normalize_mode(mode: str | None) -> str:
    m = (mode or "balanced").strip().lower()
    m = MODE_ALIASES.get(m, m)
    if m not in MODES:
        raise RoutingError(f"Mode inconnu '{mode}'. Valeurs : speed, economy, balanced.")
    return m


@dataclass
class Candidate:
    info: ModelInfo
    est_cost_usd: float
    latency_ms: float
    score: float = 0.0


@dataclass
class RouteDecision:
    ref: str
    provider: str
    model: str
    mode: str
    reason: str
    est_cost_usd: float
    alternatives: list[str] = field(default_factory=list)  # utilisé par le fallback (2.5)
    effort: str = "medium"

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "mode": self.mode,
            "effort": self.effort,
            "reason": self.reason,
            "est_cost_usd": round(self.est_cost_usd, 8),
            "alternatives": self.alternatives,
        }


class LatencyTracker:
    """Moyenne mobile exponentielle de la latence observée par modèle."""

    def __init__(self, alpha: float = 0.3):
        self.alpha = alpha
        self._ema: dict[str, float] = {}

    def observe(self, ref: str, ms: float) -> None:
        prev = self._ema.get(ref)
        self._ema[ref] = ms if prev is None else self.alpha * ms + (1 - self.alpha) * prev

    def get(self, ref: str) -> float | None:
        return self._ema.get(ref)


def candidates(registry: ModelRegistry, discovered: dict[str, list[str]], hosting: dict[str, str]) -> list[ModelInfo]:
    """Modèles réellement joignables : tous les locaux détectés + cloud connus du registre ou couverts par une
    offre gratuite (13.8)."""
    out = []
    for prov, ids in discovered.items():
        if hosting.get(prov) == "local":
            out += [registry.get(prov, i, "local") for i in ids if not any(k in i.lower() for k in NON_CHAT)]
        else:
            known = {m.id for m in registry.all() if m.provider == prov}
            out += [
                registry.get(prov, i, "cloud")
                for i in ids
                if i in known or (registry.is_free(prov, i) and not any(k in i.lower() for k in NON_CHAT))
            ]
    return out


def tier(m: ModelInfo) -> int:
    """Mode Économique : local, puis offres gratuites (13.8), puis cloud payant."""
    return 0 if m.hosting == "local" else 1 if m.free else 2


def route(
    cls: Classification,
    mode: str,
    models: list[ModelInfo],
    latency: LatencyTracker | None = None,
    json_required: bool = False,
    effort: str = "medium",
) -> RouteDecision:
    mode = normalize_mode(mode)
    q = lambda m: _quality(m, cls, effort)  # noqa: E731
    latency = latency or LatencyTracker()
    out_tokens = cls.max_output_tokens or DEFAULT_OUTPUT_TOKENS

    # 1. filtres durs
    eligible, rejected = [], []
    for m in models:
        if m.context_window < cls.min_context:
            rejected.append(f"{m.ref}: contexte")
            continue
        if cls.needs_vision and not m.vision:
            rejected.append(f"{m.ref}: vision")
            continue
        if cls.needs_tools and not m.tools:
            rejected.append(f"{m.ref}: tools")
            continue
        eligible.append(Candidate(m, m.cost_usd(cls.prompt_tokens, out_tokens), latency.get(m.ref) or m.ttft_ms))
    # modèle local de ce PC qui déborde de la carte graphique (poids + cache de sa fenêtre) : des minutes par réponse ;
    # « auto » lui préfère un autre modèle gratuit (local qui tient dans la carte, offre gratuite), jamais un cloud
    # payant à sa place ; il reste en secours
    spill = [c for c in eligible if c.info.fit == "offload"]
    if any(c.info.hosting == "local" or c.info.free for c in eligible if c not in spill):
        rejected += [f"{c.info.ref}: déborde de la carte graphique" for c in spill]
        eligible = [c for c in eligible if c not in spill]
    else:
        spill = []
    # sortie structurée (16.3) : modèles à response_format natif privilégiés ; à défaut, les autres (consigne
    # dans le prompt et contrôle de la réponse par la gateway)
    if json_required and any(c.info.json_mode for c in eligible):
        rejected += [f"{c.info.ref}: json" for c in eligible if not c.info.json_mode]
        eligible = [c for c in eligible if c.info.json_mode]
    if not eligible:
        if cls.needs_tools and any(r.endswith(": tools") for r in rejected):
            raise RoutingError(
                "Requête avec outils (function calling) : aucun modèle autorisé ne les gère "
                f"({', '.join(r[: -len(': tools')] for r in rejected if r.endswith(': tools'))} : sans "
                "outils). Installez un modèle qui les gère (llama3.1, qwen2.5, mistral…) ou "
                "autorisez un modèle cloud.",
                400,
            )
        raise RoutingError(
            f"Aucun modèle disponible ne satisfait la requête (contexte {cls.min_context}, vision={cls.needs_vision}).",
            503,
        )

    # 2. plancher de qualité (assoupli si personne ne l'atteint)
    floor = QUALITY_FLOOR[cls.complexity] + FLOOR_DELTA[effort] - (SPEED_FLOOR_RELAX if mode == "speed" else 0)
    pool = [c for c in eligible if q(c.info) >= floor]
    relaxed = not pool
    if relaxed:
        best_q = max(q(c.info) for c in eligible)
        pool = [c for c in eligible if q(c.info) >= best_q - 0.05]

    # 3. classement
    if mode == "speed":
        # à vitesse comparable (latence souvent seulement estimée), le modèle spécialisé dans la tâche passe devant
        fastest = min(c.latency_ms for c in pool)
        close = [c for c in pool if c.latency_ms <= fastest * SPEED_TIE]
        ranked = sorted(
            close, key=lambda c: (not specialist(c.info, cls), c.latency_ms, -q(c.info), c.est_cost_usd)
        ) + sorted((c for c in pool if c not in close), key=lambda c: (c.latency_ms, -q(c.info), c.est_cost_usd))
        why = f"TTFT ~{int(ranked[0].latency_ms)} ms"
        if specialist(ranked[0].info, cls) and ranked[0].latency_ms > fastest:
            why += f" · spécialisé {cls.task}"
    elif mode == "economy":
        ranked = sorted(pool, key=lambda c: (c.est_cost_usd, tier(c.info), -q(c.info), c.latency_ms))
        why = (
            "local gratuit"
            if ranked[0].info.hosting == "local"
            else "cloud gratuit"
            if ranked[0].info.free
            else f"~${ranked[0].est_cost_usd:.5f}"
        )
    else:
        wq, wc, wl = BALANCED_WEIGHTS[effort]
        max_cost = max(c.est_cost_usd for c in pool) or 1.0
        max_lat = max(c.latency_ms for c in pool) or 1.0
        for c in pool:
            c.score = wq * q(c.info) + wc * (1 - c.est_cost_usd / max_cost) + wl * (1 - c.latency_ms / max_lat)
        ranked = sorted(pool, key=lambda c: -c.score)
        why = f"score {ranked[0].score:.2f}"

    best = ranked[0]
    reason = f"{mode} · effort {effort} · {cls.complexity}/{cls.task} · {why}"
    if relaxed:
        reason += " · plancher qualité assoupli"
    # alternatives : reste du classement, puis les autres éligibles (pour le fallback)
    rest = [c.info.ref for c in ranked[1:]] + [c.info.ref for c in eligible + spill if c not in ranked]
    return RouteDecision(
        best.info.ref, best.info.provider, best.info.id, mode, reason, best.est_cost_usd, rest[:5], effort
    )


def specialist(m: ModelInfo, cls: Classification) -> bool:
    """Modèle spécialisé dans la tâche demandée (aujourd'hui : le code)."""
    return cls.task == "code" and any(k in m.id.lower() for k in CODE_MODELS)


def _quality(m: ModelInfo, cls: Classification, effort: str = "medium") -> float:
    """Qualité ajustée à la tâche et à l'effort (bonus code, bonus/malus raisonnement natif)."""
    q = m.quality
    if specialist(m, cls):
        q += 0.1
    if m.reasoning:
        q += REASONING_BONUS[effort]
    return max(0.0, min(1.0, q))
