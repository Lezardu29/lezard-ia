"""Adresse d'un serveur d'IA : sur ce PC ou sur le réseau de la maison ? (résolution gardée 10 minutes)

Une adresse privée ou de bouclage, un nom sans domaine ou en .local, .lan, .home… comptent comme locaux ; une adresse
Tailscale (100.64.0.0/10) non : c'est un accès à distance.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urlparse

LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa")
ADDRESS_TTL = 600
_addresses: dict[str, tuple[bool, float]] = {}


def _private(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    return ip.is_loopback or ip.is_private  # 100.64.0.0/10 (Tailscale) : non, c'est un accès à distance


def _resolve(host: str) -> bool:
    try:
        return _private(host)
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        addrs = {i[4][0] for i in socket.getaddrinfo(host, None)}
    except OSError:  # nom de la maison non résolu d'ici (conteneur, machine éteinte) : nom sans domaine seulement
        return "." not in host or host.endswith(LOCAL_SUFFIXES)
    return bool(addrs) and all(_private(a) for a in addrs)


def local_address(base_url: str) -> bool:
    """Le serveur est-il sur ce PC ou le réseau de la maison ? (résolution gardée 10 minutes)"""
    host = (urlparse(base_url).hostname or "").lower()
    if not host:
        return False
    cached = _addresses.get(host)
    if cached and time.monotonic() - cached[1] < ADDRESS_TTL:
        return cached[0]
    ok = _resolve(host)
    _addresses[host] = (ok, time.monotonic())
    return ok
