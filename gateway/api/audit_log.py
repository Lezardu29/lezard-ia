"""Journal d'audit (20.2) : consultation filtrée, export CSV, conservation — clé maître."""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from ..audit import CATEGORIES, LABELS, capture_days, csv_rows, retention_days, SETTING
from ..crypto import CryptoError
from ..keys import VirtualKeyError
from ..validation import json_object

KINDS = ("master", "account", "key", "system")
EXPORT_MAX = 100_000


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    admin = shared.admin

    def filters(action, actor_kind, ip, result, q, since, until) -> dict:
        if actor_kind and actor_kind not in KINDS:
            raise VirtualKeyError(400, f"actor_kind : {', '.join(KINDS)}.")
        if result and result not in ("ok", "fail"):
            raise VirtualKeyError(400, "result : ok ou fail.")
        if action and not (action in LABELS or action in CATEGORIES):
            raise VirtualKeyError(400, "Action inconnue.")
        return {
            "action": action,
            "actor_kind": actor_kind,
            "ip": ip or None,
            "result": result,
            "q": (q or "").strip()[:100] or None,
            "since": since,
            "until": until,
        }

    @router.get("/v1/admin/audit")
    async def list_audit(
        request: Request,
        action: str | None = None,
        actor_kind: str | None = None,
        ip: str | None = None,
        result: str | None = None,
        q: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        admin(request)
        if not 1 <= limit <= 500 or offset < 0:
            raise VirtualKeyError(400, "'limit' entre 1 et 500, 'offset' >= 0.")
        rows, total = await asyncio.to_thread(
            holder["store"].query_audit,
            limit=limit,
            offset=offset,
            **filters(action, actor_kind, ip, result, q, since, until),
        )
        for r in rows:
            r["label"] = LABELS.get(r["action"], r["action"])
        return {
            "object": "list",
            "data": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "actions": [{"id": a, "label": LABELS[a]} for a in sorted(LABELS)],
            "categories": CATEGORIES,
            "retention_days": retention_days(holder["store"]) or None,
            "capture_days": capture_days() or None,
        }

    @router.get("/v1/admin/audit/{audit_id}/capture")
    async def audit_capture(audit_id: int, request: Request):
        """Capture d'écran d'une action du contrôle du PC (21.4), déchiffrée pour la clé maître seulement."""
        admin(request)
        token = await asyncio.to_thread(holder["store"].get_capture, audit_id)
        if token is None:
            raise VirtualKeyError(404, "Aucune capture pour cette entrée (jamais prise, ou déjà purgée).")
        try:
            uri = holder["cipher"].decrypt(token, f"audit-capture:{audit_id}")
            head, data = uri.split(",", 1)
            media = head.removeprefix("data:").split(";")[0]
            raw = base64.b64decode(data)
        except (CryptoError, ValueError, binascii.Error):
            raise VirtualKeyError(500, "Capture illisible (clé de chiffrement changée ?).") from None
        if media not in ("image/jpeg", "image/png"):
            raise VirtualKeyError(500, "Capture d'un format inattendu.")
        return Response(raw, media_type=media, headers={"Cache-Control": "no-store"})

    @router.get("/v1/admin/audit/export")
    async def export_audit(
        request: Request,
        action: str | None = None,
        actor_kind: str | None = None,
        ip: str | None = None,
        result: str | None = None,
        q: str | None = None,
        since: float | None = None,
        until: float | None = None,
    ):
        admin(request)
        rows, _ = await asyncio.to_thread(
            holder["store"].query_audit, limit=EXPORT_MAX, **filters(action, actor_kind, ip, result, q, since, until)
        )
        name = time.strftime("journal-audit-%Y%m%d-%H%M.csv", time.gmtime())
        return StreamingResponse(
            csv_rows(rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
        )

    @router.put("/v1/admin/audit/settings")
    async def audit_settings(request: Request):
        """Conservation en jours (0 = illimitée) ; remplace AUDIT_RETENTION_DAYS."""
        admin(request)
        body = await json_object(request)
        days = body.get("retention_days")
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 3650:
            raise VirtualKeyError(400, "'retention_days' : nombre de jours entre 0 (illimitée) et 3650.")
        holder["store"].set_setting(SETTING, {"retention_days": days})
        request.state.audit = {"detail": {"retention_days": days}}
        return {"retention_days": days or None}

    return router
