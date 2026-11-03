"""cloudflared pour l'accès à distance automatique : téléchargé à la première demande (Windows), dans
<dossier de données>/bin. Version figée, publiée depuis plus de deux semaines ; taille et SHA-256 vérifiés avant usage.
Ailleurs (Linux, macOS, Docker) : cloudflared installé sur la machine (PATH) ou CLOUDFLARED_BIN."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

import httpx

VERSION = "2026.10.1"  # publiée le 2026-10-15
URL = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/cloudflared-windows-amd64.exe"
SIZE = 55325120
SHA256 = "4d2490e74082c5d48dbfedaf92576fa5bd1702379ab88eb7faa0cfbe8dc891b6"


class CloudflaredError(Exception):
    pass


def local_path() -> Path:
    return Path(os.getenv("GATEWAY_DATA_DIR", "data")) / "bin" / "cloudflared.exe"


def find() -> str | None:
    """Chemin de cloudflared : CLOUDFLARED_BIN, puis le PATH, puis la copie téléchargée ici."""
    env = os.getenv("CLOUDFLARED_BIN")
    if env:
        return env
    return shutil.which("cloudflared") or (str(local_path()) if local_path().is_file() else None)


async def ensure(client: httpx.AsyncClient) -> str:
    """cloudflared prêt à l'emploi ; téléchargé et vérifié s'il manque (Windows seulement)."""
    found = find()
    if found:
        return found
    if sys.platform != "win32":
        raise CloudflaredError("cloudflared introuvable : installez-le (https://github.com/cloudflare/cloudflared).")
    target = local_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".part")
    digest, size = hashlib.sha256(), 0
    try:
        async with client.stream("GET", URL, follow_redirects=True, timeout=httpx.Timeout(30, read=120)) as r:
            r.raise_for_status()
            with part.open("wb") as f:
                async for chunk in r.aiter_bytes(1 << 16):
                    size += len(chunk)
                    if size > SIZE:
                        raise CloudflaredError("téléchargement de cloudflared plus gros que prévu : refusé.")
                    digest.update(chunk)
                    f.write(chunk)
    except httpx.HTTPError as e:
        part.unlink(missing_ok=True)
        raise CloudflaredError(f"téléchargement de cloudflared impossible ({type(e).__name__}) : réessayez.") from e
    except CloudflaredError:
        part.unlink(missing_ok=True)
        raise
    if size != SIZE or digest.hexdigest() != SHA256:
        part.unlink(missing_ok=True)
        raise CloudflaredError("cloudflared téléchargé ne correspond pas à la version attendue : refusé.")
    part.replace(target)
    return str(target)
