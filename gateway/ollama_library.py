"""Bibliothèque publique d'Ollama (ollama.com/library) : tous les modèles, avec leurs tailles et capacités.

Rien ici n'installe quoi que ce soit : la page Modèles liste ce qui existe, et le propriétaire choisit. La liste est
lue une fois par jour et gardée dans les réglages ; si ollama.com est injoignable, la dernière liste lue sert.
Les tailles viennent des cartes du catalogue (« 8b », « 270m ») ; la taille du téléchargement est estimée
(quantification Q4_K_M, comme le catalogue de model_catalog.py), le test de performance donne la vitesse réelle.
"""

from __future__ import annotations

import html
import re
import time

import httpx

from .model_catalog import BYTES_PER_WEIGHT

URL = "https://ollama.com/library"
SETTING = "ollama_library"
TTL = 24 * 3600
CARD = re.compile(r"<li\s.*?</li>", re.S)
NAME = re.compile(r'href="/library/([a-z0-9][a-z0-9._-]*)"')
DESC = re.compile(r'<p class="max-w-lg[^"]*">(.*?)</p>', re.S)
# minuscules seulement : « 8b », « 270m » ; les nombres de téléchargements (« 120.7M ») sont en majuscules
SIZE = re.compile(r">\s*(\d+(?:\.\d+)?)([bm])\s*<")
CAP = re.compile(r">\s*(vision|tools|thinking|embedding|cloud)\s*<")
TAG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}(:[a-z0-9][a-z0-9._-]{0,63})?$")


def parse(page: str) -> list[dict]:
    """Une entrée par modèle : nom, description, capacités, tailles (en milliards de paramètres) et étiquettes."""
    out = []
    for card in CARD.findall(page):
        name = NAME.search(card)
        if not name:
            continue
        sizes = [(float(v) if u == "b" else float(v) / 1000, f"{v}{u}") for v, u in SIZE.findall(card)]
        if not sizes:  # modèle seulement disponible dans le cloud d'Ollama : rien à installer ici
            continue
        desc = DESC.search(card)
        out.append(
            {
                "name": name.group(1),
                "description": " ".join(html.unescape(re.sub(r"<[^>]+>", " ", desc.group(1))).split()) if desc else "",
                "capabilities": sorted(set(CAP.findall(card))),
                "sizes": [{"params_b": p, "label": label} for p, label in dict.fromkeys(sizes)],
            }
        )
    return out


async def fetch(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(URL, timeout=30, follow_redirects=False)
    r.raise_for_status()
    models = parse(r.text)
    if not models:
        raise ValueError("page de la bibliothèque sans modèle reconnu")
    return models


async def cached(store, client: httpx.AsyncClient) -> dict:
    """{models, fetched_at, stale} : la liste du jour, ou la dernière lue si ollama.com ne répond pas."""
    saved = store.get_setting(SETTING) or {}
    if saved.get("models") and time.time() - saved.get("at", 0) < TTL:
        return {"models": saved["models"], "fetched_at": saved["at"], "stale": False}
    try:
        models = await fetch(client)
    except (httpx.HTTPError, ValueError):
        if saved.get("models"):
            return {"models": saved["models"], "fetched_at": saved["at"], "stale": True}
        raise
    store.set_setting(SETTING, {"at": time.time(), "models": models})
    return {"models": models, "fetched_at": time.time(), "stale": False}


def tag_size_gb(params_b: float) -> float:
    """Taille du téléchargement (Go) : poids quantifiés en Q4_K_M."""
    return max(params_b * BYTES_PER_WEIGHT["q4_K_M"], 0.001)  # 1 Mo au moins : un modèle minuscule reste mesurable


def uses(capabilities: list[str]) -> tuple[str, ...]:
    if "embedding" in capabilities:
        return ("embeddings",)
    if "vision" in capabilities:
        return ("chat", "vision")
    return ("chat",)
