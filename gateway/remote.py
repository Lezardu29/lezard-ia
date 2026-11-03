"""Accès distant à l'instance et aux modèles locaux (tâche 5.6).

Fournisseurs de tunnel (binaire requis sur la machine) :
- cloudflare : `cloudflared` — quick tunnel *.trycloudflare.com (sans compte), ou tunnel nommé si
               CLOUDFLARE_TUNNEL_TOKEN (+ CLOUDFLARE_TUNNEL_HOSTNAME pour l'URL publique) ;
- ngrok      : `ngrok` — NGROK_AUTHTOKEN, domaine fixe optionnel NGROK_DOMAIN ;
- tailscale  : `tailscale serve` (réseau privé tailnet, défaut) ou `tailscale funnel` (public).

Connexion automatique au démarrage : REMOTE_ACCESS=cloudflare|ngrok|tailscale (REMOTE_ACCESS_MODE=
serve|funnel pour Tailscale). Port local exposé : GATEWAY_PORT (défaut 8000).

Sécurité : une exposition publique est refusée si les requêtes anonymes sont acceptées
(GATEWAY_REQUIRE_KEY désactivé), sauf dérogation explicite `allow_anonymous`.
"""

from __future__ import annotations

import logging
import os
import re
import socket
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger("gateway.remote")

PROVIDERS = ("cloudflare", "ngrok", "tailscale")
BINARIES = {"cloudflare": "cloudflared", "ngrok": "ngrok", "tailscale": "tailscale"}
TRYCLOUDFLARE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def lan_addresses(bind: str = "0.0.0.0") -> list[str]:
    """Adresses IPv4 de cette machine sur le réseau local (mode réseau local, 12.1)."""
    if bind not in ("0.0.0.0", "::", ""):
        return [bind]  # écoute sur une seule interface
    ips: set[str] = set()
    try:  # interface de sortie par défaut (aucun paquet envoyé)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.", "0.")))


class TunnelError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def stable_url(provider: str | None) -> bool:
    """L'adresse publique survit-elle à un redémarrage ? (quick tunnel Cloudflare / ngrok sans domaine : non)"""
    if provider == "cloudflare":
        return bool(os.getenv("CLOUDFLARE_TUNNEL_TOKEN") and os.getenv("CLOUDFLARE_TUNNEL_HOSTNAME"))
    if provider == "ngrok":
        return bool(os.getenv("NGROK_DOMAIN"))
    return provider == "tailscale"  # nom DNS de la machine dans le tailnet


@dataclass
class TunnelState:
    provider: str | None = None
    mode: str | None = None  # public | private
    state: str = "stopped"  # stopped | starting | running | reconnecting | error
    public_url: str | None = None
    started_at: float | None = None
    error: str | None = None
    restarts: int = 0  # redémarrages automatiques après une coupure (12.4)
    next_retry_at: float | None = None
    logs: deque = field(default_factory=lambda: deque(maxlen=40))

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "mode": self.mode,
            "state": self.state,
            "public_url": self.public_url,
            "started_at": self.started_at,
            "error": self.error,
            "restarts": self.restarts,
            "next_retry_at": self.next_retry_at,
            "stable_url": stable_url(self.provider),
            "logs": list(self.logs)[-15:],
        }


from .tunnel import RemoteAccess  # noqa: E402,F401  (tunnels Cloudflare, ngrok, Tailscale)
