"""Clés virtuelles (4.1) : création, statut, rotation, opérations groupées, usage et historique."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request

from ..keys import VirtualKeyError, display_prefix, generate_secret, hash_secret, parse_expiry, validate_key
from ..plans import PlanContext
from ..validation import json_object


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    manager = shared.manager

    @router.get("/v1/keys/{key_id}/usage")
    async def key_usage(key_id: str, request: Request):
        ctx = manager(request)
        key = _key_or_404(ctx.identity, key_id)
        return {"id": key_id, "limits": key["limits"], **holder["gw"].limiter.status(key_id, key["limits"])}

    # ----- clés virtuelles (4.1) -----
    def _key_or_404(owner: str, key_id: str) -> dict:
        k = holder["store"].get_key(owner, key_id)
        if not k:
            raise VirtualKeyError(404, f"Clé '{key_id}' introuvable.")
        return k

    def _check_key_project(owner: str, data: dict) -> None:
        if data.get("project_id") and not holder["store"].get_project(owner, data["project_id"]):
            raise VirtualKeyError(404, f"Projet '{data['project_id']}' introuvable.")

    def _check_pc_grant(ctx: PlanContext, request: Request, data: dict) -> None:
        """Contrôle du PC : seule la clé maître le délègue, avec la double authentification depuis Internet (sinon
        un autre compte, ou la clé maître seule, piloterait le PC hôte par une clé virtuelle)."""
        if (data.get("permissions") or {}).get("tools") != "pc":
            return
        if not ctx.is_admin:
            raise VirtualKeyError(403, "Permission « pc » (contrôle du PC) : réservée à la clé maître.")
        shared.admin_session(request)

    @router.post("/v1/keys", status_code=201)
    async def create_key(request: Request):
        ctx = manager(request)
        data = validate_key(await json_object(request), ctx.base_plan or ctx.plan)
        _check_key_project(ctx.identity, data)
        _check_pc_grant(ctx, request, data)
        secret = generate_secret()
        key = holder["store"].add_key(ctx.identity, {**data, "key_prefix": display_prefix(secret)}, hash_secret(secret))
        holder["store"].log_key_event(ctx.identity, key["id"], "created")
        request.state.audit = {"target": key["id"], "detail": {"name": key["name"], "plan": key["plan"]}}  # 20.2
        return {**key, "key": secret, "warning": "Copiez cette clé maintenant : elle ne sera plus affichée."}

    # ----- révocation / suspension (4.4) -----
    async def _reason(request: Request) -> dict:
        body = await json_object(request, empty={})
        if body.get("reason") is not None and not isinstance(body["reason"], str):
            raise VirtualKeyError(400, "'reason' doit être une chaîne.")
        return body

    def _change_status(ctx: PlanContext, key: dict, action: str, body: dict) -> dict:
        reason = (body.get("reason") or "").strip()[:300] or None
        if key["status"] == "revoked":
            raise VirtualKeyError(409, "Clé déjà révoquée : la révocation est définitive.")
        if action == "revoke":
            return holder["store"].set_key_status(ctx.identity, key["id"], "revoked", reason)
        if action == "suspend":
            until = parse_expiry(body.get("until"), body.get("duration"))
            return holder["store"].set_key_status(ctx.identity, key["id"], "suspended", reason, until)
        if key["status"] == "active":
            return key
        return holder["store"].set_key_status(ctx.identity, key["id"], "active", reason)

    for _action in ("revoke", "suspend", "resume"):

        async def _status_endpoint(key_id: str, request: Request, _a: str = _action):
            ctx = manager(request)
            return _change_status(ctx, _key_or_404(ctx.identity, key_id), _a, await _reason(request))

        router.post(f"/v1/keys/{{key_id}}/{_action}", name=f"{_action}_key")(_status_endpoint)

    @router.post("/v1/keys/bulk")
    async def bulk_keys(request: Request):
        """Réaction à incident : {action: suspend|resume|revoke, ids?: [...] (défaut : toutes), reason}."""
        ctx = manager(request)
        body = await _reason(request)
        if body.get("action") not in ("suspend", "resume", "revoke"):
            raise VirtualKeyError(400, "'action' doit valoir suspend, resume ou revoke.")
        keys = holder["store"].list_keys(ctx.identity)
        if body.get("ids") is not None:
            if not isinstance(body["ids"], list) or not all(isinstance(i, str) for i in body["ids"]):
                raise VirtualKeyError(400, "'ids' doit être une liste d'identifiants de clés.")
            wanted = set(body["ids"])
            missing = wanted - {k["id"] for k in keys}
            if missing:
                raise VirtualKeyError(404, f"Clés introuvables : {', '.join(sorted(missing))}.")
            keys = [k for k in keys if k["id"] in wanted]
        done = [_change_status(ctx, k, body["action"], body)["id"] for k in keys if k["status"] != "revoked"]
        request.state.audit = {"target": body["action"], "detail": {"ids": done[:50], "count": len(done)}}
        return {"action": body["action"], "updated": done, "count": len(done)}

    @router.get("/v1/keys/{key_id}/events")
    async def key_events(key_id: str, request: Request):
        ctx = manager(request)
        _key_or_404(ctx.identity, key_id)
        return {"object": "list", "data": holder["store"].key_events(ctx.identity, key_id)}

    @router.get("/v1/keys")
    async def list_keys(request: Request, with_usage: bool = False):
        keys = holder["store"].list_keys(manager(request).identity)
        if with_usage:  # jauges de quotas du tableau d'admin (6.3)
            limiter = holder["gw"].limiter
            keys = [{**k, "usage": limiter.status(k["id"], k["limits"])} for k in keys]
        return {"object": "list", "data": keys}

    @router.get("/v1/keys/{key_id}")
    async def get_key(key_id: str, request: Request):
        return _key_or_404(manager(request).identity, key_id)

    @router.patch("/v1/keys/{key_id}")
    async def update_key(key_id: str, request: Request):
        ctx = manager(request)
        _key_or_404(ctx.identity, key_id)
        data = validate_key(await json_object(request), ctx.base_plan or ctx.plan, partial=True)
        _check_key_project(ctx.identity, data)
        _check_pc_grant(ctx, request, data)
        return holder["store"].update_key(ctx.identity, key_id, data)

    @router.post("/v1/keys/{key_id}/rotate")
    async def rotate_key(key_id: str, request: Request):
        ctx = manager(request)
        _key_or_404(ctx.identity, key_id)
        secret = generate_secret()
        key = holder["store"].update_key(
            ctx.identity, key_id, {"key_prefix": display_prefix(secret)}, key_hash=hash_secret(secret)
        )
        holder["store"].log_key_event(ctx.identity, key_id, "rotated")
        return {**key, "key": secret, "warning": "L'ancienne clé ne fonctionne plus."}

    @router.delete("/v1/keys/{key_id}")
    async def delete_key(key_id: str, request: Request):
        ctx = manager(request)
        if not holder["store"].delete_key(ctx.identity, key_id):
            raise VirtualKeyError(404, f"Clé '{key_id}' introuvable.")
        holder["store"].log_key_event(ctx.identity, key_id, "deleted")
        holder["store"].delete_conversations(ctx.identity, f"key:{key_id}")  # ses conversations aussi (19.1)
        return {"id": key_id, "deleted": True}

    return router
