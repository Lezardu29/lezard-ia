"""Pont vers les modèles locaux (tâche 7.4).

Une gateway hébergée dans le cloud ne peut pas joindre l'Ollama / LM Studio d'un utilisateur, derrière
sa box (NAT, pare-feu). L'agent de pont (`python -m gateway.bridge_agent`), lancé sur la machine de
l'utilisateur, ouvre une connexion WebSocket SORTANTE vers la gateway : aucun port à ouvrir, aucun
tunnel public. La gateway fait alors passer par cette connexion les requêtes destinées aux modèles
locaux, comme s'il s'agissait d'un provider HTTP ordinaire.

- Chaque pont porte un nom unique sur l'instance, utilisé comme nom de provider (`maison/llama3.1:8b`)
  et n'est visible et utilisable que par le compte qui l'a créé.
- Authentification de l'agent : jeton `gwb_…` (affiché une seule fois, seule son empreinte est stockée).
- L'agent n'accepte que les routes d'inférence (ALLOWED_PATHS) : même une gateway compromise ne peut
  pas piloter le serveur local (suppression de modèles, etc.).

Protocole (trames JSON texte) :
  gateway -> agent : {"type": "request", "id", "method", "path", "headers", "body": base64}
                     {"type": "cancel", "id"}
  agent -> gateway : {"type": "hello", "agent": version}
                     {"type": "response", "id", "status", "headers"}
                     {"type": "chunk", "id", "data": base64}   (corps en flux, ex. SSE)
                     {"type": "end", "id"} | {"type": "error", "id", "message"}
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import uuid

import httpx

log = logging.getLogger("gateway.bridge")

TOKEN_PREFIX = "gwb_"
BRIDGE_HOST = ".bridge.internal"  # http://<nom>.bridge.internal -> pont <nom>
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
ALLOWED_PATHS = ("/models", "/chat/completions", "/completions", "/embeddings")
FORWARDED_HEADERS = ("content-type", "accept")
CLOSE_UNAUTHORIZED, CLOSE_PAYMENT, CLOSE_REPLACED, CLOSE_REVOKED = 4401, 4402, 4409, 4410
CLOSE_RATE_LIMITED = 4429  # IP bloquée (force brute, 12.3) : non fatal, l'agent réessaie


class BridgeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def base_url(name: str) -> str:
    return f"http://{name}{BRIDGE_HOST}"


def path_allowed(method: str, path: str) -> bool:
    """Routes relayées : inférence seulement (vérifié des deux côtés du pont)."""
    bare = path.split("?", 1)[0]
    return method in ("GET", "POST") and bare in ALLOWED_PATHS and ".." not in path and "//" not in path


def validate_name(name, reserved: set[str]) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise BridgeError(
            400,
            "Nom de pont : 2 à 31 caractères, minuscules, chiffres et tirets, "
            "commençant par une lettre (ex. « maison »).",
        )
    if name in reserved or name == "auto":
        raise BridgeError(409, f"Le nom « {name} » est déjà utilisé sur cette instance.")
    return name


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class BridgeSession:
    """Une connexion d'agent : multiplexe les requêtes en cours par identifiant."""

    def __init__(self, ws, bridge: dict):
        self.ws = ws
        self.bridge = bridge
        self.pending: dict[str, asyncio.Queue] = {}
        self._send_lock = asyncio.Lock()
        self.closed = False

    @property
    def name(self) -> str:
        return self.bridge["name"]

    async def send(self, frame: dict) -> None:
        if self.closed:
            raise httpx.ConnectError(f"Pont « {self.name} » déconnecté.")
        async with self._send_lock:
            await self.ws.send_text(json.dumps(frame))

    def dispatch(self, frame: dict) -> None:
        q = self.pending.get(frame.get("id") or "")
        if q is not None and frame.get("type") in ("response", "chunk", "end", "error"):
            q.put_nowait(frame)

    async def run(self) -> None:
        """Lit les trames de l'agent jusqu'à la déconnexion."""
        try:
            while True:
                try:
                    frame = json.loads(await self.ws.receive_text())
                except ValueError:
                    continue
                if isinstance(frame, dict):
                    self.dispatch(frame)
        finally:
            self.fail_all("Pont déconnecté en cours de requête.")

    def fail_all(self, message: str) -> None:
        self.closed = True
        for q in self.pending.values():
            q.put_nowait({"type": "error", "message": message})

    async def cancel(self, rid: str) -> None:
        self.pending.pop(rid, None)
        try:
            await self.send({"type": "cancel", "id": rid})
        except Exception:  # noqa: BLE001 — pont déjà fermé
            pass


class BridgeHub:
    """Ponts connectés, par nom."""

    def __init__(self):
        self.sessions: dict[str, BridgeSession] = {}

    def online(self, name: str) -> bool:
        s = self.sessions.get(name)
        return bool(s and not s.closed)

    async def attach(self, session: BridgeSession) -> None:
        old = self.sessions.get(session.name)
        self.sessions[session.name] = session
        if old and old is not session:  # un seul agent par pont : le plus récent gagne
            old.fail_all("Pont repris par un autre agent.")
            try:
                await old.ws.close(code=CLOSE_REPLACED)
            except Exception:  # noqa: BLE001
                pass

    def detach(self, session: BridgeSession) -> None:
        if self.sessions.get(session.name) is session:
            del self.sessions[session.name]

    async def kick(self, name: str, code: int = CLOSE_REVOKED) -> None:
        s = self.sessions.pop(name, None)
        if s:
            s.fail_all("Pont révoqué.")
            try:
                await s.ws.close(code=code)
            except Exception:  # noqa: BLE001
                pass


class _BridgeStream(httpx.AsyncByteStream):
    def __init__(self, session: BridgeSession, rid: str, queue: asyncio.Queue, timeout: float):
        self.session, self.rid, self.queue, self.timeout = session, rid, queue, timeout
        self.done = False

    async def __aiter__(self):
        while not self.done:
            try:
                frame = await asyncio.wait_for(self.queue.get(), self.timeout)
            except TimeoutError:
                raise httpx.ReadTimeout(f"Pont « {self.session.name} » : pas de réponse.")
            kind = frame.get("type")
            if kind == "chunk":
                yield base64.b64decode(frame.get("data") or "")
            elif kind == "end":
                self.done = True
            elif kind == "error":
                self.done = True
                raise httpx.ReadError(frame.get("message") or "Erreur du pont.")

    async def aclose(self) -> None:
        if not self.done:  # flux abandonné (client parti) : l'agent arrête l'upstream
            self.done = True
            await self.session.cancel(self.rid)
        self.session.pending.pop(self.rid, None)


class BridgeTransport(httpx.AsyncBaseTransport):
    """Transport httpx : http://<nom>.bridge.internal/<route> -> pont <nom>."""

    def __init__(self, hub: BridgeHub):
        self.hub = hub

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        name = host[: -len(BRIDGE_HOST)] if host.endswith(BRIDGE_HOST) else host
        session = self.hub.sessions.get(name)
        if not session or session.closed:
            raise httpx.ConnectError(
                f"Pont « {name} » hors ligne : lancez l'agent sur la machine locale.", request=request
            )
        path = request.url.raw_path.decode()
        if not path_allowed(request.method, path):
            raise httpx.UnsupportedProtocol(f"Route non relayée par le pont : {request.method} {path}")
        timeouts = request.extensions.get("timeout") or {}
        read_timeout = timeouts.get("read") or 600.0
        rid = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue()
        session.pending[rid] = queue
        body = await request.aread()
        try:
            await session.send(
                {
                    "type": "request",
                    "id": rid,
                    "method": request.method,
                    "path": path,
                    "headers": {k: v for k, v in request.headers.items() if k.lower() in FORWARDED_HEADERS},
                    "body": b64(body),
                }
            )
            while True:  # en-tête de réponse (premier octet côté agent)
                try:
                    frame = await asyncio.wait_for(queue.get(), read_timeout)
                except TimeoutError:
                    raise httpx.ReadTimeout(f"Pont « {name} » : pas de réponse.", request=request)
                if frame.get("type") == "response":
                    break
                if frame.get("type") == "error":
                    raise httpx.ConnectError(f"Pont « {name} » : {frame.get('message')}", request=request)
        except BaseException:
            await session.cancel(rid)
            raise
        headers = [
            (k, v)
            for k, v in (frame.get("headers") or {}).items()
            if k.lower() not in ("content-length", "transfer-encoding", "content-encoding", "connection")
        ]
        return httpx.Response(
            int(frame.get("status") or 502),
            headers=headers,
            stream=_BridgeStream(session, rid, queue, read_timeout),
            request=request,
        )
