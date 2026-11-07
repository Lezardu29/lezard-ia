"""Alertes budgétaires et notifications (6.5)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request

from ..alerts import AlertEngine, AlertError, new_secret, public_rule, validate_rule
from ..validation import json_object
from ..core import _err


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    manager = shared.manager
    plan_ctx = shared.plan_ctx

    # ----- alertes budgétaires & notifications (6.5) -----
    @app.exception_handler(AlertError)
    async def alert_error(_: Request, e: AlertError):
        return _err(e.status, e.message, "alerts")

    def _rule_or_404(owner: str, rule_id: str) -> dict:
        r = holder["store"].get_alert_rule(owner, rule_id)
        if not r:
            raise AlertError(f"Alerte '{rule_id}' introuvable.", 404)
        return r

    async def _check_rule_key(owner: str, data: dict) -> None:
        if data.get("key_id") and not holder["store"].get_key(owner, data["key_id"]):
            raise AlertError(f"Clé '{data['key_id']}' introuvable.", 404)

    @router.get("/v1/alerts")
    async def list_alerts(request: Request):
        ctx = manager(request)
        await holder["gw"].metrics.flush()
        engine: AlertEngine = holder["alerts"]
        rules = holder["store"].list_alert_rules(ctx.identity)
        spent = [(await asyncio.to_thread(engine.rule_spend, r))[3] for r in rules]
        return {"object": "list", "data": [public_rule(r, s) for r, s in zip(rules, spent)]}

    @router.post("/v1/alerts", status_code=201)
    async def create_alert(request: Request):
        ctx = manager(request)
        data = await asyncio.to_thread(validate_rule, await json_object(request))
        await _check_rule_key(ctx.identity, data)
        if data.get("webhook_url"):
            data["webhook_secret"] = new_secret()
        rule = holder["store"].add_alert_rule(ctx.identity, data)
        # 20.2 : jamais l'adresse du webhook (celles de Slack / Discord contiennent leur jeton)
        request.state.audit = {
            "target": rule["id"],
            "detail": {"name": rule.get("name"), "webhook": bool(data.get("webhook_url"))},
        }
        return public_rule(rule)

    @router.patch("/v1/alerts/{rule_id}")
    async def update_alert(rule_id: str, request: Request):
        ctx = manager(request)
        current = _rule_or_404(ctx.identity, rule_id)
        data = await asyncio.to_thread(validate_rule, await json_object(request), True)
        await _check_rule_key(ctx.identity, data)
        if data.get("webhook_url") and not current.get("webhook_secret"):
            data["webhook_secret"] = new_secret()
        return public_rule(holder["store"].update_alert_rule(ctx.identity, rule_id, data))

    @router.delete("/v1/alerts/{rule_id}")
    async def delete_alert(rule_id: str, request: Request):
        ctx = manager(request)
        if not holder["store"].delete_alert_rule(ctx.identity, rule_id):
            raise AlertError(f"Alerte '{rule_id}' introuvable.", 404)
        return {"id": rule_id, "deleted": True}

    @router.post("/v1/alerts/{rule_id}/test")
    async def test_alert(rule_id: str, request: Request):
        ctx = manager(request)
        rule = _rule_or_404(ctx.identity, rule_id)
        if not rule.get("webhook_url"):
            raise AlertError("Cette alerte n'a pas de webhook.")
        event = {
            "event": "budget.test",
            "rule_id": rule_id,
            "rule": rule["name"],
            "title": f"Test de l'alerte « {rule['name']} »",
            "message": "Si vous lisez ceci, le webhook fonctionne.",
            "at": time.time(),
        }
        return {"delivered": await holder["alerts"].send(rule["webhook_url"], rule.get("webhook_secret"), event)}

    def account_itself(request: Request):
        """Notifications du compte (alertes de sécurité comprises, ex. « action d'administration depuis une adresse
        inconnue ») : au compte lui-même. Avant, une clé virtuelle prêtée pouvait les lire et les marquer lues,
        effaçant l'alerte que son propriétaire devait voir."""
        ctx = plan_ctx({}, request)
        if ctx.key:
            raise AlertError(
                "Notifications du compte : avec la clé du compte (ou la clé maître), pas une clé virtuelle.", 403
            )
        return ctx

    @router.get("/v1/notifications")
    async def notifications(request: Request, limit: int = 50):
        ctx = account_itself(request)
        items, unread = holder["store"].list_notifications(ctx.identity, max(1, min(limit, 200)))
        return {"object": "list", "data": items, "unread": unread}

    @router.post("/v1/notifications/read")
    async def notifications_read(request: Request):
        ctx = account_itself(request)
        ids = (await json_object(request, empty={})).get("ids")
        if ids is not None and (
            not isinstance(ids, list)
            or len(ids) > 500
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)
        ):
            raise AlertError("'ids' doit être une liste de 500 entiers maximum.")
        return {"updated": holder["store"].mark_notifications_read(ctx.identity, ids)}

    return router
