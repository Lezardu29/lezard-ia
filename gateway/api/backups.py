"""Sauvegardes locales (18.2) et redémarrage (18.1) : clé maître ; restaurer, exporter la clé de chiffrement et
redémarrer depuis CE PC seulement."""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from ..backup import INTERVAL, KEEP_DAILY, KEEP_OTHER, KEEP_WEEKLY, BackupError, BackupManager, auto_enabled
from ..crypto import Cipher, CryptoError
from ..validation import json_object


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    admin = shared.admin
    client_ip = shared.client_ip
    is_local_console = shared.is_local_console

    def this_pc_only(request: Request, action: str) -> None:
        """Une clé maître volée (tunnel, réseau local) ne doit ni ramener la base à un état choisi par l'attaquant
        (clé révoquée redevenue valide…), ni emporter la clé qui déchiffre les clés API des fournisseurs."""
        if not is_local_console(client_ip(request)):
            raise BackupError(
                403,
                f"{action} : seulement depuis ce PC (http://127.0.0.1), accès distant arrêté. "
                "Autre possibilité : python -m gateway.backup sur le PC.",
            )

    @router.get("/v1/admin/backups")
    async def list_backups(request: Request):
        admin(request)
        m: BackupManager = holder["backups"]
        cipher: Cipher = holder["cipher"]
        store = holder["store"]
        return {
            "available": m.available,
            "auto": auto_enabled(),
            "interval_hours": INTERVAL // 3600,
            "keep": {"daily": KEEP_DAILY, "weekly": KEEP_WEEKLY, "other": KEEP_OTHER},
            "directory": str(m.dir) if m.dir else None,
            "same_disk": m.same_disk(),
            "data": await asyncio.to_thread(m.list),
            "pending": m.pending(),
            "last_restore": store.get_setting("last_restore"),
            "key": {
                "fingerprint": cipher.fingerprint,
                "source": "env" if os.getenv("GATEWAY_ENCRYPTION_KEY") else "file",
                "exported": store.get_setting("key_export"),
            },
            "this_pc": is_local_console(client_ip(request)),
            # lancée par un superviseur (lancer.ps1, service) : redémarrage possible depuis l'interface (18.1)
            "can_restart": callable(getattr(app.state, "request_restart", None)),
        }

    @router.post("/v1/admin/backups", status_code=201)
    async def create_backup(request: Request):
        admin(request)
        made = await asyncio.to_thread(holder["backups"].create, "manuelle")
        request.state.audit = {"target": made["name"]}  # 20.2
        return made

    @router.get("/v1/admin/backups/{name}")
    async def inspect_backup(name: str, request: Request):
        admin(request)
        return await asyncio.to_thread(holder["backups"].inspect, name, holder["cipher"])

    @router.post("/v1/admin/backups/{name}/restore")
    async def restore_backup(name: str, request: Request):
        admin(request)
        this_pc_only(request, "Restauration")
        body = await json_object(request)
        if body.get("confirm") != name:  # confirmation explicite : le nom recopié
            raise BackupError(400, "Confirmez en renvoyant le nom de la sauvegarde dans « confirm ».")
        return await asyncio.to_thread(holder["backups"].stage_restore, name, holder["cipher"])

    @router.delete("/v1/admin/backups/restore")
    async def cancel_restore(request: Request):
        admin(request)
        return {"cancelled": holder["backups"].cancel_restore()}

    @router.post("/v1/admin/encryption-key/export")
    async def export_key(request: Request):
        admin(request)
        this_pc_only(request, "Export de la clé de chiffrement")
        body = await json_object(request)
        passphrase = body.get("passphrase")
        if not isinstance(passphrase, str):
            raise BackupError(400, "« passphrase » (texte) requis.")
        try:
            data = await asyncio.to_thread(holder["cipher"].export_key, passphrase)
        except CryptoError as e:
            raise BackupError(400, str(e)) from e
        holder["store"].set_setting("key_export", {"at": time.time(), "fingerprint": data["fingerprint"]})
        return JSONResponse(
            data,
            headers={
                "Content-Disposition": f'attachment; filename="cle-chiffrement-{data["fingerprint"]}.json"',
                "Cache-Control": "no-store",
            },
        )

    @router.post("/v1/admin/restart")
    async def restart(request: Request):
        """Redémarrage propre (après une restauration, 18.2) : la gateway s'arrête avec le code 3 et son
        superviseur la relance. Réponse envoyée d'abord, arrêt une demi-seconde plus tard."""
        admin(request)
        this_pc_only(request, "Redémarrage")
        hook = getattr(app.state, "request_restart", None)
        if not callable(hook):
            raise BackupError(
                409, "Gateway lancée sans superviseur (ni lancer.ps1, ni service) : redémarrez-la vous-même."
            )
        asyncio.get_running_loop().call_later(0.5, hook)
        return {"restarting": True}

    return router
