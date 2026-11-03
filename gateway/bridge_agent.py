"""Agent de pont (7.4) : relie une gateway hébergée aux modèles locaux de cette machine.

    python -m gateway.bridge_agent --gateway https://ma-gateway.example.com --token gwb_… \\
        [--target http://localhost:11434/v1] [--target-key …]

Variables équivalentes : GATEWAY_URL, GATEWAY_BRIDGE_TOKEN, BRIDGE_TARGET, BRIDGE_TARGET_KEY.
Connexion WebSocket sortante (aucun port à ouvrir), reconnexion automatique avec attente progressive :
à lancer comme service (systemd, tâche planifiée Windows, conteneur `restart: unless-stopped`).
Seules les routes d'inférence sont relayées vers le serveur local (voir bridge.ALLOWED_PATHS).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import random
import sys

import httpx

from .bridge import CLOSE_PAYMENT, CLOSE_REVOKED, CLOSE_UNAUTHORIZED, FORWARDED_HEADERS, b64, path_allowed

VERSION = "1"
CHUNK = 64 * 1024
log = logging.getLogger("gateway.bridge_agent")
FATAL = {
    CLOSE_UNAUTHORIZED: "jeton refusé (révoqué ou invalide)",
    CLOSE_PAYMENT: "refusé par la gateway",
    CLOSE_REVOKED: "pont supprimé depuis le tableau de bord",
}


def ws_url(gateway: str) -> str:
    g = gateway.rstrip("/")
    if g.startswith("https://"):
        g = "wss://" + g[len("https://") :]
    elif g.startswith("http://"):
        g = "ws://" + g[len("http://") :]
    return g + "/v1/bridge/connect"


class Relay:
    """Traite les trames de la gateway sur une connexion (send/recv asynchrones, texte JSON)."""

    def __init__(self, conn, client: httpx.AsyncClient, target: str, target_key: str | None = None):
        self.conn, self.client = conn, client
        self.target, self.target_key = target.rstrip("/"), target_key
        self.tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def send(self, frame: dict) -> None:
        async with self._lock:
            await self.conn.send(json.dumps(frame))

    async def serve(self) -> None:
        await self.send({"type": "hello", "agent": VERSION})
        try:
            async for raw in self.conn:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                if frame.get("type") == "request":
                    t = asyncio.create_task(self.forward(frame))
                    self.tasks[frame["id"]] = t
                    t.add_done_callback(lambda _, rid=frame["id"]: self.tasks.pop(rid, None))
                elif frame.get("type") == "cancel" and frame.get("id") in self.tasks:
                    self.tasks[frame["id"]].cancel()
        finally:
            for t in list(self.tasks.values()):
                t.cancel()

    async def forward(self, frame: dict) -> None:
        rid, method, path = frame["id"], frame.get("method", "GET"), frame.get("path", "")
        if not path_allowed(method, path):
            await self.send({"type": "error", "id": rid, "message": f"Route refusée par l'agent : {method} {path}"})
            return
        headers = {k: v for k, v in (frame.get("headers") or {}).items() if k.lower() in FORWARDED_HEADERS}
        if self.target_key:
            headers["Authorization"] = f"Bearer {self.target_key}"
        try:
            async with self.client.stream(
                method,
                self.target + path,
                headers=headers,
                content=base64.b64decode(frame.get("body") or ""),
                timeout=600,
            ) as r:
                await self.send(
                    {
                        "type": "response",
                        "id": rid,
                        "status": r.status_code,
                        "headers": {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "retry-after")},
                    }
                )
                # octets décodés (gzip…) : seul content-type est retransmis, pas content-encoding
                async for chunk in r.aiter_bytes(CHUNK):
                    await self.send({"type": "chunk", "id": rid, "data": b64(chunk)})
            await self.send({"type": "end", "id": rid})
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as e:
            log.warning("Serveur local injoignable (%s) : %s", self.target, e)
            await self.send({"type": "error", "id": rid, "message": f"Serveur local injoignable : {type(e).__name__}"})
        except Exception as e:  # noqa: BLE001 — ne jamais laisser la gateway attendre en silence
            log.exception("Relais en échec")
            try:
                await self.send({"type": "error", "id": rid, "message": f"Erreur de l'agent : {type(e).__name__}"})
            except Exception:  # noqa: BLE001 — connexion déjà fermée
                pass


async def run(
    gateway: str,
    token: str,
    target: str,
    target_key: str | None = None,
    client: httpx.AsyncClient | None = None,
    max_attempts: int | None = None,
) -> int:
    """Boucle de connexion. Retourne un code de sortie (2 = erreur définitive : jeton, abonnement)."""
    import websockets

    url, delay, attempts = ws_url(gateway), 1.0, 0
    own = client is None
    client = client or httpx.AsyncClient()
    try:
        while max_attempts is None or attempts < max_attempts:
            attempts += 1
            try:
                async with websockets.connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    max_size=64 * 2**20,
                    ping_interval=20,
                    ping_timeout=20,
                ) as conn:
                    log.info("Pont connecté à %s -> %s", gateway, target)
                    delay = 1.0
                    await Relay(conn, client, target, target_key).serve()
            except websockets.ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd else None
                if code in FATAL:
                    log.error("Connexion refusée : %s.", FATAL[code])
                    return 2
                log.warning("Connexion perdue (%s), reconnexion…", code)
            except websockets.InvalidStatus as e:
                status = e.response.status_code
                if status in (401, 402, 403):
                    log.error("Connexion refusée par la gateway (HTTP %s).", status)
                    return 2
                log.warning("Gateway indisponible (HTTP %s).", status)
            except (TimeoutError, OSError, websockets.WebSocketException) as e:
                log.warning("Gateway injoignable : %s", e)
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, 60.0)
        return 1
    finally:
        if own:
            await client.aclose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="gateway.bridge_agent", description="Relie une gateway hébergée à vos modèles locaux."
    )
    ap.add_argument("--gateway", default=os.getenv("GATEWAY_URL"), help="URL de la gateway (https://…)")
    ap.add_argument("--token", default=os.getenv("GATEWAY_BRIDGE_TOKEN"), help="jeton du pont (gwb_…)")
    ap.add_argument(
        "--target",
        default=os.getenv("BRIDGE_TARGET", "http://localhost:11434/v1"),
        help="serveur local compatible OpenAI (défaut : Ollama)",
    )
    ap.add_argument("--target-key", default=os.getenv("BRIDGE_TARGET_KEY"), help="clé du serveur local (optionnelle)")
    args = ap.parse_args(argv)
    if not args.gateway or not args.token:
        ap.error("--gateway et --token (ou GATEWAY_URL / GATEWAY_BRIDGE_TOKEN) sont requis.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = httpx.URL(args.gateway).host
    if args.gateway.startswith("http://") and host not in ("localhost", "127.0.0.1", "::1"):
        log.warning(
            "Connexion non chiffrée (http://) : le jeton du pont et vos conversations transitent en clair. "
            "Utilisez https://."
        )
    try:
        return asyncio.run(run(args.gateway, args.token, args.target, args.target_key))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
