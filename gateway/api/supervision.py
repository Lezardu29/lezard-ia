"""Supervision (18.5) : /metrics (Prometheus) et réglages des alertes d'exploitation (clé maître)."""

from __future__ import annotations

import asyncio
import hmac
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import PlainTextResponse

from ..plans import PlanError
from ..supervision import Supervisor, metrics_token
from ..validation import json_object


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    admin = shared.admin

    def supervisor() -> Supervisor:
        return holder["supervisor"]

    def scraper_allowed(request: Request) -> bool:
        """Ce PC (sans tunnel), le jeton de lecture GATEWAY_METRICS_TOKEN, ou la clé maître."""
        if shared.is_local_console(shared.client_ip(request)):
            return True
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        expected = metrics_token()
        if expected and token and hmac.compare_digest(token.encode(), expected.encode()):
            return True
        admin(request)  # sinon : clé maître (et session d'administration hors de ce PC) ; erreur 401 / 403
        return True

    @router.get("/metrics", include_in_schema=False)
    async def prometheus(request: Request):
        if not scraper_allowed(request):
            raise PlanError(403, "Métriques réservées à ce PC, à GATEWAY_METRICS_TOKEN ou à la clé maître.")
        text = holder["prometheus"].render(supervisor().gauges(holder))
        return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")

    @router.get("/v1/admin/supervision")
    async def supervision_status(request: Request):
        admin(request)
        return await asyncio.to_thread(supervisor().status)

    @router.put("/v1/admin/supervision")
    async def supervision_settings(request: Request):
        admin(request)
        body = await json_object(request)
        return await asyncio.to_thread(supervisor().set_settings, body)

    @router.post("/v1/admin/supervision/check")
    async def supervision_check(request: Request):
        """Vérification immédiate (serveurs locaux, disque) ; {"test": true} envoie une alerte d'essai."""
        admin(request)
        body = await json_object(request, empty={})
        if body.get("test"):
            await supervisor().notify(
                "info", "Alerte d'essai", "Les alertes de supervision arrivent bien ici.", {"event": "test"}
            )
        fired = await supervisor().check()
        return {**(await asyncio.to_thread(supervisor().status)), "fired": fired}

    return router
