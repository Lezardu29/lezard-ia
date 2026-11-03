"""Lecture vocale (19.4) : POST /v1/audio/speech, au format OpenAI (texte → voix), pendant de la dictée (13.9).

Routage : un serveur TTS LOCAL détecté d'abord (Piper, Kokoro via LocalAI / Speaches…, 0 €), sinon un fournisseur
cloud configuré (OpenAI), facturé au caractère et compté dans les budgets, quotas et règles fournisseur des clés.
Mêmes garde-fous que la transcription : permissions de la clé, mode privé, IA
connectées (7.2), instance hébergée (7.4), réservations a priori (10.4), file d'attente du serveur local (18.4).
Plusieurs serveurs locaux : chaque morceau va au moins occupé, le chat en demande plusieurs à la fois (voice_split).

Réponse : l'audio du serveur, vérifié (pas de JSON ni de HTML relayé comme du son), taille plafonnée, type déclaré
par la gateway d'après le format demandé (jamais celui du serveur amont).
"""

from __future__ import annotations

import os
import re
import time

from . import voice_split
from .voice_choice import reading_payload
from .counters import Reservation
from .health import is_retryable
from .limits import RateLimited
from .permissions import PermissionDenied, check_model, model_allowed
from .plans import PlanContext
from .providers.local_tts import NAME as LOCAL_TTS
from .providers.base import ProviderError
from .registry import ModelInfo, usd_eur_rate
from .transcription import sniff
from .validation import RequestError

PATH = "/v1/audio/speech"
TTS_KEYWORDS = ("tts", "kokoro", "piper", "orpheus", "xtts", "melotts", "parler", "chatterbox")
# $ par million de caractères (tarifs publics, à vérifier) ; gpt-4o-mini-tts est facturé en tokens audio :
# ~0,015 $ / minute, soit ~15 $ / million de caractères à 1 000 caractères / minute
PRICES_PER_MCHAR = {
    "openai/tts-1": 15.0,
    "openai/tts-1-hd": 30.0,
    "openai/gpt-4o-mini-tts": 15.0,
}
MEDIA = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/L16;rate=24000;channels=1",
}
SNIFFED = {
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "mp4": "audio/mp4",
    "webm": "audio/webm",
}
AUTO_NAMES = ("", "auto", "tts-1")  # les SDK OpenAI envoient souvent « tts-1 » : routage automatique
FIELDS = {"model", "input", "voice", "response_format", "speed", "instructions", "language"}
VOICE = re.compile(r"^[A-Za-z0-9_.:+-]{1,64}$")
# voix de l'API OpenAI : envoyées telles quelles par les SDK, inconnues de Kokoro → voix par défaut de la langue
OPENAI_VOICES = {"alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer", "verse"}
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
CHARS_PER_TOKEN = 4


def max_chars() -> int:
    return int(os.getenv("SPEECH_MAX_CHARS", "4096"))  # limite de l'API OpenAI


def max_response_bytes() -> int:
    return int(os.getenv("SPEECH_MAX_RESPONSE_BYTES", str(50 * 1024 * 1024)))


def is_tts(model_id: str) -> bool:
    m = model_id.lower()
    return any(k in m for k in TTS_KEYWORDS) and "whisper" not in m


def price_per_mchar(info: ModelInfo) -> float | None:
    """0 en local ; tarif connu en cloud ; None si inconnu (interdit aux clés avec budget, comme le chat : 8.3)."""
    return 0.0 if info.hosting == "local" else PRICES_PER_MCHAR.get(info.ref)


def cost_usd(info: ModelInfo, chars: int) -> float:
    return (price_per_mchar(info) or 0.0) * chars / 1_000_000


def media_type(audio: bytes, fmt: str) -> str:
    """Type déclaré d'après les octets reçus (un serveur peut rendre du WAV pour « mp3 ») ; sinon le format demandé
    (pcm, aac : sans en-tête reconnaissable)."""
    kind = sniff(audio)
    if kind and not (fmt == "aac" and kind == "mp3"):  # ADTS (aac) ressemble à une trame MP3
        return SNIFFED[kind]
    return MEDIA[fmt]


def default_voice(info: ModelInfo, language: str) -> str:
    """Voix quand le client n'en donne pas : SPEECH_VOICE, sinon une voix existante du modèle."""
    if os.getenv("SPEECH_VOICE"):
        return os.environ["SPEECH_VOICE"]
    if "kokoro" in info.id.lower():
        return "ff_siwis" if language.startswith("fr") else "af_heart"
    return "alloy"  # voix OpenAI ; LocalAI / Piper : la voix est celle du modèle


def voice_for(info: ModelInfo, voice: str | None, language: str, reading: dict | None = None) -> str:
    if not voice or ("kokoro" in info.id.lower() and voice in OPENAI_VOICES):
        if reading and reading.get("voice") and "kokoro" in info.id.lower():
            return reading["voice"]  # voix choisie sur la page Modèles (catégorie « Voix »)
        return default_voice(info, language)
    return voice


def validate(body: dict) -> dict:
    """Requête OpenAI vérifiée : {input, voice?, response_format, speed?, instructions?, model, language}."""
    unknown = set(body) - FIELDS
    if unknown:
        raise RequestError(f"Champ(s) inconnu(s) : {', '.join(sorted(unknown))}.")
    text = body.get("input")
    if not isinstance(text, str) or not CONTROL.sub("", text).strip():
        raise RequestError("'input' : texte à lire requis.")
    text = CONTROL.sub("", text).strip()
    if len(text) > max_chars():
        raise RequestError(f"Texte trop long ({len(text)} caractères, maximum {max_chars()}).", 413)
    fmt = body.get("response_format") or "mp3"
    if fmt not in MEDIA:
        raise RequestError(f"'response_format' : {', '.join(MEDIA)}.")
    out = {"input": text, "response_format": fmt, "model": str(body.get("model") or "auto").strip()}
    voice = body.get("voice")
    if voice is not None:
        if not isinstance(voice, str) or not VOICE.match(voice):
            raise RequestError("'voice' : nom de voix invalide.")
        out["voice"] = voice
    speed = body.get("speed")
    if speed is not None:
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0.25 <= speed <= 4.0:
            raise RequestError("'speed' : nombre entre 0.25 et 4.0.")
        out["speed"] = speed
    instructions = body.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str) or len(instructions) > 2000:
            raise RequestError("'instructions' : texte de 2000 caractères au plus.")
        out["instructions"] = instructions
    language = body.get("language") or ""
    if not isinstance(language, str) or not re.match(r"^([a-z]{2}([-_][A-Za-z]{2})?)?$", language):
        raise RequestError("'language' : code de langue (fr, en-US…).")
    out["language"] = language.lower()
    return out


# ---------- routage ----------
def _candidates(gw, ctx: PlanContext) -> list[ModelInfo]:
    out = []
    for name, ids in gw.discovered.items():
        p = gw.providers.get(name)
        # ponts (7.4) exclus : leur relais ne transporte que l'API de chat JSON
        if not p or not gw.visible(name, ctx.identity) or p.cfg.kind != "openai" or p.cfg.owner:
            continue
        out += [gw.registry.get(name, i, p.hosting) for i in ids if is_tts(i)]
    return out


async def choose(gw, ctx: PlanContext, model: str) -> list[ModelInfo]:
    """Modèles de synthèse vocale à essayer, dans l'ordre (le premier qui répond l'emporte)."""
    if not gw.discovered:
        await gw.detect()
    budgeted = bool((ctx.limits or {}).get("budget"))
    if "/" in model and model.split("/", 1)[0] in gw.providers:  # modèle explicite : fournisseur/modèle
        p, mid = await gw.resolve(model, ctx.identity)
        info = gw.registry.get(p.name, mid, p.hosting)
        if p.cfg.kind != "openai" or p.cfg.owner or not is_tts(mid):
            raise RequestError(f"'{info.ref}' n'est pas un modèle de synthèse vocale.")
        if not model_allowed(ctx.permissions, info):
            check_model(ctx.permissions, info)  # refus explicite (mode privé, clé)
        if budgeted and price_per_mchar(info) is None:
            raise PermissionDenied(403, f"'{info.ref}' sans tarif connu : interdit aux clés ayant un budget.")
        gw.guard.check(info, ctx.plan, ctx.is_admin)
        return [info]
    found = _candidates(gw, ctx)
    usable = [
        m
        for m in found
        if model_allowed(ctx.permissions, m)
        and not (budgeted and price_per_mchar(m) is None)
        and gw.guard.usable(m, ctx.plan, ctx.is_admin)
    ]
    kept = gw.local.filter(usable, ctx.is_admin, ctx.permissions)  # PC occupé (18.4) : pas de repli payant
    if usable and not kept:
        raise gw.local.busy_error()
    usable = kept
    if not usable and (ctx.permissions or {}).get("private"):
        raise RequestError(
            "Mode privé : aucun serveur de synthèse vocale local (Piper, Kokoro : LocalAI, Speaches…), "
            "le texte n'est pas envoyé au cloud. Désactivez le mode privé pour l'écouter.",
            503,
        )
    if not usable:
        why = "aucun autorisé pour cette clé" if found else "aucun détecté"
        raise RequestError(
            f"Aucun serveur de synthèse vocale disponible ({why}). Ajoutez un serveur local (Piper, Kokoro via "
            "LocalAI ou Speaches) sur la page Modèles, ou une clé OpenAI.",
            503,
        )
    wanted = model.split("/")[-1].lower() if model.lower() not in AUTO_NAMES else None
    healthy = [m for m in usable if gw.health.available(m.ref)] or usable
    price = lambda m: price_per_mchar(m) if price_per_mchar(m) is not None else 1e9
    return gw.tts_choice.order(healthy, wanted, price)  # le meilleur pour ce PC, ou celui fixé (page Modèles)


async def synthesize(gw, ctx: PlanContext, body: dict) -> tuple[bytes, str, dict]:
    """Choisit le serveur, applique les limites, appelle, compte. Renvoie (audio, type MIME, métadonnées)."""
    req = validate(body)
    chain = await choose(gw, ctx, req["model"])
    chars = len(req["input"])
    est_tokens = max(1, chars // CHARS_PER_TOKEN)
    est_cost = max(cost_usd(m, chars) for m in chain)  # le secours le plus cher compte
    res = Reservation()
    t0, attempts = time.monotonic(), []
    try:
        gw.limiter.check_usage(ctx.key, ctx.limits, est_tokens, est_cost, res, est_tokens)
        for i, info in enumerate(chain):
            p = gw.providers[info.provider]
            payload = {
                "model": info.id,
                "input": req["input"],
                "voice": voice_for(info, req.get("voice"), req["language"], gw.tts_choice.reading),
                "response_format": req["response_format"],
            }
            if not req.get("voice") or req["voice"] in OPENAI_VOICES:  # réglages de lecture de la page Modèles
                payload.update(
                    reading_payload(
                        gw.tts_choice.reading, payload["voice"], req["language"], info.provider == LOCAL_TTS
                    )
                )
            payload.update({k: req[k] for k in ("speed", "instructions") if k in req})
            hold = None
            try:
                hold = gw.guard.admit(info.provider, est_tokens, cost_usd(info, chars))  # règles fournisseur (9.6)
                with voice_split.using(info.provider):
                    audio = await p.speech(payload, max_response_bytes())
                meta = _settle(gw, ctx, info, chars, est_tokens, t0, attempts)
                return audio, media_type(audio, req["response_format"]), meta
            except (ProviderError, RateLimited) as e:
                if isinstance(e, ProviderError):
                    gw.health.mark_failure(info.ref, e)
                attempts.append({"ref": info.ref, "status": e.status, "error": e.message[:200]})
                if (isinstance(e, ProviderError) and not is_retryable(e)) or i == len(chain) - 1:
                    if len(attempts) == 1:
                        raise
                    tried = ", ".join(f"{a['ref']} ({a['status']})" for a in attempts)
                    raise ProviderError(e.status, f"Lecture vocale impossible : {tried}.", "gateway")
            finally:
                if hold:
                    hold.release()  # après l'enregistrement de l'usage réel
    finally:
        res.release()


def _settle(gw, ctx, info, chars, tokens, t0, attempts) -> dict:
    gw.health.mark_success(info.ref)
    real = cost_usd(info, chars)
    billed = real
    rate = usd_eur_rate()
    meta = {
        "provider": info.provider,
        "model": info.id,
        "hosting": info.hosting,
        "endpoint": "speech",
        "characters": chars,
        "cost_usd": round(billed, 8),
        "cost_eur": round(billed * rate, 8),
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "plan": ctx.plan,
    }
    if info.hosting != "local" and price_per_mchar(info) is None:
        meta["pricing"] = "unknown"
    if attempts:
        meta["fallback"] = {"used": info.ref, "attempts": attempts}
    if gw.limiter:
        gw.limiter.record(ctx.key, ctx.limits, tokens, billed)
    gw.guard.record(info.provider, tokens, real)
    if gw.metrics:
        gw.metrics.capture(
            {
                "owner": ctx.identity,
                "key_id": (ctx.key or {}).get("id"),
                "plan": ctx.plan,
                "endpoint": "speech",
                "stream": False,
                "status": "ok",
                "provider": info.provider,
                "model": info.id,
                "hosting": info.hosting,
                "task": "speech",
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
                "usage_estimated": False,
                "fallback_from": ",".join(a["ref"] for a in attempts) or None,
            }
        )
    return meta
