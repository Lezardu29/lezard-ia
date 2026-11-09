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

Mémoire (« auto » et la carte graphique) : poids (taille d'/api/tags) + cache KV pour la fenêtre effective, calculé
d'après l'architecture d'/api/show (couches, têtes KV, couches à fenêtre glissante de Gemma), + marge d'exécution ;
un modèle déjà chargé donne la vérité (/api/ps : taille totale).
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


RUNTIME_BYTES = 0.5e9  # contexte CUDA, tampons de calcul
KV_BYTES = 2  # cache KV en fp16 (défaut d'Ollama)
SWA_GLOBAL_EVERY = 6  # Gemma 3 : une couche globale sur six quand le motif n'est pas publié


def memory_need(show: dict, window: int, weights: int | None) -> int | None:
    """Mémoire (octets) d'un modèle Ollama chargé avec `window` tokens : poids + cache KV + marge."""
    if not weights:
        return None
    info = show.get("model_info") or {}
    arch = info.get("general.architecture") or next(
        (k.split(".")[0] for k in info if k.endswith(".block_count") and k.count(".") == 1), ""
    )

    def field(name: str):  # champ du modèle de texte (pas « gemma4.vision.block_count »)
        return info.get(f"{arch}.{name}")

    layers, kv_heads, heads = field("block_count"), field("attention.head_count_kv"), field("attention.head_count")
    if isinstance(kv_heads, list):  # par couche (certains modèles)
        kv_heads = max(kv_heads) if kv_heads else None
    width = field("embedding_length")
    k_len = field("attention.key_length") or (width // heads if width and heads else None)
    v_len = field("attention.value_length") or k_len
    if not (isinstance(layers, int) and isinstance(kv_heads, int) and k_len and v_len):
        return int(weights + RUNTIME_BYTES)  # architecture inconnue : poids seuls
    per_token = kv_heads * (k_len + v_len) * KV_BYTES
    pattern, sliding = field("attention.sliding_window_pattern"), field("attention.sliding_window")
    if isinstance(pattern, list) and pattern:  # True : couche à fenêtre glissante (cache limité à la fenêtre)
        full = sum(1 for x in pattern if not x)
    elif isinstance(sliding, int) and sliding:
        full = max(1, layers // SWA_GLOBAL_EVERY)
    else:
        full, sliding = layers, 0
    kv = full * per_token * window + (layers - full) * per_token * min(window, sliding or window)
    return int(weights + kv + RUNTIME_BYTES)


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
    memory: dict | None = None,
) -> dict[str, int]:
    """Fenêtres connues des modèles `ids` d'un serveur local (vide si le serveur ne les expose pas). `caps` :
    rempli avec les capacités annoncées par Ollama ({modèle: {"tools", "vision", …}}), quand il les donne.
    `memory` : rempli avec la mémoire de chaque modèle chargé avec sa fenêtre ({modèle: octets}), mesurée s'il est
    chargé, sinon estimée."""
    root, out = server_root(base_url), {}
    if looks_ollama(name, base_url):
        sem = asyncio.Semaphore(4)
        try:  # modèles déjà chargés : fenêtre EXACTE d'exécution
            r = await client.get(f"{root}/api/ps", headers=headers, timeout=2)
            running = [m for m in (r.json().get("models") or []) if isinstance(m, dict)] if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError, AttributeError):
            running = []
        loaded = {m.get("name"): _valid(m.get("context_length")) for m in running}
        weights: dict = {}
        if memory is not None:
            for m in running:  # chargé : mémoire réelle (sa part dans la carte dépend des autres modèles chargés)
                if isinstance(m.get("size"), int) and m.get("name") in ids:
                    memory[m["name"]] = m["size"]
            try:
                r = await client.get(f"{root}/api/tags", headers=headers, timeout=3)
                tags = (r.json().get("models") or []) if r.status_code == 200 else []
                weights = {t.get("name"): t.get("size") for t in tags if isinstance(t, dict)}
            except (httpx.HTTPError, ValueError, AttributeError):
                weights = {}

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
                        size = weights.get(mid)
                        if memory is not None and mid not in memory and isinstance(size, int):
                            need = memory_need(data, ollama_window(data)[0], size)
                            if need:
                                memory[mid] = need
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
