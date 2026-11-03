"""Ponts vers les modèles locaux (7.4) : gestion des ponts et relais WebSocket de l'agent."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from types import SimpleNamespace

import httpx
from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect

from ..bridge import (
    BridgeError,
    BridgeSession,
    CLOSE_RATE_LIMITED,
    CLOSE_UNAUTHORIZED,
    TOKEN_PREFIX,
    base_url as bridge_base_url,
    new_token,
    token_hash,
    validate_name,
)
from ..config import hosted_instance
from ..limits import RateLimited
from ..store import DocumentStore
from ..validation import json_object
from ..core import Gateway, _err, log


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    client_ip = shared.client_ip
    is_local_console = shared.is_local_console
    manager = shared.manager

    # ----- ponts vers les modèles locaux (7.4) -----
    @app.exception_handler(BridgeError)
    async def bridge_error(_: Request, e: BridgeError):
        return _err(e.status, e.message, "bridge")

    def public_bridge(b: dict) -> dict:
        online = holder["hub"].online(b["name"])
        models = holder["gw"].discovered.get(b["name"]) if online else None
        return {
            "id": b["id"],
            "name": b["name"],
            "token_prefix": b["token_prefix"],
            "online": online,
            "models": models if models is not None else b["models"],
            "created_at": b["created_at"],
            "last_seen_at": b["last_seen_at"],
        }

    def public_origin(request: Request) -> str:
        return (os.getenv("GATEWAY_PUBLIC_URL") or str(request.base_url)).rstrip("/")

    @router.get("/v1/bridges")
    async def list_bridges(request: Request):
        ctx = manager(request)
        return {
            "object": "list",
            "allowed": True,
            "instance": "managed" if hosted_instance() else "self-hosted",
            "data": [public_bridge(b) for b in holder["store"].list_bridges(ctx.identity)],
        }

    @router.post("/v1/bridges", status_code=201)
    async def create_bridge(request: Request):
        ctx = manager(request)
        data = await json_object(request, empty={})
        store: DocumentStore = holder["store"]
        if len(store.list_bridges(ctx.identity)) >= int(os.getenv("BRIDGE_MAX_PER_ACCOUNT", "5")):
            raise BridgeError(409, "Nombre maximal de ponts atteint pour ce compte : supprimez-en un.")
        gw: Gateway = holder["gw"]
        name = data.get("name")
        reserved = {n for n in gw.providers if not store.bridge_name_taken(n)} | {
            p.lower() for p in ("auto", "bridge", "gateway")
        }
        validate_name(name, reserved)
        if store.bridge_name_taken(name):
            raise BridgeError(409, f"Le nom « {name} » est déjà utilisé sur cette instance.")
        token = new_token()
        try:
            b = store.create_bridge(ctx.identity, name, token_hash(token), token[: len(TOKEN_PREFIX) + 6])
        except sqlite3.IntegrityError:
            raise BridgeError(409, f"Le nom « {name} » est déjà utilisé sur cette instance.")
        gw.add_bridge(name, ctx.identity, holder["bridge_client"])
        request.state.audit = {"target": b["id"], "detail": {"name": name}}  # 20.2
        origin = public_origin(request)
        return {
            **public_bridge(b),
            "token": token,
            "command": f"python -m gateway.bridge_agent --gateway {origin} --token {token}",
            "docker": (
                f"docker run -d --restart unless-stopped --add-host=host.docker.internal:host-gateway "
                f"-e GATEWAY_URL={origin} -e GATEWAY_BRIDGE_TOKEN={token} "
                "-e BRIDGE_TARGET=http://host.docker.internal:11434/v1 "
                "smart-ai-gateway python -m gateway.bridge_agent"
            ),
        }

    @router.delete("/v1/bridges/{bridge_id}")
    async def delete_bridge(bridge_id: str, request: Request):
        ctx = manager(request)
        b = holder["store"].get_bridge(ctx.identity, bridge_id)
        if not b or not holder["store"].delete_bridge(ctx.identity, bridge_id):
            raise BridgeError(404, f"Pont '{bridge_id}' introuvable.")
        await holder["hub"].kick(b["name"])
        holder["gw"].remove_bridge(b["name"])
        return {"id": bridge_id, "deleted": True}

    @router.websocket("/v1/bridge/connect")
    async def bridge_connect(ws: WebSocket):
        """Connexion sortante de l'agent de pont (jeton gwb_… en Authorization)."""
        auth = ws.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        store: DocumentStore = holder["store"]
        ip = client_ip(ws)
        guarded = not is_local_console(ip)
        try:
            if guarded:
                holder["auth"].check(ip)  # force brute (12.3) : l'agent réessaiera plus tard
        except RateLimited:
            await ws.accept()
            await ws.close(code=CLOSE_RATE_LIMITED)
            return
        bridge = store.bridge_by_token(token_hash(token)) if token.startswith(TOKEN_PREFIX) else None
        await ws.accept()  # accepter puis fermer avec un code : l'agent sait pourquoi il est refusé
        if not bridge:
            if token and guarded:
                holder["auth"].fail(ip)
            await ws.close(code=CLOSE_UNAUTHORIZED)
            return
        gw: Gateway = holder["gw"]
        name = bridge["name"]
        if name not in gw.providers:
            gw.add_bridge(name, bridge["owner"], holder["bridge_client"])
        session = BridgeSession(ws, bridge)
        await holder["hub"].attach(session)
        reader = asyncio.create_task(session.run())
        log.info("Pont « %s » connecté.", name)
        try:
            r = await holder["bridge_client"].get(bridge_base_url(name) + "/models", timeout=30)
            models = [m["id"] for m in r.json().get("data", [])] if r.status_code == 200 else []
            if models:
                gw.discovered[name] = models
            store.touch_bridge(bridge["id"], models)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("Pont « %s » : modèles locaux illisibles (%s).", name, e)
        try:
            await reader
        except WebSocketDisconnect:
            pass
        finally:
            holder["hub"].detach(session)
            if not holder["hub"].online(name):
                gw.discovered.pop(name, None)
            store.touch_bridge(bridge["id"])
            log.info("Pont « %s » déconnecté.", name)

    return router
