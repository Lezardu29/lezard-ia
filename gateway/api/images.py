"""Génération d'images au format OpenAI (POST /v1/images/generations), par votre ComfyUI (comfyui.py)."""

from __future__ import annotations

import base64
import time
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from .. import comfyui
from ..registry import usd_eur_rate
from ..validation import json_object

PATH = "/v1/images/generations"


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    plan_ctx = shared.plan_ctx

    def record(gw, ctx, prompt: str, model: str, started: float) -> None:
        """Suivi du tableau de bord : une requête locale à 0 €, avec les tokens du texte de description (estimés : une
        image n'a pas de tokens ; ceux de sa description donnent une mesure comparable aux autres usages)."""
        tokens = max(1, len(prompt) // 4)
        if gw.limiter:
            gw.limiter.record(ctx.key, ctx.limits, tokens, 0.0)
        if gw.metrics:
            gw.metrics.capture(
                {
                    "owner": ctx.identity,
                    "key_id": (ctx.key or {}).get("id"),
                    "plan": ctx.plan,
                    "endpoint": "images",
                    "stream": False,
                    "status": "ok",
                    "provider": "comfyui",
                    "model": model,
                    "hosting": "local",
                    "task": "image",
                    "prompt_tokens": tokens,
                    "completion_tokens": 0,
                    "total_tokens": tokens,
                    "cost_usd": 0.0,
                    "billed_usd": 0.0,
                    "billed_eur": 0.0,
                    "fx_rate": usd_eur_rate(),
                    "saved_usd": 0.0,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "cache_hit": False,
                    "usage_estimated": True,
                }
            )

    @router.post(PATH)
    async def generations(request: Request):
        """JSON : prompt, size (« 768x768 »), negative_prompt ; une image (n = 1), renvoyée en base64 (b64_json)."""
        gw = holder["gw"]
        body = await json_object(request)
        ctx = plan_ctx(body, request)
        gw.limiter.hit_request(ctx.key, ctx.limits)  # RPM de la clé (4.3)
        request.state.gw_model = "comfyui"
        started, info = time.monotonic(), {}
        png = await comfyui.generate(body.get("prompt"), body.get("negative_prompt", ""), body.get("size"), info)
        record(gw, ctx, str(body.get("prompt")), info.get("model", "comfyui"), started)
        return JSONResponse(
            {"created": int(time.time()), "data": [{"b64_json": base64.b64encode(png).decode()}]},
            headers={**gw.limiter.headers(ctx.key, ctx.limits), "Cache-Control": "no-store"},
        )

    return router
