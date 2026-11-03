"""Registre de métadonnées des modèles (tâche 1.4).

Prix en USD par million de tokens. Les modèles locaux sont toujours à 0.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict, replace


_live_rate: float | None = None  # taux BCE actualisé par gateway.fx (6.1)
DEFAULT_USD_EUR = 0.92
# Modèles qui annoncent « tools » à Ollama mais ne les appellent pas (mesuré : deepseek-r1:14b répond de mémoire à une
# recherche web, qwen2.5:14b la fait). Sans outils, le routage ne les choisit pas pour une requête avec outils.
NO_TOOL_CALLING = ("deepseek-r1",)


def set_live_rate(rate: float | None) -> None:
    global _live_rate
    _live_rate = rate


def usd_eur_rate() -> float:
    """Taux USD->EUR : USD_EUR_RATE (forçage manuel) > taux BCE actualisé > 0.92."""
    override = os.getenv("USD_EUR_RATE")
    if override:
        return float(override)
    return _live_rate if _live_rate else DEFAULT_USD_EUR


@dataclass(frozen=True)
class ModelInfo:
    id: str  # id upstream (sans préfixe provider)
    provider: str
    hosting: str  # "local" | "cloud"
    context_window: int = 8192
    input_per_mtok_usd: float = 0.0
    output_per_mtok_usd: float = 0.0
    vision: bool = False
    json_mode: bool = False
    quality: float = 0.5  # 0..1, a priori et fixe (seule la latence est mesurée en continu : LatencyTracker)
    ttft_ms: int = 800  # latence a priori avant le 1er token
    tools: bool = True
    reasoning: bool = False  # mode de réflexion natif (reasoning tokens / thinking)
    cache_read_per_mtok_usd: float | None = None  # prompt caching (3.3) ; None = prix input
    cache_write_per_mtok_usd: float | None = None
    free: bool = False  # offre gratuite officielle (13.8) : cloud à 0 €, dans la limite des quotas

    @property
    def ref(self) -> str:
        return f"{self.provider}/{self.id}"

    @property
    def priced(self) -> bool:
        """Coût calculable : modèle local (0 €), offre gratuite (0 €) ou cloud avec un tarif connu."""
        return self.hosting == "local" or self.free or bool(self.input_per_mtok_usd or self.output_per_mtok_usd)

    @property
    def cache_read_price(self) -> float:
        return self.input_per_mtok_usd if self.cache_read_per_mtok_usd is None else self.cache_read_per_mtok_usd

    @property
    def cache_write_price(self) -> float:
        return self.input_per_mtok_usd if self.cache_write_per_mtok_usd is None else self.cache_write_per_mtok_usd

    def cost_usd(
        self, input_tokens: int, output_tokens: int, cached_tokens: int = 0, cache_write_tokens: int = 0
    ) -> float:
        """`input_tokens` = total des tokens d'entrée, dont `cached_tokens` lus en cache et
        `cache_write_tokens` écrits en cache (facturés à leurs tarifs propres)."""
        if self.hosting == "local" or self.free:
            return 0.0
        fresh = max(0, input_tokens - cached_tokens - cache_write_tokens)
        return (
            fresh * self.input_per_mtok_usd
            + cached_tokens * self.cache_read_price
            + cache_write_tokens * self.cache_write_price
            + output_tokens * self.output_per_mtok_usd
        ) / 1_000_000

    def usage_cost(self, usage: dict | None) -> float:
        i, o, cached, write = usage_tokens(usage)
        return self.cost_usd(i, o, cached, write)

    def cache_savings_usd(self, usage: dict | None) -> float:
        """Économie nette du prompt caching vs. sans cache (négative si seule l'écriture a eu lieu)."""
        if self.hosting == "local" or self.free:
            return 0.0
        _, _, cached, write = usage_tokens(usage)
        return (
            cached * (self.input_per_mtok_usd - self.cache_read_price)
            - write * (self.cache_write_price - self.input_per_mtok_usd)
        ) / 1_000_000

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ref"] = self.ref
        return d


def usage_tokens(usage: dict | None) -> tuple[int, int, int, int]:
    """(prompt total, completion, lus en cache, écrits en cache) depuis un usage OpenAI normalisé."""
    u = usage or {}
    d = u.get("prompt_tokens_details") or {}
    return (
        u.get("prompt_tokens", 0),
        u.get("completion_tokens", 0),
        d.get("cached_tokens", 0) or 0,
        d.get("cache_creation_tokens", 0) or 0,
    )


# Remises de cache par provider : (lecture, écriture) en multiple du prix input
CACHE_MULTIPLIERS = {"anthropic": (0.10, 1.25), "openai": (0.50, 1.0), "mistral": (0.10, 1.0)}


def _m(provider, id, ctx, cin, cout, quality, ttft, vision=False, js=True, reasoning=False, cache_read_mult=None):
    read_mult, write_mult = CACHE_MULTIPLIERS.get(provider, (None, None))
    read_mult = cache_read_mult if cache_read_mult is not None else read_mult
    return ModelInfo(
        id,
        provider,
        "cloud",
        ctx,
        cin,
        cout,
        vision,
        js,
        quality,
        ttft,
        reasoning=reasoning,
        cache_read_per_mtok_usd=None if read_mult is None else round(cin * read_mult, 6),
        cache_write_per_mtok_usd=None if write_mult is None else round(cin * write_mult, 6),
    )


# Grille de départ, utilisée tant que l'import automatique (pricing.py, chaque jour) n'a pas tourné ou pour un
# modèle absent de la base importée. Prix relevés dans la base LiteLLM (USD / million de tokens) le :
BUILTIN_PRICES_DATE = "2026-10-19"
CLOUD_MODELS: list[ModelInfo] = [
    _m("openai", "gpt-4o", 128_000, 2.50, 10.00, 0.85, 700, True),
    _m("openai", "gpt-4o-mini", 128_000, 0.15, 0.60, 0.65, 500, True),
    _m("openai", "gpt-4.1", 1_047_576, 2.00, 8.00, 0.86, 700, True, cache_read_mult=0.25),
    _m("openai", "gpt-4.1-mini", 1_047_576, 0.40, 1.60, 0.72, 500, True, cache_read_mult=0.25),
    _m("openai", "gpt-4.1-nano", 1_047_576, 0.10, 0.40, 0.55, 400, True, cache_read_mult=0.25),
    _m("openai", "o4-mini", 200_000, 1.10, 4.40, 0.88, 2500, True, reasoning=True, cache_read_mult=0.25),
    _m("openai", "gpt-5", 272_000, 1.25, 10.00, 0.92, 2000, True, reasoning=True, cache_read_mult=0.10),
    _m("openai", "gpt-5-mini", 272_000, 0.25, 2.00, 0.82, 1200, True, reasoning=True, cache_read_mult=0.10),
    _m("openai", "gpt-5-nano", 272_000, 0.05, 0.40, 0.65, 800, True, reasoning=True, cache_read_mult=0.10),
    # Anthropic : pas de response_format, sorties structurées par consigne + contrôle (16.3)
    _m(
        "anthropic",
        "claude-opus-5-5",
        1_000_000,
        4.00,
        20.00,
        0.97,
        1500,
        True,
        reasoning=True,
        cache_read_mult=0.05,
        js=False,
    ),
    _m(
        "anthropic",
        "claude-sonnet-5-5",
        1_000_000,
        2.00,
        10.00,
        0.93,
        900,
        True,
        reasoning=True,
        cache_read_mult=0.05,
        js=False,
    ),
    _m("anthropic", "claude-haiku-5-5", 1_000_000, 0.10, 0.50, 0.80, 450, True, reasoning=True, js=False),
    _m("anthropic", "claude-opus-4-5", 200_000, 5.00, 25.00, 0.95, 1500, True, reasoning=True, js=False),
    _m("anthropic", "claude-sonnet-4-5", 200_000, 3.00, 15.00, 0.90, 900, True, reasoning=True, js=False),
    _m("anthropic", "claude-haiku-4-5", 200_000, 1.00, 5.00, 0.75, 500, True, reasoning=True, js=False),
    _m("mistral", "mistral-large-latest", 262_144, 0.50, 1.50, 0.80, 800, True),
    _m("mistral", "mistral-medium-latest", 262_144, 1.50, 7.50, 0.78, 700, True, reasoning=True),
    _m("mistral", "mistral-small-latest", 262_144, 0.15, 0.60, 0.62, 500, True),
    _m("groq", "openai/gpt-oss-120b", 131_072, 0.15, 0.60, 0.80, 250, reasoning=True, cache_read_mult=0.5),
    _m("groq", "openai/gpt-oss-20b", 131_072, 0.075, 0.30, 0.65, 200, reasoning=True, cache_read_mult=0.5),
    # Groq : modèles Llama retirés de la base LiteLLM ; derniers tarifs publiés par Groq
    _m("groq", "llama-3.3-70b-versatile", 128_000, 0.59, 0.79, 0.75, 200),
    _m("groq", "llama-3.1-8b-instant", 128_000, 0.05, 0.08, 0.50, 150),
    # OpenRouter publie ses tarifs dans /models : lus à la détection (prix annoncés par le fournisseur)
    _m("openrouter", "openai/gpt-4o-mini", 128_000, 0.15, 0.60, 0.65, 700, True),
    # Google Gemini (point d'accès compatible OpenAI) : tarif payant, sans objet quand l'offre gratuite est activée
    # 2.5 retiré : Google renvoie 404 aux nouveaux comptes (« no longer available to new users »). Tarifs repris de
    # comparateurs tiers (oct. 2026, non vérifiés sur la page officielle) : à confirmer sur ai.google.dev.
    _m("gemini", "gemini-3.1-pro-preview", 1_048_576, 2.00, 12.00, 0.90, 1500, True, reasoning=True),
    _m("gemini", "gemini-3.8-flash", 1_048_576, 0.75, 3.75, 0.80, 2500, True, reasoning=True),  # réflexion longue
    _m("gemini", "gemini-3.5-flash-lite", 1_048_576, 0.25, 1.50, 0.65, 400, True),
]

_PARAMS = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)
_REASONING_NAME = re.compile(r"(^|[-_:./])r1\b|qwq|qwen3|think|magistral|gpt-oss|reason")


# familles locales sans appel d'outils (function calling), quand le serveur ne publie pas ses capacités (16.2)
_NO_TOOLS_NAME = re.compile(
    r"(^|/)(llava|bakllava|moondream|minicpm-v|gemma:|gemma2|gemma3|phi:|phi3|tinyllama|"
    r"codellama|starcoder|deepseek-coder:|orca|vicuna|wizard|stablelm|dolphin|neural-chat)"
)


def infer_local(provider: str, model_id: str) -> ModelInfo:
    """Métadonnées a priori d'un modèle local, déduites de son nom (ex: llama3.1:70b)."""
    m = _PARAMS.search(model_id.replace(":", " ").replace("-", " "))
    size = float(m.group(1)) if m else 7.0
    quality = 0.35 if size <= 4 else 0.5 if size <= 9 else 0.6 if size <= 15 else 0.7 if size <= 35 else 0.8
    ttft = int(150 + size * 25)  # plus gros = plus lent sur matériel grand public
    name = model_id.lower()
    vision = any(k in name for k in ("llava", "vision", "-vl", "vl:", "gemma3", "gemma4", "minicpm-v", "bakllava"))
    reasoning = bool(_REASONING_NAME.search(name))
    tools = not _NO_TOOLS_NAME.search(name)
    return ModelInfo(
        model_id, provider, "local", 8192, 0.0, 0.0, vision, True, quality, ttft, reasoning=reasoning, tools=tools
    )


PRICE_FIELDS = (
    "input_per_mtok_usd",
    "output_per_mtok_usd",
    "cache_read_per_mtok_usd",
    "cache_write_per_mtok_usd",
    "context_window",
)


class ModelRegistry:
    def __init__(self, models: list[ModelInfo] | None = None):
        self._models: dict[str, ModelInfo] = {}
        self.local_context: dict[str, int] = {}  # fenêtre réelle des modèles locaux, mesurée à la détection (10.6)
        self.local_caps: dict[str, set[str]] = {}  # capacités annoncées par le serveur local (Ollama, 16.2)
        for m in models if models is not None else CLOUD_MODELS:
            self.add(m)
        self.builtin = dict(self._models)  # grille d'origine (retour arrière d'un prix, 6.2)
        self.announced: set[str] = set()  # prix lus dans /models du fournisseur (OpenRouter…), 10.7
        self.free_enabled: set[str] = set()  # fournisseurs dont l'offre gratuite est activée (13.8)

    def is_free(self, provider: str, model_id: str) -> bool:
        from .free_tiers import covers

        return covers(provider, model_id, self.free_enabled)

    def announce(self, provider: str, model_id: str, fields: dict) -> bool:
        """Prix publié par le fournisseur lui-même : appliqué à un modèle sans tarif, ou rafraîchi s'il venait déjà
        de là. Un prix saisi par l'administrateur ou importé n'est jamais remplacé."""
        ref = f"{provider}/{model_id}"
        current = self._models.get(ref)
        if ref not in self.announced and current is not None and current.priced:
            return False
        self.update_price(provider, model_id, **fields)
        self.announced.add(ref)
        return True

    def add(self, m: ModelInfo) -> None:
        self._models[m.ref] = m

    def update_price(self, provider: str, model_id: str, **fields) -> ModelInfo:
        """Met à jour les prix d'un modèle (ou l'ajoute, en cloud, s'il est inconnu)."""
        clean = {k: v for k, v in fields.items() if k in PRICE_FIELDS and v is not None}
        current = self._models.get(f"{provider}/{model_id}") or ModelInfo(
            model_id, provider, "cloud", quality=0.6, ttft_ms=800
        )
        updated = replace(current, **clean)
        self.add(updated)
        return updated

    def reset_price(self, ref: str) -> None:
        self.announced.discard(ref)
        if ref in self.builtin:
            self._models[ref] = self.builtin[ref]
        else:
            self._models.pop(ref, None)

    def all(self) -> list[ModelInfo]:
        return list(self._models.values())

    def get(self, provider: str, model_id: str, hosting: str = "cloud") -> ModelInfo:
        """Retourne les métadonnées, ou un défaut (gratuit si local, inconnu sinon). Un modèle cloud couvert par
        une offre gratuite (13.8) est marqué `free` : 0 €, en gardant ses caractéristiques connues."""
        found = self._models.get(f"{provider}/{model_id}")
        if hosting != "local" and self.is_free(provider, model_id):
            if found:
                return replace(found, free=True)
            guess = infer_local(provider, model_id)  # inconnu (OpenRouter « :free »…) : taille du nom
            return replace(guess, hosting="cloud", context_window=32_768, free=True)
        if found:
            return found
        if hosting == "local":
            info = infer_local(provider, model_id)
            ctx = self.local_context.get(info.ref)
            if ctx:
                info = replace(info, context_window=ctx)
            caps = self.local_caps.get(info.ref)
            if caps is not None:  # annoncées par le serveur : priment sur le nom
                info = replace(info, tools="tools" in caps, vision="vision" in caps)
            if model_id.lower().startswith(NO_TOOL_CALLING):
                info = replace(info, tools=False)
            return info
        return ModelInfo(model_id, provider, hosting)

    def find_bare(self, model_id: str) -> ModelInfo | None:
        for m in self._models.values():
            if m.id == model_id:
                return m
        return None
