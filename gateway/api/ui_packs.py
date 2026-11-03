"""Interfaces personnalisées (ui_packs.py) : import d'un .zip depuis le panneau Apparence, liste, suppression ;
fichiers servis à /ui-packs/<id>/<chemin> (sans clé : une feuille de style ne peut pas envoyer d'en-tête
d'autorisation ; l'identifiant est aléatoire) ; modèle à /ui-packs/modele.zip."""

from __future__ import annotations

import asyncio
import secrets
import time
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import Response

from .. import ui_packs
from ..core import WEB_DIR, _err
from ..ui_packs import PackError

TEMPLATE_DIR = WEB_DIR / "ui-pack-modele"
GUIDE = WEB_DIR / "static" / "guide-interface.md"


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    plan_ctx = shared.plan_ctx

    @app.exception_handler(PackError)
    async def pack_error(_: Request, e: PackError):
        return _err(e.status, e.message, "ui_packs")

    def owner(request: Request) -> str:
        ctx = plan_ctx({}, request)
        if ctx.identity.startswith("ip:"):
            raise PackError(403, "Connectez-vous avec une clé pour importer une interface.")
        return ctx.identity

    @router.get("/v1/ui-packs")
    async def list_packs(request: Request):
        return {"object": "list", "data": holder["store"].list_ui_packs(owner(request))}

    @router.post("/v1/ui-packs", status_code=201)
    async def upload_pack(request: Request):
        """Multipart (champ « file ») ou corps brut (application/zip)."""
        who = owner(request)
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            f = (await request.form()).get("file")
            if f is None or not hasattr(f, "read"):
                raise PackError(400, "Champ « file » manquant.")
            data = await f.read()
        else:
            data = await request.body()
        manifest, files = await asyncio.to_thread(ui_packs.read_pack, data)
        store = holder["store"]
        if len(store.list_ui_packs(who)) >= ui_packs.MAX_PACKS:
            raise PackError(409, f"{ui_packs.MAX_PACKS} interfaces au plus : supprimez-en une d'abord.")
        pack_id = f"pk_{secrets.token_hex(12)}"
        store.add_ui_pack(who, pack_id, manifest, files, time.time())
        return next(p for p in store.list_ui_packs(who) if p["id"] == pack_id)

    @router.delete("/v1/ui-packs/{pack_id}")
    async def delete_pack(pack_id: str, request: Request):
        if not holder["store"].delete_ui_pack(owner(request), pack_id):
            raise PackError(404, "Interface introuvable.")
        return {"id": pack_id, "deleted": True}

    @router.get("/ui-packs/modele.zip", include_in_schema=False)
    async def template():
        data = await asyncio.to_thread(ui_packs.template_zip, TEMPLATE_DIR, GUIDE.read_bytes())
        return Response(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="lezard-interface-modele.zip"'},
        )

    @router.get("/ui-packs/{pack_id}/{path:path}", include_in_schema=False)
    async def pack_file(pack_id: str, path: str):
        try:
            kind = ui_packs.content_type(path)
        except KeyError:
            kind = None
        data = holder["store"].ui_pack_file(pack_id, path) if kind and ui_packs.valid_id(pack_id) else None
        if data is None:
            return Response("Introuvable", status_code=404, media_type="text/plain")
        return Response(
            data,
            media_type=kind,
            headers={
                "Content-Security-Policy": ui_packs.FILE_CSP,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-cache",
            },
        )

    return router
