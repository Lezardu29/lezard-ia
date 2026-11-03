"""Connexion avec un compte lezard-ia.fr depuis la page de connexion de ce PC (/v1/compte/sel, /v1/compte/connexion).

La gateway relaie seulement ces deux appels vers le service des comptes (Cloudflare) : la page de connexion est servie
par ce PC, et le site n'accepte pas les appels d'autres origines. Rien n'y est déchiffré ni gardé : comme sur
lezard-ia.fr, le navigateur tire du mot de passe la preuve envoyée et la clé du coffre, qui ne quitte pas la page ;
la gateway ne voit passer que l'identifiant, la preuve et le coffre chiffré. Plafond par adresse (RELAY_PER_MINUTE),
en plus de ceux du service : la page ne sert pas à essayer des mots de passe en masse.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from types import SimpleNamespace

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from ..validation import json_object

ACTIONS = ("sel", "connexion")
RELAY_PER_MINUTE = 12
MAX_FIELD = 300


def service_url() -> str:
    return os.getenv("LEZARD_COMPTES_URL", "https://lezard-ia.fr/api/compte").rstrip("/")


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    recent: dict[str, deque] = defaultdict(deque)

    def refuse(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)  # même forme que le service des comptes

    @router.post("/v1/compte/{action}")
    async def relay(action: str, request: Request):
        if action not in ACTIONS:
            return refuse(404, "Action inconnue.")
        ip, now = shared.client_ip(request), time.monotonic()
        seen = recent[ip]
        while seen and now - seen[0] > 60:
            seen.popleft()
        if len(seen) >= RELAY_PER_MINUTE:
            return refuse(429, "Trop de tentatives de connexion : réessayez dans une minute.")
        seen.append(now)
        body = await json_object(request)
        sent = {k: body[k] for k in ("user", "auth") if isinstance(body.get(k), str) and len(body[k]) <= MAX_FIELD}
        try:
            r = await holder["gw"].client.post(f"{service_url()}/{action}", json=sent, timeout=20)
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return refuse(502, "Service des comptes injoignable : vérifiez la connexion internet de ce PC.")
        return JSONResponse(data if isinstance(data, dict) else {}, status_code=r.status_code)

    return router
