"""Compte de l'appelant : /v1/me, préférences (5.3)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request

from .. import __version__, updates
from ..conversations import retention_days, storable
from ..config import hosted_instance
from ..edition import edition
from ..plans import FEATURES, PlanContext
from ..preferences import VALIDATORS as PREF_VALIDATORS, PreferenceError
from ..totp import TotpError
from ..validation import json_object
from ..core import _err


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    plan_ctx = shared.plan_ctx

    @router.get("/v1/me")
    async def me(request: Request):
        """Qui suis-je ? (valide une clé côté client, ex. page de connexion 5.1)."""
        ctx = plan_ctx({}, request)
        kind = "virtual_key" if ctx.key else "account" if ctx.can_manage else "anonymous"
        out = {
            "type": kind,
            "plan": ctx.plan,
            "edition": edition(),  # offre de l'installation (29.1) : gratuit | pro
            "features": FEATURES[ctx.plan],
            "can_manage_keys": ctx.can_manage,
            "is_admin": ctx.is_admin,
            "key": ctx.key,
        }
        if ctx.is_admin:
            out["version"] = __version__  # clé maître seulement (15.4)
            last = holder["store"].get_setting("update_check") or {}
            if last.get("available"):  # nouvelle version signalée (18.3)
                out["update"] = {
                    "available": True,
                    "behind": last.get("behind"),
                    "version": last.get("version"),  # proposée au lancement (bandeau), jamais installée seule
                    "installable": updates.installer_mode(),  # installateur : d'un clic ; copie git : lancer.ps1 -Update
                }
            # double authentification (20.1) : faut-il un code pour administrer d'ici ? session ouverte ?
            required = shared.admin_session_required(request)
            try:
                active = shared.admin_session(request) is not None
            except TotpError:
                active = False
            out["admin_session"] = {
                "totp": holder["totp"].enabled(),
                "required": required,
                "active": active,
                "blocked": shared.remote_admin_blocked(request),  # depuis Internet sans double authentification
            }
        # conversations enregistrées sur la gateway (19.1) ; sinon, dans le navigateur seulement
        out["conversations"] = {"stored": storable(ctx), "retention_days": retention_days() or None}
        if ctx.key:
            out["limits"] = ctx.limits or {}
            out["usage"] = holder["gw"].limiter.status(ctx.key["id"], ctx.limits)
            out["key"]["expires_at"] = (holder["store"].get_key(ctx.identity, ctx.key["id"]) or {}).get("expires_at")
            # restrictions de routage utiles à l'interface (5.4) : modes, effort max, multi-agents, hébergement
            perms = ctx.permissions or {}
            out["routing_rights"] = {
                k: perms[k] for k in ("modes", "max_effort", "orchestration", "hosting") if k in perms
            }
        # 7.4 : instance hébergée ; ponts vers les modèles locaux possibles pour ce compte ?
        out["hosting"] = {
            "instance": "managed" if hosted_instance() else "self-hosted",
            "bridges": ctx.can_manage and not ctx.key,
        }
        return out

    # ----- préférences : thème graphique (5.3) -----
    def pref_scope(ctx: PlanContext) -> str:
        return f"key:{ctx.key['id']}" if ctx.key else "account"

    def _pref_name(name: str) -> str:
        if name not in PREF_VALIDATORS:
            raise PreferenceError(f"Préférence inconnue '{name}'. Valeurs : {', '.join(PREF_VALIDATORS)}.")
        return name

    @router.get("/v1/preferences/{name}")
    async def get_pref(name: str, request: Request):
        ctx = plan_ctx({}, request)
        found = holder["store"].get_pref(ctx.identity, pref_scope(ctx), _pref_name(name))
        return {name: found[0] if found else None, "updated_at": found[1] if found else None}

    @router.put("/v1/preferences/{name}")
    async def put_pref(name: str, request: Request):
        ctx = plan_ctx({}, request)
        _pref_name(name)
        if ctx.identity.startswith("ip:"):
            raise PreferenceError("Connectez-vous avec une clé pour enregistrer vos préférences.")
        value = PREF_VALIDATORS[name](await json_object(request))
        updated = holder["store"].set_pref(ctx.identity, pref_scope(ctx), name, value)
        return {name: value, "updated_at": updated}

    @router.delete("/v1/preferences/{name}")
    async def delete_pref(name: str, request: Request):
        ctx = plan_ctx({}, request)
        return {"deleted": holder["store"].delete_pref(ctx.identity, pref_scope(ctx), _pref_name(name))}

    @app.exception_handler(PreferenceError)
    async def preference_error(_: Request, e: PreferenceError):
        return _err(400, str(e), "preferences")

    return router
