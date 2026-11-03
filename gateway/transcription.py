"""Transcription audio (13.9) : POST /v1/audio/transcriptions, au format OpenAI.

Routage : un serveur Whisper LOCAL détecté d'abord (LocalAI, Speaches / faster-whisper…, 0 €), sinon un fournisseur
cloud configuré (OpenAI, Groq, Mistral), facturé à la minute et compté dans les budgets, quotas et règles
fournisseur des clés. Mêmes garde-fous que le chat : permissions de la clé,
IA connectées (7.2), instance hébergée (7.4), réservations a priori (10.4).
Plusieurs serveurs locaux : un long enregistrement est coupé aux silences et transcrit en parallèle (voice_split).

Audio : taille limitée (TRANSCRIPTION_MAX_BYTES, BodyLimitMiddleware 9.1), durée limitée quand elle est mesurable,
type vérifié sur les octets (pas seulement l'en-tête Content-Type), jamais écrit sur le disque.
"""

from __future__ import annotations

import asyncio
import os
import struct
import time
from collections import deque

from starlette.formparsers import MultiPartParser

from . import voice_split
from .counters import Reservation
from .health import is_retryable
from .limits import RateLimited
from .permissions import PermissionDenied, check_model, model_allowed
from .plans import PlanContext
from .providers.base import ProviderError
from .registry import ModelInfo, usd_eur_rate
from .validation import RequestError

PATH = "/v1/audio/transcriptions"
STT_KEYWORDS = ("whisper", "transcribe", "voxtral", "parakeet")
# $ par minute d'audio (tarifs publics des fournisseurs, à vérifier : PUT des prix non géré pour l'audio)
PRICES_PER_MIN = {
    "openai/whisper-1": 0.006,
    "openai/gpt-4o-transcribe": 0.006,
    "openai/gpt-4o-mini-transcribe": 0.003,
    "groq/whisper-large-v3": 0.111 / 60,
    "groq/whisper-large-v3-turbo": 0.04 / 60,
    "groq/distil-whisper-large-v3-en": 0.02 / 60,
    "mistral/voxtral-mini-latest": 0.001,
    "mistral/voxtral-mini-2507": 0.001,
}
MIN_BILLED_SECONDS = {"groq": 10}  # Groq facture au moins 10 s par requête
FORMATS = ("json", "text", "verbose_json", "srt", "vtt")
FORWARDED = ("language", "prompt", "temperature")
AUTO_NAMES = ("", "auto", "whisper-1")  # les SDK OpenAI envoient « whisper-1 » : routage automatique
ESTIMATE_BYTES_PER_S = 4000  # 32 kbit/s (dictée Opus du client web) quand la durée est inconnue
TOKENS_PER_S = 3  # ~150 mots/min : estimation du texte produit


def max_bytes() -> int:
    return int(os.getenv("TRANSCRIPTION_MAX_BYTES", str(25 * 1024 * 1024)))  # limite de l'API OpenAI


def max_seconds() -> int:
    return int(os.getenv("TRANSCRIPTION_MAX_SECONDS", "1800"))


def is_stt(model_id: str) -> bool:
    m = model_id.lower()
    return any(k in m for k in STT_KEYWORDS) and "tts" not in m


def price_per_min(info: ModelInfo) -> float | None:
    """0 en local ; tarif connu en cloud ; None si inconnu (interdit aux clés avec budget, comme le chat : 8.3)."""
    return 0.0 if info.hosting == "local" else PRICES_PER_MIN.get(info.ref)


# ---------- audio ----------
def sniff(data: bytes) -> str | None:
    """Format reconnu d'après les premiers octets."""
    if data[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"fLaC":
        return "flac"
    if data[4:8] == b"ftyp":
        return "mp4"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "mp3"
    return None


def wav_seconds(data: bytes) -> float | None:
    """Durée d'un WAV PCM d'après son en-tête (fmt + data)."""
    pos, rate = 12, None
    while pos + 8 <= len(data):
        cid, size = data[pos : pos + 4], struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        if cid == b"fmt " and size >= 16:
            _, channels, rate, byte_rate = struct.unpack("<HHII", data[pos + 8 : pos + 20])
        elif cid == b"data" and rate:
            return size / byte_rate if byte_rate else None
        pos += 8 + size + (size & 1)
    return None


class _InMemoryParser(MultiPartParser):
    """Multipart sans fichier temporaire : Starlette bascule sur disque au-delà de 1 Mo par défaut."""

    spool_max_size = 1 << 62


async def read_upload(request) -> tuple[bytes, str, dict]:
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise RequestError("Format attendu : multipart/form-data avec un champ 'file' (comme l'API OpenAI).")
    form = await _InMemoryParser(request.headers, request.stream(), max_files=1, max_fields=16).parse()
    try:
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise RequestError("Champ 'file' manquant.")
        data = await f.read()
        fields = {k: v for k, v in form.multi_items() if isinstance(v, str)}
        return data, f.filename or "audio", fields
    finally:
        await form.close()


# ---------- routage ----------
def _stt_candidates(gw, ctx: PlanContext) -> list[ModelInfo]:
    out = []
    for name, ids in gw.discovered.items():
        p = gw.providers.get(name)
        # ponts (7.4) exclus : leur relais ne transporte que l'API de chat JSON
        if not p or not gw.visible(name, ctx.identity) or p.cfg.kind != "openai" or p.cfg.owner:
            continue
        out += [gw.registry.get(name, i, p.hosting) for i in ids if is_stt(i)]
    return out


async def choose(gw, ctx: PlanContext, model: str) -> list[ModelInfo]:
    """Modèles de transcription à essayer, dans l'ordre (le premier qui répond l'emporte)."""
    if not gw.discovered:
        await gw.detect()
    budgeted = bool((ctx.limits or {}).get("budget"))
    if "/" in model and model.split("/", 1)[0] in gw.providers:  # modèle explicite : fournisseur/modèle
        p, mid = await gw.resolve(model, ctx.identity)
        info = gw.registry.get(p.name, mid, p.hosting)
        if p.cfg.kind != "openai" or p.cfg.owner or not is_stt(mid):
            raise RequestError(f"'{info.ref}' n'est pas un modèle de transcription.")
        if not model_allowed(ctx.permissions, info):
            check_model(ctx.permissions, info)  # refus explicite (mode privé, clé)
        if budgeted and price_per_min(info) is None:
            raise PermissionDenied(403, f"'{info.ref}' sans tarif connu : interdit aux clés ayant un budget.")
        gw.guard.check(info, ctx.plan, ctx.is_admin)
        return [info]
    found = _stt_candidates(gw, ctx)
    usable = [
        m
        for m in found
        if model_allowed(ctx.permissions, m)
        and not (budgeted and price_per_min(m) is None)
        and gw.guard.usable(m, ctx.plan, ctx.is_admin)
    ]
    kept = gw.local.filter(usable, ctx.is_admin, ctx.permissions)  # PC occupé (18.4) : pas de repli payant
    if usable and not kept:
        raise gw.local.busy_error()
    usable = kept
    if not usable and (ctx.permissions or {}).get("private"):
        raise RequestError(
            "Mode privé : aucun serveur de transcription local (Whisper : LocalAI, Speaches…), "
            "l'audio n'est pas envoyé au cloud. Tapez votre message ou désactivez le mode privé.",
            503,
        )
    if not usable:
        why = "aucun autorisé pour cette clé" if found else "aucun détecté"
        raise RequestError(
            f"Aucun serveur de transcription disponible ({why}). Ajoutez un serveur Whisper local "
            "(LocalAI, Speaches / faster-whisper) sur la page Modèles, ou une clé OpenAI / Groq.",
            503,
        )
    wanted = model.split("/")[-1].lower() if model.lower() not in AUTO_NAMES else None
    healthy = [m for m in usable if gw.health.available(m.ref)] or usable
    price = lambda m: price_per_min(m) if price_per_min(m) is not None else 1e9
    return gw.stt_choice.order(healthy, wanted, price)  # le meilleur pour ce PC, ou celui fixé (page Modèles)


def billed_seconds(info: ModelInfo, seconds: float) -> float:
    return max(seconds, MIN_BILLED_SECONDS.get(info.provider, 0))


def cost_usd(info: ModelInfo, seconds: float) -> float:
    return (price_per_min(info) or 0.0) * billed_seconds(info, seconds) / 60


async def transcribe(gw, ctx: PlanContext, data: bytes, filename: str, fields: dict) -> tuple[bytes | dict, dict]:
    """Choisit le serveur, applique les limites, appelle, compte. Renvoie (réponse, métadonnées x_gateway)."""
    if not data:
        raise RequestError("Fichier audio vide.")
    if len(data) > max_bytes():
        raise RequestError(f"Audio trop volumineux (maximum {max_bytes()} octets).", 413)
    kind = sniff(data)
    if not kind:
        raise RequestError("Type de fichier non pris en charge : audio WebM, Ogg, WAV, MP3, MP4/M4A ou FLAC attendu.")
    fmt = (fields.get("response_format") or "json").lower()
    if fmt not in FORMATS:
        raise RequestError(f"'response_format' : {', '.join(FORMATS)}.")
    measured = wav_seconds(data) if kind == "wav" else None
    if measured is not None and measured > max_seconds():
        raise RequestError(f"Audio trop long ({measured:.0f} s, maximum {max_seconds()} s).", 413)
    est_s = measured if measured is not None else min(len(data) / ESTIMATE_BYTES_PER_S, max_seconds())

    chain = await choose(gw, ctx, (fields.get("model") or "auto").strip())
    est_tokens = int(est_s * TOKENS_PER_S)
    est_cost = max(cost_usd(m, est_s) for m in chain)  # le secours le plus cher compte
    res = Reservation()
    t0, attempts = time.monotonic(), []
    extra = {k: fields[k] for k in FORWARDED if fields.get(k)}
    try:
        gw.limiter.check_usage(ctx.key, ctx.limits, est_tokens, est_cost, res, est_tokens)
        workers = voice_split.local_servers(chain)
        if len(workers) > 1 and fmt in ("json", "text") and est_s * 2 >= voice_split.split_seconds():
            parts = await asyncio.to_thread(voice_split.split_audio, data, kind)  # durée exacte une fois décodé
            if parts and len(parts) > 1:
                return await _split(gw, ctx, workers, parts, extra, fmt, t0)
        for i, info in enumerate(chain):
            p = gw.providers[info.provider]
            hold = None
            try:
                hold = gw.guard.admit(info.provider, est_tokens, cost_usd(info, est_s))  # règles fournisseur (9.6)
                with voice_split.using(info.provider):
                    result, upstream_fmt = await _call(p, info, data, filename, kind, extra, fmt)
                return _settle(gw, ctx, info, result, fmt, upstream_fmt, est_s, measured, t0, attempts)
            except (ProviderError, RateLimited) as e:
                if isinstance(e, ProviderError):
                    gw.health.mark_failure(info.ref, e)
                attempts.append({"ref": info.ref, "status": e.status, "error": e.message[:200]})
                if (isinstance(e, ProviderError) and not is_retryable(e)) or i == len(chain) - 1:
                    if len(attempts) == 1:
                        raise
                    tried = ", ".join(f"{a['ref']} ({a['status']})" for a in attempts)
                    raise ProviderError(e.status, f"Transcription impossible : {tried}.", "gateway")
            finally:
                if hold:
                    hold.release()  # après l'enregistrement de l'usage réel
    finally:
        res.release()


async def _call(p, info: ModelInfo, data: bytes, filename: str, kind: str, extra: dict, fmt: str):
    """Un appel au serveur. Renvoie (réponse, format demandé au serveur)."""
    # whisper : « verbose_json » donne la durée réelle (facturation exacte), reconverti ensuite
    upstream = "verbose_json" if fmt in ("json", "text") and "whisper" in info.id.lower() else fmt
    try:
        return await p.transcribe(info.id, data, filename, kind, {**extra, "response_format": upstream}), upstream
    except ProviderError as e:
        if e.status != 400 or upstream == fmt:
            raise
    # serveur sans verbose_json : format demandé
    return await p.transcribe(info.id, data, filename, kind, {**extra, "response_format": fmt}), fmt


async def _split(gw, ctx: PlanContext, workers: list[ModelInfo], parts: list, extra: dict, fmt: str, t0: float):
    """Morceaux transcrits en parallèle, un worker par serveur local prenant le suivant dans la file (comme le
    découpage des longs messages, splitter._map) ; un morceau en échec est repris par un autre serveur, un worker qui échoue deux
    fois s'arrête. Chaque morceau est compté (limites, quotas, statistiques) ; le texte est recollé dans l'ordre."""
    texts: list[str | None] = [None] * len(parts)
    metas: list[dict] = [{}] * len(parts)
    queue, failed, lost = deque(range(len(parts))), [set() for _ in parts], []

    async def worker(info: ModelInfo):
        p, fails = gw.providers[info.provider], 0
        while not lost:
            i = next((j for j in queue if info.provider not in failed[j]), None)  # jamais deux fois le même raté
            if i is None:
                return
            queue.remove(i)
            audio, seconds = parts[i]
            hold = None
            try:
                hold = gw.guard.admit(info.provider, int(seconds * TOKENS_PER_S), cost_usd(info, seconds))
                with voice_split.using(info.provider):
                    result, upstream = await _call(p, info, audio, f"partie-{i + 1}.wav", "wav", extra, "json")
                out, metas[i] = _settle(gw, ctx, info, result, "json", upstream, seconds, seconds, t0, [])
                texts[i] = (out.get("text", "") if isinstance(out, dict) else out.decode("utf-8", "replace")).strip()
            except (ProviderError, RateLimited) as e:
                if isinstance(e, ProviderError):
                    gw.health.mark_failure(info.ref, e)
                failed[i].add(info.provider)
                fails += 1
                if len(failed[i]) < len(workers):
                    queue.append(i)  # repris par un autre serveur
                else:
                    lost.append(e)
                if fails >= 2:
                    return
            finally:
                if hold:
                    hold.release()

    await asyncio.gather(*(worker(w) for w in workers))
    if any(t is None for t in texts):
        why = lost[0].message if lost else "aucun serveur disponible"
        raise ProviderError(502, f"Audio découpé : un morceau n'a pas pu être transcrit ({why}).", "gateway")
    meta = {k: v for k, v in metas[0].items() if k != "fallback"}
    meta["audio_seconds"] = round(sum(m["audio_seconds"] for m in metas), 2)
    meta["cost_usd"] = round(sum(m["cost_usd"] for m in metas), 8)
    meta["cost_eur"] = round(sum(m["cost_eur"] for m in metas), 8)
    meta["latency_ms"] = int((time.monotonic() - t0) * 1000)
    refs = list(dict.fromkeys(f"{m['provider']}/{m['model']}" for m in metas))
    meta["split"] = {"parts": len(parts), "parallel": len(refs) > 1, "workers": refs}
    text = " ".join(t for t in texts if t)
    return (text.encode("utf-8") if fmt == "text" else {"text": text}), meta


def _settle(gw, ctx, info, result, fmt, upstream_fmt, est_s, measured, t0, attempts):
    """Usage réel : durée renvoyée par le serveur (verbose_json), sinon mesurée (WAV), sinon estimée."""
    gw.health.mark_success(info.ref)
    duration = result.get("duration") if isinstance(result, dict) else None
    estimated = not isinstance(duration, (int, float))
    seconds = float(duration) if not estimated else (measured if measured is not None else est_s)
    text = result.get("text", "") if isinstance(result, dict) else result.decode("utf-8", "replace")
    tokens = max(1, len(text) // 4) if text else 0
    real = cost_usd(info, seconds)
    billed = real
    rate = usd_eur_rate()
    meta = {
        "provider": info.provider,
        "model": info.id,
        "hosting": info.hosting,
        "endpoint": "transcription",
        "audio_seconds": round(seconds, 2),
        "cost_usd": round(billed, 8),
        "cost_eur": round(billed * rate, 8),
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "plan": ctx.plan,
    }
    if measured is None and estimated:
        meta["usage_estimated"] = True  # durée estimée d'après la taille du fichier
    if info.hosting != "local" and price_per_min(info) is None:
        meta["pricing"] = "unknown"
    if attempts:
        meta["fallback"] = {"used": info.ref, "attempts": attempts}
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
                "endpoint": "transcription",
                "stream": False,
                "status": "ok",
                "provider": info.provider,
                "model": info.id,
                "hosting": info.hosting,
                "task": "transcription",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": round(real, 8),
                "billed_usd": round(billed, 8),
                "billed_eur": round(billed * rate, 8),
                "fx_rate": rate,
                "saved_usd": 0.0,
                "latency_ms": meta["latency_ms"],
                "cache_hit": False,
                "usage_estimated": bool(meta.get("usage_estimated")),
                "fallback_from": ",".join(a["ref"] for a in attempts) or None,
            }
        )
    if isinstance(result, dict):  # reconversion vers le format demandé
        if fmt == "text":
            return text.encode("utf-8"), meta
        if fmt == "json" and upstream_fmt != "json":
            return {"text": text}, meta
    return result, meta
