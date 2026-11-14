"""Modèles de Hugging Face dans la marketplace : recherche des dépôts GGUF, puis installation par Ollama.

Ollama sait télécharger un fichier GGUF d'un dépôt public de Hugging Face sous le nom
« hf.co/<auteur>/<dépôt>:<quantification> » : aucun programme en plus, le modèle arrive dans Ollama comme ceux de sa
bibliothèque. Ici, on lit seulement l'API publique de Hugging Face (sans compte ni clé) :
- recherche : dépôts publics au format GGUF, les plus téléchargés d'abord ;
- fichiers d'un dépôt : un seul fichier .gguf par quantification (les modèles découpés en plusieurs fichiers, les
  projecteurs d'images « mmproj » et les sous-dossiers sont écartés : Ollama ne les prend pas par ce nom).
Les dépôts à accès restreint (gated) ou privés sont écartés : Ollama ne peut pas les télécharger sans compte.
Ce qui est envoyé à Hugging Face : les mots cherchés et le nom du dépôt ouvert, rien d'autre (ni clé, ni nom de PC).
"""

from __future__ import annotations

import re
import time

import httpx

API = "https://huggingface.co/api/models"
SEARCH_LIMIT = 30
CACHE_TTL = 600  # secondes : la même recherche ne repart pas vers Hugging Face pendant 10 minutes
CACHE_MAX = 200
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
QUERY = re.compile(r"^[\w .:+/-]{1,80}$")
# quantification à la fin du nom de fichier : Q4_K_M, IQ4_XS, UD-Q4_K_XL, Q8_0, BF16…
QUANT = re.compile(r"[.\-_]((?:UD-)?(?:I?Q\d(?:_[A-Z0-9]+)*|BF16|F16|F32))\.gguf$", re.IGNORECASE)
SPLIT = re.compile(r"-\d{5}-of-\d{5}\.gguf$", re.IGNORECASE)
# licence → usage commercial (comme les licences des modèles d'Ollama, marketplace.py)
COMMERCIAL_OK = {
    "apache-2.0",
    "mit",
    "bsd",
    "bsd-2-clause",
    "bsd-3-clause",
    "openrail",
    "cc-by-4.0",
    "cc0-1.0",
    "unlicense",
}
COMMERCIAL_NO = re.compile(r"(^|-)nc($|-)|non-commercial|research", re.IGNORECASE)
COMMERCIAL_TERMS = re.compile(r"llama|gemma|qwen|deepseek|mistral|openrail\+\+|creativeml|other", re.IGNORECASE)

_cache: dict[str, tuple[float, object]] = {}
_sizes: dict[str, int] = {}  # « hf.co/auteur/dépôt:QUANT » → taille du fichier (contrôle de l'espace disque)


class HfError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def tag_of(repo: str, quant: str) -> str:
    return f"hf.co/{repo}:{quant.upper()}"


def size_hint(model: str) -> int | None:
    """Taille connue d'un modèle de Hugging Face (lue quand ses fichiers ont été affichés), pour l'espace disque."""
    return _sizes.get(model)


def license_of(name: str | None) -> dict:
    """Licence déclarée par le dépôt et ce qu'elle permet, en clair. Un dépôt GGUF est souvent la conversion d'un
    autre modèle : sa licence est celle du modèle d'origine, à vérifier sur sa page."""
    key = (name or "").strip().lower()
    if not key:
        return {
            "name": "Non indiquée",
            "commercial": "inconnu",
            "summary": "Aucune licence déclarée : à vérifier sur la page du modèle.",
        }
    if COMMERCIAL_NO.search(key):
        commercial, summary = "non", "Usage non commercial seulement."
    elif key in COMMERCIAL_OK:
        commercial, summary = "oui", "Libre, usage commercial compris."
    elif COMMERCIAL_TERMS.search(key):
        commercial, summary = "conditions", "Licence propre au modèle : lisez ses conditions sur la page du modèle."
    else:
        commercial, summary = "inconnu", "Licence à vérifier sur la page du modèle."
    return {"name": name, "commercial": commercial, "summary": summary}


def _license_tag(tags: list) -> str | None:
    for t in tags or []:
        if isinstance(t, str) and t.startswith("license:"):
            return t.split(":", 1)[1]
    return None


def _usable(d: dict) -> bool:
    return (
        isinstance(d, dict)
        and isinstance(d.get("id"), str)
        and bool(REPO.match(d["id"]))
        and not d.get("private")
        and not d.get("gated")
        and "gguf" in (d.get("tags") or [])
    )


def parse_search(rows: list) -> list[dict]:
    out = []
    for d in rows if isinstance(rows, list) else []:
        if not _usable(d):
            continue
        tags = [t for t in d.get("tags") or [] if isinstance(t, str)]
        base = next((t.split(":", 1)[1] for t in tags if t.startswith("base_model:") and t.count(":") == 1), None)
        out.append(
            {
                "repo": d["id"],
                "author": d["id"].split("/")[0],
                "downloads": int(d.get("downloads") or 0),
                "likes": int(d.get("likes") or 0),
                "task": d.get("pipeline_tag"),
                "base_model": base,
                "license": license_of(_license_tag(tags)),
                "vision": "image-text-to-text" in tags or d.get("pipeline_tag") == "image-text-to-text",
                "updated": d.get("lastModified") or d.get("createdAt"),
            }
        )
    return out


def parse_files(d: dict) -> dict:
    """Une ligne par quantification utilisable par Ollama, la plus petite d'abord."""
    if not _usable(d):
        raise HfError(404, "Dépôt introuvable, privé, à accès restreint ou sans fichier GGUF.")
    files = []
    for s in d.get("siblings") or []:
        name, size = s.get("rfilename"), s.get("size")
        if not isinstance(name, str) or "/" in name or "mmproj" in name.lower() or SPLIT.search(name):
            continue
        m = QUANT.search(name)
        if not m or not isinstance(size, int) or size <= 0:
            continue
        files.append({"file": name, "quant": m.group(1).upper(), "size": size})
    seen, rows = set(), []
    for f in sorted(files, key=lambda f: f["size"]):
        if f["quant"] not in seen:  # Ollama choisit le fichier par sa quantification : une seule ligne par nom
            seen.add(f["quant"])
            rows.append({**f, "tag": tag_of(d["id"], f["quant"])})
    card = d.get("cardData") if isinstance(d.get("cardData"), dict) else {}
    lic = card.get("license") if isinstance(card.get("license"), str) else _license_tag(d.get("tags"))
    gguf = d.get("gguf") if isinstance(d.get("gguf"), dict) else {}
    params = gguf.get("total")
    return {
        "repo": d["id"],
        "license": license_of(lic),
        "params_b": round(params / 1e9, 2) if isinstance(params, (int, float)) and params > 0 else None,
        "context": gguf.get("context_length") if isinstance(gguf.get("context_length"), int) else None,
        "vision": any("mmproj" in str(s.get("rfilename", "")).lower() for s in d.get("siblings") or []),
        "files": rows,
        "url": f"https://huggingface.co/{d['id']}",
    }


def _cached(key: str):
    hit = _cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < CACHE_TTL else None


def _keep(key: str, value) -> None:
    if len(_cache) >= CACHE_MAX:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    _cache[key] = (time.time(), value)


async def _get(client: httpx.AsyncClient, url: str, params: dict | None = None):
    try:
        r = await client.get(url, params=params, timeout=20, follow_redirects=False)
    except httpx.HTTPError:
        raise HfError(502, "Hugging Face injoignable : réessayez plus tard.") from None
    if r.status_code in (401, 403, 404):
        raise HfError(404, "Dépôt introuvable, privé ou à accès restreint.")
    if r.status_code == 429:
        raise HfError(429, "Hugging Face limite les recherches : réessayez dans une minute.")
    if r.status_code >= 400:
        raise HfError(502, f"Hugging Face a répondu {r.status_code}.")
    try:
        return r.json()
    except ValueError:
        raise HfError(502, "Réponse illisible de Hugging Face.") from None


async def search(client: httpx.AsyncClient, query: str) -> list[dict]:
    q = (query or "").strip()
    if q and not QUERY.match(q):
        raise HfError(400, "Recherche invalide : lettres, chiffres, espaces et . : + / - seulement (80 caractères).")
    key = "s:" + q.lower()
    if (hit := _cached(key)) is not None:
        return hit
    params = {"filter": "gguf", "sort": "downloads", "direction": "-1", "limit": str(SEARCH_LIMIT)}
    if q:
        params["search"] = q
    rows = parse_search(await _get(client, API, params))
    _keep(key, rows)
    return rows


async def files(client: httpx.AsyncClient, repo: str) -> dict:
    if not isinstance(repo, str) or not REPO.match(repo):
        raise HfError(400, "Nom de dépôt invalide (auteur/dépôt).")
    key = "f:" + repo.lower()
    if (hit := _cached(key)) is None:
        hit = parse_files(await _get(client, f"{API}/{repo}", {"blobs": "true"}))
        _keep(key, hit)
    for f in hit["files"]:
        _sizes[f["tag"]] = f["size"]
    return hit
