"""Double authentification de l'administration (20.1) : activation TOTP, codes de secours, sessions.

Hors de ce PC, la clé maître s'échange avec un code contre une session (cookie HttpOnly, SameSite=Strict) ; le
compteur anti force brute (12.3) compte aussi les mauvais codes."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from ..limits import RateLimited
from ..plans import PlanError
from ..totp import COOKIE, AdminTotp, TotpError, session_seconds
from ..validation import json_object

GLOBAL_SCOPE = "totpfail:all"


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    admin = shared.admin
    client_ip = shared.client_ip
    is_local_console = shared.is_local_console
    plan_ctx = shared.plan_ctx
    session_token = shared.session_token

    def totp() -> AdminTotp:
        return holder["totp"]

    def global_max() -> int:
        """Mauvais codes, toutes adresses confondues, par heure : un attaquant qui a la clé maître et beaucoup
        d'adresses IP ne peut pas pour autant essayer le million de codes."""
        return int(os.getenv("TOTP_MAX_FAILURES_PER_HOUR", "60"))

    async def code_of(request: Request, required: bool = True) -> str | None:
        body = await json_object(request, empty={})
        code = body.get("code")
        if code is None and not required:
            return None
        if not isinstance(code, str) or not code.strip() or len(code) > 32:
            raise TotpError(400, "'code' attendu : les 6 chiffres de l'application, ou un code de secours.")
        return code

    async def checked(request: Request, code: str, action) -> object:
        """Vérifie un code avec l'anti force brute (12.3) ; `action(code)` lève TotpError(401) s'il est faux."""
        ip = client_ip(request)
        guarded = not is_local_console(ip)
        auth = holder["auth"]
        now = auth.clock()
        if guarded:
            auth.check(ip)
            if auth.counters.used(GLOBAL_SCOPE, 3600, now) >= global_max():
                raise RateLimited(
                    "Trop de codes de double authentification invalides : réessayez plus tard, ou "
                    "connectez-vous depuis le PC.",
                    600,
                    "auth",
                )
        try:
            return await asyncio.to_thread(action, code)
        except TotpError as e:
            if e.status == 401 and guarded:
                auth.fail(ip)
                auth.counters.add(GLOBAL_SCOPE, 3600, 1, now)
            raise

    def with_cookie(data: dict, request: Request, token: str | None) -> JSONResponse:
        resp = JSONResponse(data, headers={"Cache-Control": "no-store"})
        if token:
            secure = (
                request.url.scheme == "https"
                or holder["remote"].active
                or request.headers.get("x-forwarded-proto", "").lower() == "https"
            )
            resp.set_cookie(
                COOKIE, token, max_age=session_seconds(), httponly=True, secure=secure, samesite="strict", path="/"
            )
        return resp

    def open_session(request: Request, via: str) -> tuple[str, dict]:
        return totp().open_session(
            holder["gw"].plans.master_key, client_ip(request), request.headers.get("user-agent"), via
        )

    @router.get("/v1/admin/totp")
    async def totp_status(request: Request):
        admin(request)
        t = totp()
        return {
            **await asyncio.to_thread(t.status),
            "this_pc": is_local_console(client_ip(request)),
            "sessions": await asyncio.to_thread(t.sessions, session_token(request)),
        }

    @router.post("/v1/admin/totp/setup")
    async def totp_setup(request: Request):
        """Nouveau secret (QR code) ; actif seulement après validation d'un premier code (enable)."""
        admin(request)
        host = request.url.hostname or "gateway"
        return JSONResponse(
            await asyncio.to_thread(totp().begin_setup, f"admin@{host}"), headers={"Cache-Control": "no-store"}
        )

    @router.post("/v1/admin/totp/enable")
    async def totp_enable(request: Request):
        admin(request)
        code = await code_of(request)
        codes = await checked(request, code, totp().enable)
        token = session = None
        if not is_local_console(client_ip(request)):  # à distance : on ne s'enferme pas dehors
            token, session = await asyncio.to_thread(open_session, request, "activation")
        return with_cookie({"enabled": True, "recovery_codes": codes, "session": session}, request, token)

    @router.post("/v1/admin/totp/recovery-codes")
    async def totp_recovery(request: Request):
        """Nouveaux codes de secours (les anciens ne marchent plus) ; demande un code de l'application."""
        admin(request)
        code = await code_of(request)
        codes = await checked(request, code, totp().regenerate_recovery)
        return JSONResponse({"recovery_codes": codes}, headers={"Cache-Control": "no-store"})

    @router.post("/v1/admin/totp/disable")
    async def totp_disable(request: Request):
        """Sur ce PC, la clé maître suffit (comme lancer.ps1 -ResetTotp) ; à distance, un code est demandé en
        plus de la session."""
        admin(request)
        t = totp()
        if not await asyncio.to_thread(t.enabled):
            raise TotpError(409, "La double authentification n'est pas activée.")
        if not is_local_console(client_ip(request)):
            await checked(request, await code_of(request), t.verify)
        await asyncio.to_thread(t.disable)
        resp = JSONResponse(await asyncio.to_thread(t.status))
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @router.post("/v1/admin/session")
    async def open_admin_session(request: Request):
        """Clé maître, ou clé web du compte lezard-ia.fr du propriétaire (mêmes droits), + code (application ou
        secours) -> session d'administration (cookie). Une autre clé web + code -> session « web » : elle marche alors
        depuis Internet, avec ses seuls droits."""
        ctx = plan_ctx({}, request, web_session=False)
        web = not ctx.is_admin and bool(ctx.key) and shared.is_web_key(ctx.key)
        if not ctx.is_admin and not web:
            raise PlanError(403, "Réservé à la clé maître (GATEWAY_MASTER_KEY).")
        t = totp()
        if not await asyncio.to_thread(t.enabled):
            if web:
                raise TotpError(403, "Clé web : la double authentification de ce PC est désactivée.")
            raise TotpError(409, "La double authentification n'est pas activée : la clé maître suffit.")
        via = await checked(request, await code_of(request), t.verify)
        if web:
            via = f"web-{via}"
        token, session = await asyncio.to_thread(open_session, request, via)
        request.state.audit = {"target": session["id"][:12], "detail": {"via": via}}  # 20.2
        out = {"session": session}
        if via.endswith("recovery"):
            left = (await asyncio.to_thread(t.status))["recovery_left"]
            out["warning"] = (
                f"Code de secours utilisé : il n'est plus valable ({left} restant(s)). "
                "Générez-en de nouveaux si besoin."
            )
        return with_cookie(out, request, token)

    @router.delete("/v1/admin/session")
    async def close_admin_session(request: Request):
        ctx = plan_ctx({}, request, web_session=False)
        if not ctx.is_admin and not (ctx.key and shared.is_web_key(ctx.key)):
            raise PlanError(403, "Réservé à la clé maître (GATEWAY_MASTER_KEY).")
        closed = await asyncio.to_thread(totp().close, session_token(request))
        resp = JSONResponse({"closed": closed})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @router.delete("/v1/admin/sessions/{session_id}")
    async def revoke_session(session_id: str, request: Request):
        admin(request)
        if not await asyncio.to_thread(totp().revoke, session_id):
            raise TotpError(404, "Session inconnue ou déjà fermée.")
        return {"revoked": 1}

    @router.delete("/v1/admin/sessions")
    async def revoke_all_sessions(request: Request):
        """Ferme toutes les sessions d'administration, y compris celle-ci (appareil perdu, doute)."""
        admin(request)
        resp = JSONResponse({"revoked": await asyncio.to_thread(totp().revoke)})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    return router
