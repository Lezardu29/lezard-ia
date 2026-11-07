"""Accès à distance automatique : ce PC demande à lezard-ia.fr (/api/tunnel) un tunnel Cloudflare nommé et une adresse
fixe <nom>-<hex>.lezard-ia.fr, puis le garde (jeton chiffré dans la base) et le relance à chaque démarrage. Rien à
configurer : « Créer mon compte web » s'en charge quand aucun accès à distance n'existe encore."""

from __future__ import annotations

import os

import httpx

from .crypto import CryptoError

SETTING = "managed_tunnel"  # même réglage que le tunnel géré par l'ancien serveur central (31.1)
CONTEXT = "managed-tunnel"
IDLE_DAYS = 5  # comme functions/api/tunnel/[action].js : tunnel supprimé après 5 jours sans aucune connexion


class AutoTunnelError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def site_url() -> str:
    return os.getenv("LEZARD_SITE_URL", "https://lezard-ia.fr").rstrip("/")


async def _call(client: httpx.AsyncClient, action: str, body: dict) -> dict:
    try:
        r = await client.post(f"{site_url()}/api/tunnel/{action}", json=body, timeout=60)
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise AutoTunnelError(502, "lezard-ia.fr injoignable : vérifiez la connexion Internet du PC.") from e
    if r.status_code != 200 or not isinstance(data, dict):
        message = data.get("error") if isinstance(data, dict) else None
        raise AutoTunnelError(r.status_code if r.status_code >= 400 else 502, message or "Accès à distance refusé.")
    return data


async def create(client: httpx.AsyncClient, instance: str, name: str, port: int) -> dict:
    """Nouveau tunnel : {hostname, token, proof}."""
    data = await _call(client, "creer", {"instance": instance, "name": name, "port": port})
    if not all(isinstance(data.get(k), str) and data[k] for k in ("hostname", "token", "proof")):
        raise AutoTunnelError(502, "Réponse inattendue de lezard-ia.fr.")
    return {"hostname": data["hostname"], "token": data["token"], "proof": data["proof"]}


async def refresh(client: httpx.AsyncClient, instance: str, proof: str, port: int) -> dict:
    """Tunnel existant (jeton perdu, autre port) : {hostname, token}."""
    data = await _call(client, "jeton", {"instance": instance, "proof": proof, "port": port})
    return {"hostname": data.get("hostname"), "token": data.get("token")}


async def forget_if_deleted(client: httpx.AsyncClient, store, cipher, instance: str, port: int) -> bool:
    """Tunnel gardé supprimé par lezard-ia.fr (ménage des tunnels sans connexion depuis 5 jours) : oublié, True.
    Site injoignable ou autre refus : False, le tunnel gardé est démarré comme avant."""
    saved = store.get_setting(SETTING) or {}
    try:
        proof = cipher.decrypt(saved["proof"], CONTEXT)
    except (KeyError, TypeError, CryptoError):  # pas de preuve gardée, ou clé de chiffrement changée
        return False
    try:
        await refresh(client, instance, proof, port)
    except AutoTunnelError as e:
        if e.status != 404:
            return False
        store.set_setting(SETTING, {})
        return True
    return False


def save(store, cipher, tunnel: dict) -> None:
    sealed = {"hostname": tunnel["hostname"], "token": cipher.encrypt(tunnel["token"], CONTEXT), "auto": True}
    if tunnel.get("proof"):
        sealed["proof"] = cipher.encrypt(tunnel["proof"], CONTEXT)
    store.set_setting(SETTING, sealed)
