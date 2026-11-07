"""Client web (phase 5) : pages HTML, fichiers statiques et /health."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..core import CSP, PREVIEW_CSP, WEB_DIR


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health():
        return {"status": "ok"}

    # ----- client web (phase 5) -----
    def page(name: str) -> FileResponse:
        return FileResponse(
            WEB_DIR / name,
            media_type="text/html",
            headers={
                "Content-Security-Policy": CSP,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-cache",
                # micro pour la dictée du chat (13.9), sur cette origine seulement ; caméra et position jamais
                "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
            },
        )

    @router.get("/", include_in_schema=False)
    async def login_page():
        return page("login.html")

    # Sans « frame » : le cadre principal (shell.html), qui garde chaque page chargée et ne la recharge pas à chaque
    # changement de page. Avec « frame » : la page elle-même, dans son cadre.
    @router.get("/app", include_in_schema=False)
    async def app_page(frame: str | None = None):
        return page("app.html" if frame is not None else "shell.html")

    @router.get("/admin", include_in_schema=False)
    async def admin_page(frame: str | None = None):
        return page("admin.html" if frame is not None else "shell.html")

    @router.get("/models", include_in_schema=False)
    async def models_page(frame: str | None = None):
        return page("models.html" if frame is not None else "shell.html")

    # Marketplace : page à part, hors du cadre de l'application, ouverte aussi aux visiteurs (sans clé)
    @router.get("/marketplace", include_in_schema=False)
    async def marketplace_page():
        return page("marketplace.html")

    # Conversation partagée par lien (lecture seule, sans clé) : la page lit /v1/shared/<jeton>
    @router.get("/partage/{token}", include_in_schema=False)
    async def shared_page(token: str):
        return page("share.html")

    # Panneau d'aperçu du chat : page vide qui reçoit le contenu par postMessage, isolée (core.PREVIEW_CSP)
    @router.get("/preview", include_in_schema=False)
    async def preview_page():
        return FileResponse(
            WEB_DIR / "preview.html",
            media_type="text/html",
            headers={
                "Content-Security-Policy": PREVIEW_CSP,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-cache",
                "Permissions-Policy": "microphone=(), camera=(), geolocation=()",
            },
        )

    # application installée (13.6) : le service worker est servi à la racine pour couvrir tout le site
    @router.get("/sw.js", include_in_schema=False)
    async def service_worker():
        return FileResponse(
            WEB_DIR / "sw.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @router.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(
            WEB_DIR / "manifest.webmanifest",
            media_type="application/manifest+json",
            headers={"Cache-Control": "no-cache"},
        )

    app.mount("/static", RevalidatedStatic(directory=WEB_DIR / "static"), name="static")

    return router


class RevalidatedStatic(StaticFiles):
    """Fichiers statiques toujours revalidés (ETag : 304 sans renvoyer le fichier s'il n'a pas changé). Sans
    Cache-Control, le navigateur gardait des heures un ancien script après une mise à jour de la gateway."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if response.media_type and response.media_type.startswith(("text/html", "image/svg")):
            # page ou image SVG ouverte directement (offline.html, icônes) : mêmes règles que les pages
            response.headers.setdefault("Content-Security-Policy", CSP)
        return response
