"""Taille maximale des corps de requête (tâche 9.1).

Middleware ASGI (et non `@app.middleware("http")`, qui ne voit pas la lecture du corps) :
- Content-Length annoncé au-delà de la limite : 413 immédiat, sans rien lire ;
- corps sans Content-Length (transfert « chunked ») : octets comptés au fil de la lecture, 413 dès le
  dépassement — un client ne peut donc pas contourner la limite en omettant l'en-tête.

Limites : GATEWAY_MAX_BODY_BYTES (défaut 10 Mo) ; l'upload de documents (POST /v1/documents) accepte
DOCUMENT_MAX_BYTES encodé en base64 (+33 %) ou en multipart, plus 1 Mo de marge ; la transcription
(POST /v1/audio/transcriptions) accepte TRANSCRIPTION_MAX_BYTES (25 Mo par défaut), plus 64 Ko de marge.
"""

from __future__ import annotations

import json
import os

from .documents import max_upload_bytes
from .transcription import PATH as TRANSCRIPTION_PATH, max_bytes as transcription_max_bytes

UPLOAD_PATH = "/v1/documents"


def max_body_bytes() -> int:
    return int(os.getenv("GATEWAY_MAX_BODY_BYTES", str(10 * 1024 * 1024)))


def limit_for(method: str, path: str) -> int:
    if method == "POST" and path.rstrip("/") == UPLOAD_PATH:
        return max(max_body_bytes(), max_upload_bytes() * 4 // 3 + 1024 * 1024)
    if method == "POST" and path.rstrip("/") == TRANSCRIPTION_PATH:  # audio (13.9) + marge multipart
        return transcription_max_bytes() + 64 * 1024
    return max_body_bytes()


class BodyTooLarge(Exception):
    pass


def _too_large(exc: BaseException) -> bool:
    """BodyTooLarge, éventuellement enveloppé dans un ExceptionGroup (middlewares HTTP de Starlette/anyio)."""
    if isinstance(exc, BodyTooLarge):
        return True
    subs = getattr(exc, "exceptions", None)
    return bool(subs) and all(_too_large(s) for s in subs)


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = limit_for(scope["method"], scope["path"])
        declared = dict(scope.get("headers") or []).get(b"content-length")
        try:
            if declared is not None and int(declared) > limit:
                return await self._reject(send, limit)
        except ValueError:
            return await self._reject(send, limit, 400, "En-tête Content-Length invalide.")

        received, started = 0, False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge()
            return message

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except Exception as e:
            if not _too_large(e):
                raise
            if not started:  # le corps est lu avant toute réponse : cas normal
                await self._reject(send, limit)

    @staticmethod
    async def _reject(send, limit: int, status: int = 413, message: str | None = None):
        body = json.dumps(
            {
                "error": {
                    "message": message or f"Requête trop volumineuse (maximum {limit} octets).",
                    "type": "invalid_request_error",
                    "code": status,
                    "provider": "gateway",
                }
            },
            ensure_ascii=False,
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
