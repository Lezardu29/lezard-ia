"""Outils de la marketplace utilisés depuis le chat (« /<commande> », gateway/chat_tools.py). Ils agissent sur ce PC :
réservés à la clé maître (à distance, avec la double authentification de l'administration, comme le contrôle du PC)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request

from .. import chat_tools, travail
from ..core import _err
from ..validation import json_object


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    admin = shared.admin
    runs = chat_tools.Runs()
    sessions = travail.Sessions()

    def token(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        return auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key") or ""

    @router.get("/v1/apps")
    async def tools_in_chat(request: Request):
        """Menu « / » du chat : les outils installés sur ce PC, pour la clé maître (sans demander le code de double
        authentification : il ne sera demandé qu'au lancement d'une demande)."""
        ctx = shared.plan_ctx({}, request)
        if not ctx.is_admin:
            return {"data": []}
        return {"data": await asyncio.to_thread(chat_tools.available, holder["store"])}

    @router.post("/v1/apps/{tool}/runs")
    async def start_run(tool: str, request: Request):
        """{"message", "session", "model"?} : lance une demande ; la page suit la tâche (GET /v1/apps/runs/{id})."""
        ctx = admin(request)
        body = await json_object(request)
        model = body.get("model")
        present = await asyncio.to_thread(chat_tools.available, holder["store"])  # lit le PC : hors de la boucle
        try:
            run = runs.start(
                tool=tool,
                present=present,
                owner=ctx.identity,
                key=token(request),
                message=str(body.get("message") or ""),
                session=str(body.get("session") or ""),
                model=model if isinstance(model, str) and model else None,
                port=holder["remote"].port,
                claude_model=chat_tools.claude_model_of(holder["gw"].providers),  # clé d'Anthropic dans Lezard IA
            )
        except chat_tools.ChatToolError as e:
            return _err(e.status, e.message)
        request.state.audit = {"detail": {"outil": tool}}
        return chat_tools.public(run)

    @router.get("/v1/apps/runs/{rid}")
    async def get_run(rid: str, request: Request):
        ctx = admin(request)
        try:
            return chat_tools.public(runs.get(rid, ctx.identity))
        except chat_tools.ChatToolError as e:
            return _err(e.status, e.message)

    @router.delete("/v1/apps/runs/{rid}")
    async def stop_run(rid: str, request: Request):
        ctx = admin(request)
        try:
            return chat_tools.public(runs.stop(rid, ctx.identity))
        except chat_tools.ChatToolError as e:
            return _err(e.status, e.message)

    # ----- travail en durée (« /travail 30min <tâche> ») : l'IA travaille seule et se relit, gateway/travail.py -----
    def travail_fail(e: travail.TravailError):
        return _err(e.status, e.message)

    @router.post("/v1/travail")
    async def travail_start(request: Request):
        """{"text": "30min <tâche>", "model"?, "private"?} : lance une séance ; la page la suit (GET /v1/travail/{id}).
        Clé maître : une séance occupe l'IA pendant des heures."""
        ctx = admin(request)
        body = await json_object(request)
        model = body.get("model")
        model = model if isinstance(model, str) and travail.MODEL.match(model) and model != "auto" else None
        ask = travail.gateway_ask(holder["remote"].port, token(request), model, body.get("private") is True)
        try:
            run = sessions.start(owner=ctx.identity, text=str(body.get("text") or ""), ask=ask, model=model)
        except travail.TravailError as e:
            return travail_fail(e)
        request.state.audit = {"detail": {"travail": run["seconds"]}}
        return travail.public(run, full=False)

    @router.get("/v1/travail/last")
    async def travail_last(request: Request):
        """La dernière séance de ce compte (/travail seul) : pour la retrouver après avoir fermé la page."""
        ctx = admin(request)
        try:
            run = sessions.last(ctx.identity)
        except travail.TravailError as e:
            return travail_fail(e)
        return {**travail.public(run, full=False), "report": travail.report(run) if run["status"] == "done" else None}

    @router.get("/v1/travail/{rid}")
    async def travail_get(rid: str, request: Request):
        ctx = admin(request)
        try:
            run = sessions.get(rid, ctx.identity)
        except travail.TravailError as e:
            return travail_fail(e)
        return {**travail.public(run, full=False), "report": travail.report(run) if run["status"] == "done" else None}

    @router.delete("/v1/travail/{rid}")
    async def travail_stop(rid: str, request: Request):
        """Arrêt : la meilleure version obtenue jusque-là est rendue."""
        ctx = admin(request)
        try:
            return travail.public(sessions.stop(rid, ctx.identity), full=False)
        except travail.TravailError as e:
            return travail_fail(e)

    return router
