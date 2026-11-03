"""Gestionnaires d'erreurs communs : chaque exception métier devient une réponse JSON au format OpenAI."""

from __future__ import annotations

import math

from fastapi import FastAPI
from fastapi import Request

from ..accounts import AccountError
from ..backup import BackupError
from ..cache import CacheError
from ..conversations import ConversationError
from ..compression import CompressionError
from ..documents import DocumentError
from ..effort import EffortError
from ..keys import VirtualKeyError
from ..limits import LimitError, RateLimited
from ..local_gpu import LocalGpuError
from ..permissions import PermissionDenied
from ..plans import PlanError
from ..providers.base import ProviderError
from ..router import RoutingError
from ..totp import AdminSessionRequired, TotpError
from ..validation import RequestError
from ..workspaces import WorkspaceError
from ..core import _err


def register(app: FastAPI) -> None:
    @app.exception_handler(RequestError)
    async def request_error(_: Request, e: RequestError):
        return _err(e.status, e.message, "gateway")

    @app.exception_handler(ProviderError)
    async def provider_error(_: Request, e: ProviderError):
        retry = {"Retry-After": str(max(1, math.ceil(e.retry_after)))} if e.retry_after else None
        return _err(e.status, e.message, e.provider, headers=retry)

    @app.exception_handler(RoutingError)
    async def routing_error(_: Request, e: RoutingError):
        return _err(e.status, e.message, "router")

    @app.exception_handler(EffortError)
    async def effort_error(_: Request, e: EffortError):
        return _err(400, str(e), "router")

    @app.exception_handler(PlanError)
    async def plan_error(_: Request, e: PlanError):
        return _err(e.status, e.message, "plan")

    @app.exception_handler(DocumentError)
    async def document_error(_: Request, e: DocumentError):
        return _err(e.status, e.message, "documents")

    @app.exception_handler(WorkspaceError)
    async def workspace_error(_: Request, e: WorkspaceError):
        return _err(e.status, e.message, "workspace")

    @app.exception_handler(RateLimited)
    async def rate_limited(_: Request, e: RateLimited):
        return _err(429, e.message, f"limits:{e.limit}", headers={"Retry-After": str(e.retry_after)})

    @app.exception_handler(AccountError)
    async def account_error(_: Request, e: AccountError):
        return _err(e.status, e.message, "accounts")

    @app.exception_handler(LocalGpuError)
    async def local_gpu_error(_: Request, e: LocalGpuError):
        return _err(e.status, e.message, "local")

    @app.exception_handler(LimitError)
    async def limit_error(_: Request, e: LimitError):
        return _err(400, str(e), "limits")

    @app.exception_handler(PermissionDenied)
    async def permission_error(_: Request, e: PermissionDenied):
        return _err(e.status, e.message, "permissions")

    @app.exception_handler(VirtualKeyError)
    async def key_error(_: Request, e: VirtualKeyError):
        return _err(e.status, e.message, "keys")

    @app.exception_handler(BackupError)
    async def backup_error(_: Request, e: BackupError):
        return _err(e.status, e.message, "backups")

    @app.exception_handler(ConversationError)
    async def conversation_error(_: Request, e: ConversationError):
        return _err(e.status, e.message, "conversations")

    @app.exception_handler(TotpError)
    async def totp_error(_: Request, e: TotpError):
        # provider "admin_session" : l'interface demande alors le code et rejoue la requête (20.1)
        return _err(e.status, e.message, "admin_session" if isinstance(e, AdminSessionRequired) else "totp")

    @app.exception_handler(CacheError)
    async def cache_error(_: Request, e: CacheError):
        return _err(400, str(e), "cache")

    @app.exception_handler(CompressionError)
    async def compression_error(_: Request, e: CompressionError):
        return _err(400, str(e), "compression")
