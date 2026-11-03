"""Recherche des serveurs de modèles des autres machines du réseau local (12.5).

Cibles : adresses fournies par l'admin, sinon GATEWAY_NETWORK_HOSTS (ex. « 192.168.1.20, pc-gpu.local,
192.168.1.0/24 »), sinon le sous-réseau /24 de chaque adresse réseau de ce PC. Uniquement des adresses PRIVÉES
(jamais Internet, ni cette machine elle-même, déjà couverte par les fournisseurs locaux par défaut), au plus
MAX_ADDRESSES par recherche.

Chaque port connu (Ollama 11434, LM Studio 1234, vLLM 8000…) est d'abord testé par une simple connexion TCP
courte ; seuls les ports ouverts reçoivent une requête GET /v1/models. Est retenu ce qui répond une vraie liste de
modèles au format OpenAI.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket

import httpx

from .providers.base import GATEWAY_HEADER

KNOWN_PORTS = {11434: "ollama", 1234: "lmstudio", 8000: "vllm", 5000: "textgen", 8080: "localai", 4000: "litellm"}
MAX_ADDRESSES = 1024
CONNECT_TIMEOUT = 0.4
CONCURRENCY = 128


class ScanError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _private(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private and not ip.is_loopback and not ip.is_link_local and not ip.is_multicast and not ip.is_unspecified
    )


def default_targets(own: list[str]) -> list[str]:
    raw = os.getenv("GATEWAY_NETWORK_HOSTS", "").strip()
    if raw:
        return [h.strip() for h in raw.split(",") if h.strip()]
    nets = {str(ipaddress.ip_network(f"{ip}/24", strict=False)) for ip in own if _private(ipaddress.ip_address(ip))}
    return sorted(nets)


def expand(targets: list[str], own: list[str]) -> list[str]:
    """Adresses IP privées à sonder (hors cette machine), dans l'ordre, sans doublon."""
    if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
        raise ScanError("'hosts' : liste d'adresses, de noms ou de réseaux (ex. 192.168.1.0/24).")
    out: list[str] = []
    for t in (t.strip() for t in targets):
        if not t:
            continue
        try:
            if "/" in t:
                net = ipaddress.ip_network(t, strict=False)
                if not _private(net.network_address):
                    raise ScanError(f"« {t} » n'est pas un réseau privé (recherche limitée au réseau local).")
                if net.num_addresses > MAX_ADDRESSES * 4:
                    raise ScanError(f"« {t} » est trop grand : un /22 au maximum.")
                addrs = [str(a) for a in net.hosts()]
            else:
                try:
                    addrs = [str(ipaddress.ip_address(t))]
                except ValueError:
                    infos = socket.getaddrinfo(t, None, family=socket.AF_INET, proto=socket.IPPROTO_TCP)
                    addrs = [i[4][0] for i in infos]
                for a in addrs:
                    if not _private(ipaddress.ip_address(a)):
                        raise ScanError(f"« {t} » ({a}) n'est pas une adresse privée du réseau local.")
        except (OSError, ValueError) as e:
            if isinstance(e, ScanError):
                raise
            raise ScanError(f"Adresse ou réseau invalide : « {t} ».")
        out += [a for a in addrs if a not in own and a not in out]
        if len(out) > MAX_ADDRESSES:
            raise ScanError(
                f"Recherche limitée à {MAX_ADDRESSES} adresses : précisez les machines ou un réseau plus petit."
            )
    return out


async def _open(host: str, port: int, sem: asyncio.Semaphore) -> bool:
    async with sem:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)
        except (TimeoutError, OSError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True


async def _models(client: httpx.AsyncClient, host: str, port: int) -> dict | None:
    base = f"http://{host}:{port}/v1"
    try:
        r = await client.get(f"{base}/models", timeout=3)
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    served_by = r.headers.get(GATEWAY_HEADER)  # une gateway Smart AI (peut-être celle-ci)
    if r.status_code == 401:
        return {"base_url": base, "models": [], "needs_key": True, "served_by": served_by}
    items = data.get("data") if isinstance(data, dict) else None
    if r.status_code != 200 or not isinstance(items, list):
        return None
    ids = [m["id"] for m in items if isinstance(m, dict) and isinstance(m.get("id"), str)][:200]
    return {"base_url": base, "models": ids, "needs_key": False, "served_by": served_by}


async def scan(
    addresses: list[str], client: httpx.AsyncClient, ports: dict[int, str] | None = None, opener=None
) -> list[dict]:
    """Serveurs trouvés : {host, port, server, base_url, models, needs_key}."""
    ports = ports or KNOWN_PORTS
    sem = asyncio.Semaphore(CONCURRENCY)
    probe = opener or _open
    pairs = [(h, p) for h in addresses for p in ports]
    opened = await asyncio.gather(*(probe(h, p, sem) for h, p in pairs))
    found = []
    for (host, port), ok in zip(pairs, opened):
        if not ok:
            continue
        info = await _models(client, host, port)
        if info:
            found.append({"host": host, "port": port, "server": ports[port], **info})
    return found


def suggest_name(server: str, host: str, taken: set[str]) -> str:
    """Nom de fournisseur lisible et unique : ollama-192-168-1-20 (ou -2, -3…)."""
    base = f"{server}-{host.replace('.', '-').replace(':', '-')}"[:31].rstrip("-")
    name, n = base, 2
    while name in taken:
        suffix = f"-{n}"
        name, n = base[: 31 - len(suffix)] + suffix, n + 1
    return name
