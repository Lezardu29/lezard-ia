"""Accès distant et partage : tunnels (5.6), adresses et QR code (12.6), autorité locale (12.7),
documentation de l'API (9.6)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse, Response

from ..config import hosted_instance, lan_mode
from ..keys import VirtualKeyError
from ..plans import PlanError
from ..remote import RemoteAccess, TunnelError, stable_url
from ..tls import tls_dir, tls_port
from ..validation import RequestError, json_object
from ..core import _err, lan_urls, tls_info


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    admin = shared.admin
    client_ip = shared.client_ip
    is_local_console = shared.is_local_console
    manager = shared.manager
    start_remote = shared.start_remote

    # ----- accès distant (5.6) -----
    @app.exception_handler(TunnelError)
    async def tunnel_error(_: Request, e: TunnelError):
        return _err(e.status, e.message, "remote")

    @router.get("/v1/admin/remote")
    async def remote_status(request: Request):
        admin(request)
        r: RemoteAccess = holder["remote"]
        return {
            **r.status.to_dict(),
            "available": r.available(),
            "local_port": r.port,
            "require_key": holder["gw"].plans.require_key,
            "anonymous": r.active and holder["remote_anonymous"] and not holder["gw"].plans.require_key,
            "auth": holder["auth"].status(),
            "counters": holder["counters"].status(),
            "lan": {"enabled": lan_mode(), "urls": lan_urls(r.port) if lan_mode() else [], "tls": tls_info()},
        }

    # ----- partage d'un accès en un clic (12.6) -----
    def access_urls(request: Request) -> list[dict]:
        """Adresses où joindre cette gateway, de la plus large à la plus restreinte."""
        r: RemoteAccess = holder["remote"]
        out: list[dict] = []

        def add(kind: str, url: str, label: str, **extra) -> None:
            url = url.rstrip("/")
            if url and all(u["url"] != url for u in out):
                out.append({"kind": kind, "url": url, "label": label, **extra})

        st = r.status
        if st.state == "running" and st.public_url:
            if st.mode == "private":
                add("tailscale", st.public_url, "Tailscale (vos appareils connectés)", stable=True)
            else:
                add("internet", st.public_url, "Internet (de partout)", stable=stable_url(st.provider))
        if lan_mode():
            for url in lan_urls(r.port):
                add("lan", url, "Réseau local (même Wi-Fi)", secure=url.startswith("https://"))
        here = str(request.base_url)
        local = (request.url.hostname or "") in ("127.0.0.1", "localhost", "::1")
        add("local" if local else "current", here, "Ce PC uniquement" if local else "Adresse actuelle")
        return out

    @router.get("/v1/access")
    async def access_info(request: Request):
        manager(request)
        return {
            "urls": access_urls(request),
            "remote_active": holder["remote"].active,
            "lan": lan_mode(),
            "tls": tls_info(),
        }

    @router.get("/ca.crt", include_in_schema=False)
    async def local_ca():
        """Certificat PUBLIC de l'autorité locale (12.7), à installer sur les appareils du réseau. Public par nature ;
        comparer son empreinte avec celle affichée sur le PC avant de lui faire confiance."""
        path = tls_dir() / "ca.crt"
        if not tls_port() or not path.exists():
            raise VirtualKeyError(404, "HTTPS réseau local non activé sur cette gateway.")
        return Response(
            path.read_bytes(),
            media_type="application/x-x509-ca-cert",
            headers={"Content-Disposition": 'attachment; filename="smart-ai-gateway-ca.crt"'},
        )

    def docs_allowed(request: Request) -> bool:
        """Documentation de l'API (9.6) : une carte complète des endpoints, utile à un attaquant. GATEWAY_DOCS :
        `auto` (défaut) = machine locale sans tunnel, coupée sur une instance hébergée ; `public` ; `off`.
        La clé maître y accède toujours pour /openapi.json (outils, génération de clients)."""
        mode = os.getenv("GATEWAY_DOCS", "auto").strip().lower()
        if mode == "public":
            return True
        if mode != "off" and not hosted_instance() and is_local_console(client_ip(request)):
            return True
        if request.headers.get("authorization") or request.headers.get("x-api-key"):
            try:
                return admin(request) is not None
            except PlanError:
                return False
        return False

    @router.get("/openapi.json", include_in_schema=False)
    async def openapi_schema(request: Request):
        if not docs_allowed(request):
            raise VirtualKeyError(404, "Not Found")
        return JSONResponse(app.openapi())

    @router.get("/docs", include_in_schema=False)
    async def api_docs(request: Request):
        if not docs_allowed(request):
            raise VirtualKeyError(404, "Not Found")
        return get_swagger_ui_html(openapi_url="/openapi.json", title="Smart AI Gateway — API")

    @router.post("/v1/access/qr")
    async def access_qr(request: Request):
        """QR code d'un lien de connexion directe pour téléphone : <adresse>/#key=<clé>. La clé est dans le
        fragment (#), que le navigateur n'envoie jamais au serveur : ni journaux, ni proxy, ni tunnel ne la voient.
        Rien n'est conservé ; la réponse n'est pas mise en cache."""
        manager(request)
        body = await json_object(request)
        url, key = body.get("url"), body.get("key")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")) or len(url) > 500:
            raise RequestError("'url' : adresse http(s) attendue.")
        if not isinstance(key, str) or not key or len(key) > 256 or any(c.isspace() for c in key):
            raise RequestError("'key' : clé attendue.")
        try:
            import qrcode
            import qrcode.image.svg
        except ImportError:
            raise RequestError("Génération de QR code indisponible (paquet 'qrcode' absent).", 501)
        link = f"{url.rstrip('/')}/#key={quote(key, safe='')}"
        img = qrcode.make(link, image_factory=qrcode.image.svg.SvgPathFillImage, border=2)
        return JSONResponse(
            {"svg": img.to_string(encoding="unicode"), "link": link}, headers={"Cache-Control": "no-store"}
        )

    @router.post("/v1/admin/remote/start")
    async def remote_start(request: Request):
        admin(request)
        body = await json_object(request, empty={})
        request.state.audit = {
            "target": body.get("provider"),
            "detail": {"mode": body.get("mode"), "allow_anonymous": bool(body.get("allow_anonymous"))},
        }  # 20.2
        status = await start_remote(body.get("provider"), body.get("mode"), bool(body.get("allow_anonymous")))
        request.state.audit["detail"]["public_url"] = status.get("public_url")
        return status

    @router.post("/v1/admin/remote/stop")
    async def remote_stop(request: Request):
        admin(request)
        holder["remote_anonymous"] = False
        return await holder["remote"].stop()

    return router
