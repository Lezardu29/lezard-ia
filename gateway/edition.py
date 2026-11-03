"""Licence du site web (29.1) : toutes les fonctions du logiciel sont ouvertes (open source), y compris les modèles
cloud avec votre clé, les clés pour d'autres logiciels, les outils web et l'accès à distance par votre propre tunnel.
Seul l'accès par le site web (relais : votre IA depuis n'importe où, PC éteint) demande une licence, gratuite
(chaque personne la crée sur son compte),
signée par le serveur central (license.py, 30.4) : collée dans « Accès par le site » ou dans GATEWAY_LICENSE. Une instance
hébergée a cet accès.
"""

from __future__ import annotations

import os
import time

import httpx

from .config import hosted_instance
from .license import License, LicenseError, verify

EDITIONS = ("gratuit", "pro")
PRICE = "gratuit"
RENEW_BEFORE_DAYS = 10  # renouvellement demandé au serveur central dans les 10 derniers jours (30.4)


_state: dict = {"license": None, "error": None, "token": None}
_override: str | None = None  # tests seulement (conftest.py) : jamais réglé par le logiciel


def load_license(token: str | None, instance_id: str | None) -> License | None:
    """Vérifie et retient la licence de cette installation (démarrage, activation) ; LicenseError si refusée."""
    _state.update(license=None, error=None, token=None)
    if not token:
        return None
    try:
        _state["license"] = verify(token, instance_id)
        _state["token"] = token.strip()
    except LicenseError as e:
        _state["error"] = str(e)
        raise
    return _state["license"]


def license_token() -> str | None:
    """Code de la licence en vigueur (preuve envoyée au serveur central : renouvellement, tunnel)."""
    return _state["token"] if _state["license"] else None


def license_info() -> dict:
    lic: License | None = _state["license"]
    return {"license": lic.public() if lic else None, "license_error": _state["error"]}


def account_url() -> str:
    """Serveur central des comptes (lezard-ia.fr) : activation et renouvellement des licences."""
    return os.getenv("LEZARD_ACCOUNT_URL", "https://compte.lezard-ia.fr").rstrip("/")


async def renew(client: httpx.AsyncClient, instance_id: str | None) -> str | None:
    """Licence fraîche demandée au serveur central quand l'actuelle expire bientôt (ou est en délai de grâce) :
    nouveau code vérifié et en vigueur, ou None (rien à faire, serveur injoignable, abonnement terminé, PC retiré :
    la licence actuelle court jusqu'à son terme, puis la version gratuite reprend)."""
    lic: License | None = _state["license"]
    old = _state["token"]
    if not lic or not old or lic.device == "*" or lic.expires - time.time() > RENEW_BEFORE_DAYS * 86400:
        return None
    try:
        r = await client.post(f"{account_url()}/api/licenses/renew", json={"license": old}, timeout=20)
        new = r.json().get("license") if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    if not isinstance(new, str):
        return None
    try:
        load_license(new, instance_id)
    except LicenseError:  # réponse inattendue : la licence actuelle reste en vigueur
        load_license(old, instance_id)
        return None
    return new


async def managed_tunnel(client: httpx.AsyncClient, port: int) -> dict | bool | None:
    """Tunnel Cloudflare de ce PC (31.1), créé par le serveur central : {hostname, token} ; False si le serveur
    le refuse (abonnement terminé, PC retiré) ; None si on ne peut pas savoir (pas de licence liée à ce PC, serveur
    injoignable ou en panne) : la gateway reprend alors le tunnel qu'elle a gardé (chiffré)."""
    lic: License | None = _state["license"]
    if not lic or lic.device == "*" or lic.status() == "expiree" or not _state["token"]:
        return None
    try:
        r = await client.post(
            f"{account_url()}/api/tunnel", json={"license": _state["token"], "port": port}, timeout=30
        )
        if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
            return False
        data = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("token"), str) and isinstance(data.get("hostname"), str):
        return {"token": data["token"], "hostname": data["hostname"]}
    return None


def edition() -> str:
    if _override:
        return _override
    if hosted_instance():
        return "pro"
    lic: License | None = _state["license"]
    return "pro" if lic and lic.status() != "expiree" else "gratuit"


def web_access() -> bool:
    """Accès par le site web (relais, PC éteint) : licence valide ou instance hébergée. Le reste est ouvert."""
    return edition() == "pro"
