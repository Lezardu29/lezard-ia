"""Version installée et mises à jour disponibles (18.3) — clé maître. Installation Windows : mise à jour signée
téléchargée, vérifiée et installée sur demande, ou retour à la version précédente (32.3)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request

from ..core import _err
from ..updates import UpdateChecker, UpdateError
from ..validation import json_object


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    admin = shared.admin

    def checker() -> UpdateChecker:
        return holder["updates"]

    @router.get("/v1/admin/update")
    async def update_status(request: Request):
        admin(request)
        return checker().status()

    @router.post("/v1/admin/update/check")
    async def update_check(request: Request):
        """Vérification immédiate, à la demande (même si la vérification régulière est désactivée)."""
        admin(request)
        await asyncio.to_thread(checker().check)
        return checker().status()

    @router.post("/v1/admin/update/install")
    async def update_install(request: Request):
        """{"target": "latest" | "previous"} : installe la version signée vérifiée par la dernière vérification."""
        admin(request)
        target = (await json_object(request)).get("target", "latest")
        if target not in ("latest", "previous"):
            return _err(400, "'target' : « latest » (mise à jour) ou « previous » (retour à la version précédente).")
        try:
            result = await asyncio.to_thread(checker().install, target)
        except UpdateError as e:
            return _err(e.status, e.message)
        request.state.audit = {"detail": {"version": result["installing"], "cible": target}}
        return result

    return router
