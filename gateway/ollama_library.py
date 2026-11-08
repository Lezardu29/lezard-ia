"""Bibliothèque publique d'Ollama (ollama.com/library) : tous les modèles, avec leurs tailles et capacités.

Rien ici n'installe quoi que ce soit : la page Modèles liste ce qui existe, et le propriétaire choisit. La liste est
lue une fois par jour et gardée dans les réglages ; si ollama.com est injoignable, la dernière liste lue sert.
Les tailles viennent des cartes du catalogue (« 8b », « 270m ») ; la taille du téléchargement est alors estimée
(quantification Q4_K_M, comme le catalogue de model_catalog.py), le test de performance donne la vitesse réelle.
Une carte sans taille lisible (« 8x7b », « e4b », ou aucune) : les tailles et téléchargements exacts sont lus sur la
page de ses étiquettes (/library/<nom>/tags). Les modèles « cloud » d'Ollama (rien à faire tourner sur ce PC : ils
tournent chez Ollama, avec un compte ollama.com) sont listés aussi, avec leur étiquette (« kimi-k3:cloud »).
"""

from __future__ import annotations

import asyncio
import html
import re
import time

import httpx

from .model_catalog import BYTES_PER_WEIGHT
from .registry import is_ollama_cloud

URL = "https://ollama.com/library"
SETTING = "ollama_library"
TTL = 24 * 3600
CARD = re.compile(r"<li\s.*?</li>", re.S)
NAME = re.compile(r'href="/library/([a-z0-9][a-z0-9._-]*)"')
DESC = re.compile(r'<p class="max-w-lg[^"]*">(.*?)</p>', re.S)
# minuscules seulement : « 8b », « 270m » ; les nombres de téléchargements (« 120.7M ») sont en majuscules
SIZE = re.compile(r">\s*(\d+(?:\.\d+)?)([bm])\s*<")
CAP = re.compile(r">\s*(vision|tools|thinking|embedding|cloud)\s*<")
# toutes les tailles affichées sur la carte, même celles qu'on ne sait pas convertir (« 8x7b », « e4b »)
LABEL = re.compile(r">\s*((?:\d+x)?e?\d+(?:\.\d+)?[bm])\s*<")
QUANT = re.compile(r"^(q\d|iq\d|fp\d|bf\d|f\d)", re.I)  # étiquettes de quantification (« q8_0 », « bf16 »)
TAG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}(:[a-z0-9][a-z0-9._-]{0,63})?$")
# page des étiquettes : « gemma3n:e2b 0a377808afea • 5.6GB • 32K context window »
TAG_ROW = r"{name}:([A-Za-z0-9][A-Za-z0-9._-]*)\s+[0-9a-f]{{12}}\s+•\s+(?:(\d+(?:\.\d+)?)\s*(GB|MB|KB)|[^•<]*)"
TAG_PAGES = 4  # pages d'étiquettes lues à la fois


def is_cloud_tag(tag: str) -> bool:
    """Modèle « cloud » d'Ollama (« kimi-k3:cloud ») : la même règle que le routage (registry.is_ollama_cloud)."""
    return is_ollama_cloud(tag)


def size_gb(size: dict) -> float:
    """Téléchargement (Go) d'une taille : exact s'il a été lu sur ollama.com, sinon estimé (Q4_K_M)."""
    if size.get("download_gb") is not None:
        return size["download_gb"]
    return tag_size_gb(size["params_b"])


def public_size(name: str, size: dict) -> dict:
    """Une taille telle que les marketplaces la montrent (téléchargement en octets ; « cloud » : chez Ollama)."""
    return {
        "tag": f"{name}:{size['label']}",
        "params_b": size["params_b"],
        "download": size_gb(size) * 1e9,
        **({"cloud": True} if size.get("cloud") else {}),
    }


def parse(page: str) -> list[dict]:
    """Une entrée par modèle : nom, description, capacités, tailles (en milliards de paramètres) et étiquettes.
    Une carte sans taille lisible garde « sizes » vide et « pending » : ses étiquettes sont lues ensuite (fetch)."""
    out = []
    for card in CARD.findall(page):
        name = NAME.search(card)
        if not name:
            continue
        sizes = [(float(v) if u == "b" else float(v) / 1000, f"{v}{u}") for v, u in SIZE.findall(card)]
        desc = DESC.search(card)
        caps = sorted(set(CAP.findall(card)))
        out.append(
            {
                "name": name.group(1),
                "description": " ".join(html.unescape(re.sub(r"<[^>]+>", " ", desc.group(1))).split()) if desc else "",
                "capabilities": caps,
                "sizes": [{"params_b": p, "label": label} for p, label in dict.fromkeys(sizes)],
                **({} if sizes else {"pending": "cloud" if "cloud" in caps else "tags", "labels": LABEL.findall(card)}),
            }
        )
    return out


def parse_tags(name: str, page: str, cloud: bool, labels: list[str] | None = None) -> list[dict]:
    """Tailles d'un modèle d'après la page de ses étiquettes. Modèle local : une par taille de téléchargement
    distincte, sous la taille affichée sur sa carte (« 8x7b », « e4b ») ou à défaut son étiquette la plus courte sans
    variante de quantification (« v1.5 », « latest ») ;
    paramètres estimés d'après le téléchargement (Q4_K_M). Modèle cloud : ses étiquettes « cloud »."""
    rows = re.findall(TAG_ROW.format(name=re.escape(name)), re.sub(r"<[^>]+>", " ", page))
    if cloud:
        tags = list(dict.fromkeys(t for t, *_ in rows if is_cloud_tag(f"{name}:{t}")))
        return [{"params_b": 0, "label": t, "download_gb": 0, "cloud": True} for t in tags]
    unit = {"GB": 1, "MB": 1e-3, "KB": 1e-6}
    by_size: dict[float, list[str]] = {}
    for tag, value, u in rows:
        if not value or "-" in tag or QUANT.match(tag) or is_cloud_tag(f"{name}:{tag}"):  # variantes : pas proposées
            continue
        by_size.setdefault(round(float(value) * unit[u], 3), []).append(tag)
    if not by_size:  # seulement des variantes (« 7b-q4_0 ») : la quantification habituelle, une par taille
        for wanted in ("q4_k_m", "q4_0"):
            for tag, value, u in rows:
                if value and tag.lower().endswith(wanted) and not is_cloud_tag(f"{name}:{tag}"):
                    by_size.setdefault(round(float(value) * unit[u], 3), []).append(tag)
            if by_size:
                break
        return [
            {"params_b": round(gb / BYTES_PER_WEIGHT["q4_K_M"], 1), "label": min(tags, key=len), "download_gb": gb}
            for gb, tags in sorted(by_size.items())
        ]
    labels = sorted(dict.fromkeys(labels or ()), key=_label_size)
    # autant de tailles sur la carte que de téléchargements distincts : dans l'ordre (la page des étiquettes ne
    # donne pas toujours la taille de « 8x7b », partagée avec « latest »)
    ordered = len(labels) == len(by_size)
    out = []
    for i, (gb, tags) in enumerate(sorted(by_size.items())):
        shown = [t for t in tags if t in labels]
        named = [t for t in tags if t != "latest"]
        label = shown[0] if shown else labels[i] if ordered else "latest" if "latest" in tags else min(named, key=len)
        out.append({"params_b": round(gb / BYTES_PER_WEIGHT["q4_K_M"], 1), "label": label, "download_gb": gb})
    return out


def _label_size(label: str) -> float:
    """Ordre des tailles d'une carte : « 8x7b » = 56 milliards, « e4b » = 4, « 270m » = 0,27."""
    m = re.fullmatch(r"(?:(\d+)x)?e?(\d+(?:\.\d+)?)([bm])", label)
    if not m:
        return 0.0
    n = float(m.group(2)) * (int(m.group(1)) if m.group(1) else 1)
    return n if m.group(3) == "b" else n / 1000


async def fetch(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(URL, timeout=30, follow_redirects=False)
    r.raise_for_status()
    models = parse(r.text)
    if not models:
        raise ValueError("page de la bibliothèque sans modèle reconnu")
    gate = asyncio.Semaphore(TAG_PAGES)

    async def complete(m: dict) -> None:
        cloud, labels = m.pop("pending") == "cloud", m.pop("labels", [])
        try:
            async with gate:
                page = await client.get(f"{URL}/{m['name']}/tags", timeout=30, follow_redirects=False)
            page.raise_for_status()
            m["sizes"] = parse_tags(m["name"], page.text, cloud, labels)
        except httpx.HTTPError:
            m["sizes"] = []
        if cloud and not m["sizes"]:  # page illisible : l'étiquette habituelle
            m["sizes"] = [{"params_b": 0, "label": "cloud", "download_gb": 0, "cloud": True}]

    await asyncio.gather(*(complete(m) for m in models if "pending" in m))
    return [m for m in models if m["sizes"]]


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
