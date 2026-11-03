"""Conversations enregistrées (19.1) : chaque compte et chaque clé virtuelle ne voit que les siennes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from ..conversations import ConversationError, Conversations, scope_of, validate_messages
from ..validation import json_object


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    plan_ctx = shared.plan_ctx

    def service() -> Conversations:
        return holder["conversations"]

    def who(request: Request):
        ctx = plan_ctx({}, request)
        return ctx, *scope_of(ctx)

    def _int(request: Request, name: str, default: int, low: int, high: int) -> int:
        raw = request.query_params.get(name)
        try:
            value = default if raw is None else int(raw)
        except ValueError:
            raise ConversationError(400, f"« {name} » doit être un entier.") from None
        return max(low, min(high, value))

    @router.get("/v1/conversations")
    async def list_conversations(request: Request):
        _, owner, scope = who(request)
        if request.query_params.get("in") == "messages" and request.query_params.get("q", "").strip():
            return await asyncio.to_thread(  # recherche plein texte, messages compris (19.3)
                service().search,
                owner,
                scope,
                request.query_params["q"],
                _int(request, "limit", 50, 1, 200),
                _int(request, "offset", 0, 0, 10**6),
            )
        return await asyncio.to_thread(
            service().list,
            owner,
            scope,
            request.query_params.get("q", ""),
            _int(request, "limit", 50, 1, 200),
            _int(request, "offset", 0, 0, 10**6),
            request.query_params.get("project") or None,  # conversations d'un projet
        )

    @router.post("/v1/conversations", status_code=201)
    async def create_conversation(request: Request):
        _, owner, scope = who(request)
        body = await json_object(request, empty={})
        messages = validate_messages(body.get("messages", []))
        private = body.get("private", False)
        if not isinstance(private, bool):
            raise ConversationError(400, "« private » doit valoir true ou false.")
        return await asyncio.to_thread(
            service().create, owner, scope, messages, body.get("title") or "", private, body.get("project")
        )

    @router.get("/v1/conversations/{conv_id}")
    async def get_conversation(conv_id: str, request: Request):
        _, owner, scope = who(request)
        return await asyncio.to_thread(service().get, owner, scope, conv_id)

    @router.put("/v1/conversations/{conv_id}/messages")
    async def save_messages(conv_id: str, request: Request):
        _, owner, scope = who(request)
        body = await json_object(request)
        messages = validate_messages(body.get("messages"))
        return await asyncio.to_thread(service().save_messages, owner, scope, conv_id, messages, body.get("revision"))

    @router.patch("/v1/conversations/{conv_id}")
    async def update_conversation(conv_id: str, request: Request):
        _, owner, scope = who(request)
        body = await json_object(request)
        project = body["project"] if "project" in body else ...  # null : retirée de son projet
        return await asyncio.to_thread(
            service().update,
            owner,
            scope,
            conv_id,
            body.get("title"),
            body.get("private"),
            project,
            body.get("starred"),  # favorite
        )

    @router.post("/v1/conversations/{conv_id}/title")
    async def generate_title(conv_id: str, request: Request):
        ctx, owner, scope = who(request)
        return await service().generate_title(holder["gw"], ctx, owner, scope, conv_id)

    # ----- partage par lien, en lecture seule -----
    @router.post("/v1/conversations/{conv_id}/share", status_code=201)
    async def share_conversation(conv_id: str, request: Request):
        _, owner, scope = who(request)
        return await asyncio.to_thread(service().share, owner, scope, conv_id)

    @router.get("/v1/conversations/{conv_id}/share")
    async def share_status(conv_id: str, request: Request):
        _, owner, scope = who(request)
        return await asyncio.to_thread(service().share_status, owner, scope, conv_id)

    @router.delete("/v1/conversations/{conv_id}/share")
    async def unshare_conversation(conv_id: str, request: Request):
        _, owner, scope = who(request)
        return {"deleted": await asyncio.to_thread(service().unshare, owner, scope, conv_id)}

    @router.get("/v1/shared/{token}")
    async def shared_conversation(token: str):
        """Public : le jeton du lien suffit (aucune clé). Rien d'autre que la copie figée n'est lisible ainsi."""
        data = await asyncio.to_thread(service().shared, token)
        return JSONResponse(data, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @router.delete("/v1/conversations/{conv_id}")
    async def delete_conversation(conv_id: str, request: Request):
        _, owner, scope = who(request)
        await asyncio.to_thread(service().delete, owner, scope, conv_id)
        return {"id": conv_id, "deleted": True}

    return router
