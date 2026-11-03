"""Embeddings (16.1) : POST /v1/embeddings, au format OpenAI (Open WebUI, Continue, LlamaIndex, LangChain…).

Modèle : un serveur LOCAL d'abord (Ollama, LM Studio, vLLM, LocalAI exposent /v1/embeddings ; 0 €), sinon un
fournisseur cloud configuré (OpenAI, Mistral, Gemini), facturé au token et compté dans les budgets, quotas et règles
fournisseur des clés. Mêmes garde-fous que le chat et la transcription : permissions de la clé (dont la nouvelle
permission « embeddings »), mode privé, IA connectées (7.2), instance hébergée,
réservations a priori (10.4). Ponts (7.4) exclus : leur relais ne transporte que le chat.

AUCUNE BASCULE vers un autre modèle, contrairement au chat : deux modèles d'embeddings produisent des vecteurs
incompatibles, et un index construit avec l'un puis interrogé avec l'autre renvoie des résultats faux sans aucune
erreur. Un modèle en panne donne donc une erreur. « auto » choisit toujours de la même façon (local, puis cloud le
moins cher, puis ordre alphabétique) ; EMBEDDINGS_DEFAULT_MODEL=fournisseur/modèle le fixe. Si le modèle choisi par
« auto » change pour un compte (serveur arrêté, modèle ajouté), la réponse le signale (x_gateway.model_changed).
Un nom de modèle inconnu est refusé, jamais remplacé.
"""

from __future__ import annotations

import base64
import os
import struct
import time
from collections import OrderedDict

from .counters import Reservation
from .permissions import PermissionDenied, check_model, model_allowed
from .plans import PlanContext
from .providers.base import ProviderError
from .registry import ModelInfo, usd_eur_rate
from .router import EMBED_KEYWORDS as KEYWORDS  # mêmes noms, exclus du chat
from .validation import RequestError

PATH = "/v1/embeddings"
# $ par million de tokens (tarifs publics, relevés le 2026-10-20 ; à vérifier : PUT des prix non géré ici)
PRICES_PER_MTOK = {
    "openai/text-embedding-3-small": 0.02,
    "openai/text-embedding-3-large": 0.13,
    "openai/text-embedding-ada-002": 0.10,
    "mistral/mistral-embed": 0.10,
    "gemini/gemini-embedding-001": 0.15,
}
AUTO_NAMES = ("", "auto")
CHARS_PER_TOKEN = 4
ENCODINGS = ("float", "base64")
_last_auto: OrderedDict[str, str] = OrderedDict()  # compte+clé → modèle choisi par « auto » (borné)
_LAST_AUTO_MAX = 2000


def max_inputs() -> int:
    return int(os.getenv("EMBEDDINGS_MAX_INPUTS", "2048"))  # limite de l'API OpenAI


def max_tokens() -> int:
    return int(os.getenv("EMBEDDINGS_MAX_TOKENS", "300000"))  # par requête (OpenAI : 300 000)


def is_embedding(model_id: str) -> bool:
    m = model_id.lower()
    return any(k in m for k in KEYWORDS)


def price_per_mtok(info: ModelInfo) -> float | None:
    """0 en local ; tarif connu en cloud ; None si inconnu (interdit aux clés avec budget, comme le chat : 8.3)."""
    return 0.0 if info.hosting == "local" else PRICES_PER_MTOK.get(info.ref)


def cost_usd(info: ModelInfo, tokens: int) -> float:
    return (price_per_mtok(info) or 0.0) * tokens / 1e6


def parse_input(raw) -> tuple[list, int]:
    """Texte, liste de textes, liste de tokens ou liste de listes de tokens (format OpenAI) → (entrées, tokens
    estimés). Les listes de tokens ne sont comprises que par les modèles OpenAI (LangChain les envoie par défaut :
    check_embedding_ctx_length=False pour un modèle local)."""
    if isinstance(raw, str):
        raw = [raw]
    elif isinstance(raw, list) and raw and all(isinstance(t, int) and not isinstance(t, bool) for t in raw):
        raw = [raw]  # un seul texte déjà découpé en tokens
    if not isinstance(raw, list) or not raw:
        raise RequestError("« input » requis : un texte ou une liste de textes.")
    if len(raw) > max_inputs():
        raise RequestError(f"Trop d'entrées : {max_inputs()} au plus par requête.", 413)
    tokens = 0
    for i, item in enumerate(raw):
        if isinstance(item, str):
            if not item.strip():
                raise RequestError(f"Entrée {i} vide.")
            tokens += max(1, len(item) // CHARS_PER_TOKEN)
        elif isinstance(item, list) and item and all(isinstance(t, int) and not isinstance(t, bool) for t in item):
            tokens += len(item)
        else:
            raise RequestError(f"Entrée {i} invalide : texte, ou liste de tokens (entiers), attendu.")
    if tokens > max_tokens():
        raise RequestError(f"Entrée trop longue (≈ {tokens} tokens, {max_tokens()} au plus par requête).", 413)
    return raw, tokens


# ---------- choix du modèle (un seul, sans bascule) ----------
def candidates(gw, ctx: PlanContext) -> list[ModelInfo]:
    out = []
    for name, ids in gw.discovered.items():
        p = gw.providers.get(name)
        # ponts (7.4) exclus, et fournisseurs sans API d'embeddings (Anthropic)
        if not p or not gw.visible(name, ctx.identity) or p.cfg.kind != "openai" or p.cfg.owner:
            continue
        out += [gw.registry.get(name, i, p.hosting) for i in ids if is_embedding(i)]
    return out


def _check_one(gw, ctx: PlanContext, info: ModelInfo) -> ModelInfo:
    budgeted = bool((ctx.limits or {}).get("budget"))
    if not model_allowed(ctx.permissions, info):
        check_model(ctx.permissions, info)  # refus explicite (mode privé, clé)
    if budgeted and price_per_mtok(info) is None:
        raise PermissionDenied(403, f"'{info.ref}' sans tarif connu : interdit aux clés ayant un budget.")
    gw.guard.check(info, ctx.plan, ctx.is_admin)
    return info


def usable(gw, ctx: PlanContext) -> list[ModelInfo]:
    budgeted = bool((ctx.limits or {}).get("budget"))
    ok = [
        m
        for m in candidates(gw, ctx)
        if model_allowed(ctx.permissions, m)
        and not (budgeted and price_per_mtok(m) is None)
        and gw.guard.usable(m, ctx.plan, ctx.is_admin)
    ]
    price = lambda m: price_per_mtok(m) if price_per_mtok(m) is not None else 1e9
    return sorted(ok, key=lambda m: (m.hosting != "local", price(m), m.ref))  # ordre stable


async def choose(gw, ctx: PlanContext, model: str) -> ModelInfo:
    if (ctx.permissions or {}).get("embeddings") is False:
        raise PermissionDenied(403, "Cette clé n'a pas accès aux embeddings.")
    if not gw.discovered:
        await gw.detect()
    model = (model or "").strip()
    if model.lower() in AUTO_NAMES and os.getenv("EMBEDDINGS_DEFAULT_MODEL"):
        model = os.environ["EMBEDDINGS_DEFAULT_MODEL"].strip()  # « auto » fixé par le propriétaire (.env)
    elif model.lower() in AUTO_NAMES and gw.embedding_choice.pinned:
        model = gw.embedding_choice.pinned  # fixé dans l'administration (catégorie « Embeddings »)
    if "/" in model and model.split("/", 1)[0] in gw.providers:  # fournisseur/modèle explicite
        p, mid = await gw.resolve(model, ctx.identity)
        info = gw.registry.get(p.name, mid, p.hosting)
        if p.cfg.kind != "openai" or p.cfg.owner or not is_embedding(mid):
            raise RequestError(f"'{info.ref}' n'est pas un modèle d'embeddings.")
        return _check_one(gw, ctx, info)
    found = usable(gw, ctx)
    if not found:
        if (ctx.permissions or {}).get("private"):
            raise RequestError(
                "Mode privé : aucun modèle d'embeddings local (ex. ollama pull nomic-embed-text) ; "
                "le texte n'est pas envoyé au cloud.",
                503,
            )
        why = "aucun autorisé pour cette clé" if candidates(gw, ctx) else "aucun détecté"
        raise RequestError(
            f"Aucun modèle d'embeddings disponible ({why}). Installez-en un en local "
            "(ollama pull nomic-embed-text) ou ajoutez une clé OpenAI / Mistral.",
            503,
        )
    if model.lower() in AUTO_NAMES:  # le même que pour les documents : le meilleur pour ce PC
        return gw.embedding_choice.rank(found)[0]
    same = [m for m in found if m.id.lower() == model.split("/")[-1].lower()]
    if same:  # nom sans fournisseur (Open WebUI) : même modèle
        return same[0]
    raise RequestError(
        f"Modèle d'embeddings '{model}' indisponible : il n'est pas remplacé par un autre (vecteurs "
        f"incompatibles). Disponibles : {', '.join(m.ref for m in found)} — ou « auto ».",
        404,
    )


def _remember_auto(ctx: PlanContext, ref: str) -> bool:
    """True si « auto » choisit un autre modèle que la dernière fois pour ce compte / cette clé."""
    who = f"{ctx.identity}|{(ctx.key or {}).get('id', '')}"
    before = _last_auto.get(who)
    _last_auto[who] = ref
    _last_auto.move_to_end(who)
    while len(_last_auto) > _LAST_AUTO_MAX:
        _last_auto.popitem(last=False)
    return before is not None and before != ref


def _vector(item) -> list[float]:
    vec = item.get("embedding") if isinstance(item, dict) else None
    if isinstance(vec, str):  # serveur qui a répondu en base64 malgré « float »
        raw = base64.b64decode(vec)
        return list(struct.unpack(f"<{len(raw) // 4}f", raw))
    if not isinstance(vec, list) or not vec:
        raise ProviderError(502, "Réponse d'embeddings sans vecteur.", "embeddings")
    return vec


async def embed(gw, ctx: PlanContext, body: dict) -> dict:
    inputs, est_tokens = parse_input(body.get("input"))
    encoding = body.get("encoding_format") or "float"
    if encoding not in ENCODINGS:
        raise RequestError("« encoding_format » : float ou base64.")
    dims = body.get("dimensions")
    if dims is not None and (not isinstance(dims, int) or isinstance(dims, bool) or dims < 1):
        raise RequestError("« dimensions » doit être un entier positif.")
    requested = body.get("model") or "auto"
    if not isinstance(requested, str):
        raise RequestError("« model » doit être un texte.")
    info = await choose(gw, ctx, requested)
    changed = requested.strip().lower() in AUTO_NAMES and _remember_auto(ctx, info.ref)
    res, hold, t0 = Reservation(), None, time.monotonic()
    try:
        gw.limiter.check_usage(ctx.key, ctx.limits, est_tokens, cost_usd(info, est_tokens), res, est_tokens)
        hold = gw.guard.admit(info.provider, est_tokens, cost_usd(info, est_tokens))  # règles fournisseur (9.6)
        # toujours en float vers le serveur (tous ne savent pas encoder) ; base64 fait ici si demandé
        payload = {"model": info.id, "input": inputs, "encoding_format": "float"}
        if dims is not None:
            payload["dimensions"] = dims
        try:
            result = await gw.providers[info.provider].embed(payload)
        except ProviderError as e:
            gw.health.mark_failure(info.ref, e)
            raise
        gw.health.mark_success(info.ref)
        items = sorted(result.get("data") or [], key=lambda d: d.get("index", 0) if isinstance(d, dict) else 0)
        if len(items) != len(inputs):
            raise ProviderError(502, f"{len(items)} vecteur(s) reçu(s) pour {len(inputs)} entrée(s).", info.provider)
        vectors = [_vector(d) for d in items]
        return _settle(gw, ctx, info, vectors, encoding, result.get("usage") or {}, est_tokens, t0, changed)
    finally:
        if hold:
            hold.release()  # après l'enregistrement de l'usage réel
        res.release()


def _settle(gw, ctx, info, vectors, encoding, usage, est_tokens, t0, changed) -> dict:
    tokens = usage.get("prompt_tokens") or usage.get("total_tokens")
    estimated = not isinstance(tokens, int) or tokens <= 0
    tokens = est_tokens if estimated else tokens
    real = cost_usd(info, tokens)
    billed = real
    rate = usd_eur_rate()
    meta = {
        "provider": info.provider,
        "model": info.id,
        "hosting": info.hosting,
        "endpoint": "embeddings",
        "dimensions": len(vectors[0]),
        "cost_usd": round(billed, 8),
        "cost_eur": round(billed * rate, 8),
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "plan": ctx.plan,
    }
    if estimated:
        meta["usage_estimated"] = True
    if changed:
        meta["model_changed"] = True  # vecteurs incompatibles avec ceux du modèle précédent
    if ctx.key:
        meta["key"] = {"id": ctx.key["id"], "name": ctx.key["name"]}
    if gw.limiter:
        gw.limiter.record(ctx.key, ctx.limits, tokens, billed)
    gw.guard.record(info.provider, tokens, real)
    if gw.metrics:
        gw.metrics.capture(
            {
                "owner": ctx.identity,
                "key_id": (ctx.key or {}).get("id"),
                "plan": ctx.plan,
                "endpoint": "embeddings",
                "stream": False,
                "status": "ok",
                "provider": info.provider,
                "model": info.id,
                "hosting": info.hosting,
                "task": "embeddings",
                "prompt_tokens": tokens,
                "completion_tokens": 0,
                "total_tokens": tokens,
                "cost_usd": round(real, 8),
                "billed_usd": round(billed, 8),
                "billed_eur": round(billed * rate, 8),
                "fx_rate": rate,
                "saved_usd": 0.0,
                "latency_ms": meta["latency_ms"],
                "cache_hit": False,
                "usage_estimated": estimated,
                "fallback_from": None,
            }
        )
    encode = (lambda v: base64.b64encode(struct.pack(f"<{len(v)}f", *v)).decode()) if encoding == "base64" else None
    return {
        "object": "list",
        "model": info.ref,
        "data": [
            {"object": "embedding", "index": i, "embedding": encode(v) if encode else v} for i, v in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        "x_gateway": meta,
    }
