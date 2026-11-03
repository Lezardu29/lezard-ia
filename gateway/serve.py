"""Lanceur de la gateway avec HTTPS réseau local (12.7).

    python -m gateway.serve [--port 8000]

Sans GATEWAY_TLS_PORT : équivaut à `uvicorn gateway.main:app --host $GATEWAY_BIND --port 8000 --ws websockets-sansio`.
Avec GATEWAY_TLS_PORT (ex. 8443) : DEUX écoutes dans le MÊME processus, sur la même application (un seul état :
tunnel, ponts, compteurs, cache) —
  - HTTP  127.0.0.1:<port>         cette machine seulement (navigateur local, tunnel, agent de pont) ;
  - HTTPS <GATEWAY_BIND>:<tls>     les autres appareils du réseau, certificat de l'autorité locale (gateway.tls).
Avec GATEWAY_SUPERVISED=1 (lancer.ps1, service systemd) : POST /v1/admin/restart arrête proprement la gateway avec le
code 3, que le superviseur relance aussitôt (18.1).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

import uvicorn

from .config import bind_address, lan_mode
from .remote import lan_addresses
from .tls import ensure_certificates, tls_port

log = logging.getLogger("gateway.serve")
WS_MAX = 64 * 1024 * 1024
# implémentation WebSocket d'uvicorn (10.9) : « auto » choisit encore websockets.legacy, dépréciée et vouée à
# disparaître ; la version sans E/S est l'implémentation actuelle de la bibliothèque websockets
WS_IMPL = "websockets-sansio"
# 18.1 : code de sortie « redémarrage demandé » (POST /v1/admin/restart). lancer.ps1 et systemd relancent la
# gateway aussitôt ; GATEWAY_SUPERVISED=1 indique qu'un superviseur est là pour le faire (sinon : pas de bouton).
RESTART_EXIT = 3


def supervised() -> bool:
    return os.getenv("GATEWAY_SUPERVISED") == "1"


async def serve(port: int, app=None) -> bool:
    """Sert la gateway ; True si l'arrêt est un redémarrage demandé."""
    if app is None:
        from .main import app
    tls = tls_port()
    bind = bind_address()
    common = {
        "ws": WS_IMPL,
        "ws_max_size": WS_MAX,
        "proxy_headers": True,
        "forwarded_allow_ips": os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    }
    servers: list[uvicorn.Server] = []
    restart = {"asked": False}
    if supervised():

        def request_restart() -> None:
            restart["asked"] = True
            for s in servers:  # arrêt propre : requêtes en cours terminées, base fermée
                s.should_exit = True

        app.state.request_restart = request_restart
    if not tls:
        servers.append(uvicorn.Server(uvicorn.Config(app, host=bind, port=port, **common)))
        await servers[0].serve()
        return restart["asked"]
    if not lan_mode():
        raise SystemExit("GATEWAY_TLS_PORT demande le mode réseau local (GATEWAY_BIND=0.0.0.0 ou une adresse).")
    files = ensure_certificates(lan_addresses(bind))
    http = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, **common))
    https = uvicorn.Server(
        uvicorn.Config(
            app,
            host=bind,
            port=tls,
            lifespan="off",  # état créé par le 1er
            ssl_certfile=str(files.cert),
            ssl_keyfile=str(files.key),
            **common,
        )
    )
    servers.extend((http, https))
    log.warning(
        "HTTPS réseau local sur le port %s — autorité locale %s (empreinte %s)", tls, files.ca_cert, files.fingerprint
    )

    async def http_first():
        await http.serve()
        https.should_exit = True  # arrêt du HTTP (Ctrl+C…) : on arrête aussi le HTTPS

    async def https_after_startup():
        while not http.started:  # l'état de l'application d'abord (lifespan)
            if http.should_exit:
                return
            await asyncio.sleep(0.05)
        await https.serve()
        http.should_exit = True

    await asyncio.gather(http_first(), https_after_startup())
    return restart["asked"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=int(os.getenv("GATEWAY_PORT", "8000")))
    args = parser.parse_args()
    if asyncio.run(serve(args.port)):
        raise SystemExit(RESTART_EXIT)


if __name__ == "__main__":
    main()
