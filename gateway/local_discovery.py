"""Recherche des IA locales de CE PC, pour la fenêtre « Ajouter un modèle » : serveurs qui tournent (Ollama,
LM Studio, vLLM…) et leurs modèles, Ollama installé mais arrêté (démarrable en un clic).

Lancée seulement à la demande de l'administrateur (jamais au démarrage), sur les ports connus de cette machine.
La gateway elle-même, qui répond aussi en « compatible OpenAI », est écartée grâce à son en-tête d'installation.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from .network_scan import KNOWN_PORTS, scan
from .providers.base import is_self

LABELS = {
    "ollama": "Ollama",
    "lmstudio": "LM Studio",
    "vllm": "vLLM",
    "textgen": "text-generation-webui",
    "localai": "LocalAI",
    "litellm": "LiteLLM",
}
OLLAMA_DOWNLOAD = "https://ollama.com/download"


def this_host() -> str:
    """Adresse de ce PC vue par la gateway (GATEWAY_LOCAL_HOST quand elle tourne en conteneur)."""
    return os.getenv("GATEWAY_LOCAL_HOST", "").strip() or "127.0.0.1"


def ollama_executable() -> str | None:
    """Ollama installé sur ce PC ? (PATH, sinon emplacement par défaut de l'installeur Windows / macOS)."""
    found = shutil.which("ollama")
    if found:
        return found
    candidates = [Path("/Applications/Ollama.app/Contents/Resources/ollama"), Path("/usr/local/bin/ollama")]
    if os.getenv("LOCALAPPDATA"):
        candidates.insert(0, Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Ollama" / "ollama.exe")
    return next((str(p) for p in candidates if p.is_file()), None)


async def find_servers(client: httpx.AsyncClient, instance_id: str | None) -> list[dict]:
    """Serveurs de modèles qui répondent sur ce PC : {server, label, port, base_url, models, needs_key}."""
    host = this_host()
    found = await scan([host], client, KNOWN_PORTS)
    out = []
    for f in found:
        if is_self(f.get("served_by"), instance_id):
            continue  # la gateway elle-même
        out.append(
            {
                "server": f["server"],
                "label": LABELS.get(f["server"], f["server"]),
                "port": f["port"],
                "base_url": f["base_url"].replace(f"//{host}:", "//localhost:", 1)
                if host == "127.0.0.1"
                else f["base_url"],
                "models": f["models"],
                "needs_key": f["needs_key"],
            }
        )
    return out


async def start_ollama(exe: str, client: httpx.AsyncClient, log_dir: Path, wait_s: float = 20) -> bool:
    """Lance « ollama serve » en arrière-plan (détaché de la gateway) et attend qu'il réponde. Création du dossier,
    ouverture du journal et Popen (détaché, rend la main aussitôt) : instantanés, pas besoin d'un thread."""
    log_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(log_dir / "ollama.log", "ab") as err:  # noqa: ASYNC230
        subprocess.Popen(  # noqa: ASYNC220
            [exe, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=err,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=sys.platform != "win32",
        )
    url = f"http://{this_host()}:11434/api/tags"
    for _ in range(int(wait_s * 2)):
        try:
            if (await client.get(url, timeout=1)).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)
    return False
