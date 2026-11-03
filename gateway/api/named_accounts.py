"""Comptes nommés (20.3) — clé maître : liste, création (jeton montré une fois), modification, désactivation,
nouveau jeton, suppression. Les comptes de GATEWAY_KEYS sont listés en lecture seule (sans leur jeton)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request

from ..accounts import Accounts
from ..plans import account_identity
from ..validation import json_object


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    admin = shared.admin

    def service() -> Accounts:
        return holder["accounts"]

    def env_accounts() -> list[dict]:
        plans = holder["gw"].plans
        return [
            {"identity": account_identity(t), "plan": p, "source": "env", "name": "GATEWAY_KEYS (.env)"}
            for t, p in plans.keys.items()
            if t != plans.master_key
        ]

    @router.get("/v1/admin/accounts")
    async def list_accounts(request: Request):
        admin(request)
        return {"object": "list", "data": await asyncio.to_thread(service().list), "env": env_accounts()}

    @router.post("/v1/admin/accounts", status_code=201)
    async def create_account(request: Request):
        admin(request)
        body = await json_object(request)
        account, token = await asyncio.to_thread(service().create, body.get("name"), body.get("plan", "free"))
        request.state.audit = {"target": account["name"], "detail": {"plan": account["plan"]}}
        return {**account, "token": token}

    @router.patch("/v1/admin/accounts/{account_id}")
    async def update_account(account_id: str, request: Request):
        admin(request)
        body = await json_object(request)
        account = await asyncio.to_thread(service().update, account_id, body)
        request.state.audit = {
            "target": account["name"],
            "detail": {k: body[k] for k in ("plan", "status") if k in body},
        }
        return account

    @router.post("/v1/admin/accounts/{account_id}/rotate")
    async def rotate_account(account_id: str, request: Request):
        admin(request)
        account, token = await asyncio.to_thread(service().rotate, account_id)
        request.state.audit = {"target": account["name"]}
        return {**account, "token": token}

    @router.delete("/v1/admin/accounts/{account_id}")
    async def delete_account(account_id: str, request: Request):
        admin(request)
        name = (await asyncio.to_thread(service()._get, account_id))["name"]
        revoked = await asyncio.to_thread(service().delete, account_id)
        request.state.audit = {"target": name, "detail": {"keys_revoked": revoked}}
        return {"deleted": True, "keys_revoked": revoked}

    return router
