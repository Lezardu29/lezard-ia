"""Fenêtre de contexte réelle des modèles locaux (10.6, complète 1.4).

Sans information, un modèle local est supposé avoir 8 192 tokens : un modèle 128k était écarté des longs
messages, et un modèle 2k/4k choisi à tort. Sources, de la plus précise à la plus générale :
- Ollama, modèle déjà chargé (/api/ps) : la fenêtre exacte d'exécution, telle qu'Ollama l'a choisie.
- Ollama, sinon (/api/show) : la fenêtre EFFECTIVE estimée, pas celle d'entraînement. L'API compatible OpenAI d'Ollama utilise le
  `num_ctx` du modèle (Modelfile) ou, à défaut, celui du serveur (OLLAMA_CONTEXT_LENGTH, 4 096 par défaut), et
  TRONQUE en silence au-delà. llama3.1 (128k) ne reçoit donc que 4 096 tokens tant qu'Ollama n'est pas réglé
  autrement : c'est ce chiffre qui compte pour le routage. Plafonnée par la longueur d'entraînement.
- LM Studio (/api/v0/models) : fenêtre chargée (loaded_context_length), sinon maximale.
- /v1/models quand il l'expose (vLLM : max_model_len ; LiteLLM, OpenRouter… : context_length).

Capacités (16.2) : /api/show d'Ollama les liste (« capabilities » : completion, tools, vision, thinking…) ; le
routeur n'envoie une requête avec outils qu'aux modèles qui les gèrent.
"""

from __future__ import annotations

import asyncio
import os
import re

import httpx

CONTEXT_FIELDS = ("max_model_len", "loaded_context_length", "context_length", "max_context_length", "context_window")
OLLAMA_DEFAULT_CTX = 4096
_NUM_CTX = re.compile(r"^\s*num_ctx\s+(\d+)\s*$", re.MULTILINE)


def _valid(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and 256 <= v <= 10_000_000 else None


def item_context(item: dict) -> int | None:
    """Fenêtre annoncée par une entrée de /v1/models (ou de /api/v0/models), si elle y figure."""
    for k in CONTEXT_FIELDS:
        v = _valid(item.get(k))
        if v:
            return v
    return None


def ollama_server_default() -> int:
    """num_ctx du serveur Ollama : OLLAMA_CONTEXT_LENGTH (partagé avec `ollama serve` quand lancer.ps1 le démarre
    avec le même .env), sinon le défaut d'Ollama."""
    raw = os.getenv("OLLAMA_CONTEXT_LENGTH", "").strip()
    return int(raw) if raw.isdigit() and int(raw) >= 256 else OLLAMA_DEFAULT_CTX


def ollama_window(show: dict) -> tuple[int, int | None]:
    """(fenêtre effective, longueur d'entraînement) d'après la réponse de /api/show."""
    trained = next(
        (_valid(v) for k, v in (show.get("model_info") or {}).items() if k.endswith(".context_length") and _valid(v)),
        None,
    )
    m = _NUM_CTX.search(show.get("parameters") or "")
    effective = int(m.group(1)) if m else ollama_server_default()
    return (min(effective, trained) if trained else effective), trained


def looks_ollama(name: str, base_url: str) -> bool:
    return name.startswith("ollama") or ":11434" in base_url


def server_root(base_url: str) -> str:
    url = base_url.rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


async def probe(
    name: str,
    base_url: str,
    ids: list[str],
    client: httpx.AsyncClient,
    headers: dict | None = None,
    caps: dict | None = None,
) -> dict[str, int]:
    """Fenêtres connues des modèles `ids` d'un serveur local (vide si le serveur ne les expose pas). `caps` :
    rempli avec les capacités annoncées par Ollama ({modèle: {"tools", "vision", …}}), quand il les donne."""
    root, out = server_root(base_url), {}
    if looks_ollama(name, base_url):
        sem = asyncio.Semaphore(4)
        try:  # modèles déjà chargés : fenêtre EXACTE d'exécution
            r = await client.get(f"{root}/api/ps", headers=headers, timeout=2)
            loaded = (
                {
                    m.get("name"): _valid(m.get("context_length"))
                    for m in (r.json().get("models") or [])
                    if isinstance(m, dict)
                }
                if r.status_code == 200
                else {}
            )
        except (httpx.HTTPError, ValueError, AttributeError):
            loaded = {}

        async def show(mid: str):
            if loaded.get(mid):
                out[mid] = loaded[mid]
                if caps is None:
                    return
            async with sem:
                try:
                    r = await client.post(f"{root}/api/show", json={"model": mid}, headers=headers, timeout=3)
                    data = r.json() if r.status_code == 200 else None
                    if isinstance(data, dict) and ("model_info" in data or "modelfile" in data):  # vraie réponse
                        out.setdefault(mid, ollama_window(data)[0])
                        found = data.get("capabilities")
                        if caps is not None and isinstance(found, list):  # Ollama 0.6+ ; absent avant
                            caps[mid] = {str(c).lower() for c in found}
                except (httpx.HTTPError, ValueError, AttributeError):
                    pass

        await asyncio.gather(*(show(m) for m in ids))
        return out
    try:  # LM Studio : API native, plus précise
        r = await client.get(f"{root}/api/v0/models", headers=headers, timeout=2)
        items = r.json().get("data", []) if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError, AttributeError):
        items = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and it.get("id") in ids and item_context(it):
            out[it["id"]] = item_context(it)
    return out
