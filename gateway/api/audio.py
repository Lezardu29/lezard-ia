"""Audio : transcription voix → texte (13.9) et lecture vocale texte → voix (19.4) au format OpenAI, leur
disponibilité pour le client web (dictée, bouton « Écouter »), et les catégories « Dictée » et « Lecture vocale » de
l'administration (le modèle retenu pour ce PC, fixé ou automatique : voice_choice.py)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ..limits import RateLimited
from ..model_catalog import budget
from ..permissions import PermissionDenied
from ..plans import PlanError
from ..providers.local_tts import NAME as LOCAL_TTS
from ..providers.local_whisper import NAME as LOCAL_WHISPER
from ..speech import PATH as SPEECH_PATH, _candidates as tts_candidates, choose as choose_speech, max_chars
from ..speech import price_per_mchar, synthesize
from ..transcription import PATH, _stt_candidates, choose, max_bytes, price_per_min, read_upload, transcribe
from ..voice_split import local_servers
from ..validation import RequestError, json_object
from ..voice_choice import KOKORO_VOICES, check_reading, voice_label

MEDIA = {"text": "text/plain; charset=utf-8", "srt": "application/x-subrip", "vtt": "text/vtt"}


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    plan_ctx = shared.plan_ctx
    admin = shared.admin

    @router.post(PATH)
    async def transcriptions(request: Request):
        """multipart/form-data : file (audio), model (« auto », « whisper-1 » ou fournisseur/modèle),
        language, prompt, temperature, response_format (json, text, verbose_json, srt, vtt)."""
        gw = holder["gw"]
        ctx = plan_ctx({}, request)
        gw.limiter.hit_request(ctx.key, ctx.limits)  # RPM de la clé (4.3)
        data, filename, fields = await read_upload(request)  # en mémoire seulement
        request.state.gw_model = fields.get("model")
        result, meta = await transcribe(gw, ctx, data, filename, fields)
        headers = {
            **gw.limiter.headers(ctx.key, ctx.limits),
            "X-Gateway-Provider": meta["provider"],
            "X-Gateway-Model": meta["model"],
            "X-Gateway-Cost-EUR": f"{meta['cost_eur']:.8f}",
        }
        if isinstance(result, bytes):  # text / srt / vtt
            fmt = (fields.get("response_format") or "json").lower()
            return Response(result, media_type=MEDIA.get(fmt, "text/plain"), headers=headers)
        return JSONResponse({**result, "x_gateway": meta}, headers=headers)

    @router.post(SPEECH_PATH)
    async def speech(request: Request):
        """JSON : input (texte), model (« auto », « tts-1 » ou fournisseur/modèle), voice, response_format
        (mp3, opus, aac, flac, wav, pcm), speed, instructions ; extension : language (voix par défaut)."""
        gw = holder["gw"]
        body = await json_object(request)
        ctx = plan_ctx(body, request)
        gw.limiter.hit_request(ctx.key, ctx.limits)  # RPM de la clé (4.3)
        request.state.gw_model = body.get("model")
        audio, media, meta = await synthesize(gw, ctx, body)
        headers = {
            **gw.limiter.headers(ctx.key, ctx.limits),
            "X-Gateway-Provider": meta["provider"],
            "X-Gateway-Model": meta["model"],
            "X-Gateway-Hosting": meta["hosting"],
            "X-Gateway-Cost-EUR": f"{meta['cost_eur']:.8f}",
            "Cache-Control": "no-store",
        }
        return Response(audio, media_type=media, headers=headers)

    async def availability(ctx, chooser) -> dict:
        try:
            chain = await chooser(holder["gw"], ctx, "auto")
        except (RequestError, PlanError, PermissionDenied, RateLimited) as e:
            return {"available": False, "reason": e.message}
        # parallel : serveurs locaux qui se partagent les morceaux (lecture : morceaux demandés à la fois)
        return {"available": True, "local": chain[0].hosting == "local", "parallel": max(1, len(local_servers(chain)))}

    @router.get("/v1/audio/status")
    async def audio_status(request: Request):
        """Dictée et lecture vocale du chat : un serveur est-il utilisable par cette clé ? (sans adresse interne)"""
        ctx = plan_ctx({}, request)
        out = {**(await availability(ctx, choose)), "max_bytes": max_bytes()}
        out["speech"] = {**(await availability(ctx, choose_speech)), "max_chars": max_chars()}
        return out

    # ----- catégories « Dictée » et « Lecture vocale » de la page Modèles -----
    KINDS = {
        "stt": ("stt_choice", _stt_candidates, price_per_min),
        "tts": ("tts_choice", tts_candidates, price_per_mchar),
    }

    async def voice_overview(ctx) -> dict:
        gw = holder["gw"]
        if not gw.discovered:
            await gw.detect()
        b = budget(await gw.hardware.snapshot(list(gw.providers.values()), gw.client))
        out = {"gpu": b.get("gpu")}
        for kind, (attr, candidates, price) in KINDS.items():
            choice = getattr(gw, attr)
            choice.budget = b
            found = candidates(gw, ctx)
            models = choice.order(found, None, lambda m, price=price: price(m) if price(m) is not None else 1e9)
            used = models[0].ref if models else None
            out[kind] = {
                "mode": "pinned" if choice.pinned else "auto",
                "pinned": choice.pinned,
                "pinned_missing": bool(choice.pinned) and choice.pinned not in {m.ref for m in found},
                "chosen": used,
                "models": [
                    {
                        "ref": m.ref,
                        "provider": m.provider,
                        "model": m.id,
                        "hosting": m.hosting,
                        "this_pc": choice.this_pc(m.provider),
                        "available": gw.health.available(m.ref),
                        "chosen": m.ref == used,
                        **choice.assess(m),
                    }
                    for m in models
                ],
            }
        out["reading"] = {
            **gw.tts_choice.reading,
            "voices": [voice_label(v) for v in await kokoro_voices()],
            "builtin": LOCAL_TTS in gw.providers,  # prononciation, mélange et pauses : Kokoro intégré seulement
            "kokoro": any("kokoro" in m["model"].lower() for m in out["tts"]["models"]),
        }
        w = gw.providers.get(LOCAL_WHISPER)
        out["whisper"] = (
            {"model": w.model_id, "device": w.device, "reason": w.reason, "auto": w.auto} if w is not None else None
        )
        return out

    async def kokoro_voices() -> list[str]:
        """Voix du Kokoro intégré quand il est chargé (jamais chargé pour ça), sinon celles de Kokoro 1.0."""
        p = holder["gw"].providers.get(LOCAL_TTS)
        if p is not None and getattr(p, "_model", None) is not None:
            return await asyncio.to_thread(p.voices)
        return list(KOKORO_VOICES)

    @router.put("/v1/admin/voice/reading")
    async def admin_voice_reading(request: Request):
        """Voix de lecture (Kokoro) : {voice, speed, pronunciation ("text" | "voice"), blend_voice, blend_share,
        pause} ; un champ absent reprend sa valeur par défaut."""
        admin(request)
        body = await json_object(request)
        try:
            reading = check_reading(body, await kokoro_voices())
        except ValueError as e:
            raise RequestError(str(e))
        holder["gw"].tts_choice.set_reading(reading)
        request.state.audit = {"target": reading["voice"] or "défaut"}
        return await voice_overview(plan_ctx({}, request))

    @router.get("/v1/admin/voice")
    async def admin_voice(request: Request):
        """Dictée et lecture vocale : modèles détectés classés pour ce PC (qualité en français, place en mémoire),
        celui qui sert, et la taille du Whisper intégré choisie pour ce PC."""
        admin(request)
        return await voice_overview(plan_ctx({}, request))

    @router.put("/v1/admin/voice")
    async def admin_voice_choice(request: Request):
        """{"kind": "stt" | "tts", "model": "fournisseur/modèle"} fixe le modèle ; {"model": null} : automatique."""
        admin(request)
        body = await json_object(request)
        if set(body) != {"kind", "model"} or body["kind"] not in KINDS:
            raise RequestError('Corps attendu : {"kind": "stt" ou "tts", "model": "fournisseur/modèle" ou null}.')
        attr, candidates, _ = KINDS[body["kind"]]
        ctx = plan_ctx({}, request)
        gw = holder["gw"]
        ref = body["model"]
        if ref is not None and (not isinstance(ref, str) or ref not in {m.ref for m in candidates(gw, ctx)}):
            raise RequestError(f"'{ref}' n'est pas un modèle de voix détecté.", 404)
        getattr(gw, attr).set_pinned(ref)
        request.state.audit = {"target": f"{body['kind']}:{ref or 'auto'}"}
        return await voice_overview(ctx)

    return router
