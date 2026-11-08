"""Outils de la marketplace utilisés depuis le chat (« /<commande> », gateway/chat_tools.py). Ils agissent sur ce PC :
réservés à la clé maître (à distance, avec la double authentification de l'administration, comme le contrôle du PC)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request

from .. import chat_tools
from ..core import _err
from ..validation import json_object


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    admin = shared.admin
    runs = chat_tools.Runs()

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

    return router
