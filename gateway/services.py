"""Clés des services branchés comme serveurs MCP (Brave Search, GitHub…), gérées dans la page Modèles.

Plusieurs clés par service, chacune avec une étiquette et un accès :
- GitHub : « tous les dépôts » ou une liste de dépôts précis (owner/repo). La passerelle refuse les appels visant un
  autre dépôt, et les outils qui ne désignent pas de dépôt (recherche, profil…). La vraie limite reste celle du jeton
  sur GitHub : réglez-la aussi là-bas.
- Brave : pas de notion de dépôt, la clé sert à la recherche.

Chaque clé lance son propre serveur MCP, nommé « <service>-<étiquette> » : ses outils s'appellent
mcp_<service>-<étiquette>_<outil>. Les clés sont chiffrées comme celles des fournisseurs (table provider_credentials,
nom « svc:<identifiant> ») ; leurs métadonnées (étiquette, accès) sont dans les réglages (non secrètes). Jamais
réaffichées : la gateway ne renvoie qu'un aperçu masqué.
"""

from __future__ import annotations

import os
from pathlib import Path
import re

from .crypto import mask

PREFIX = "svc:"
SETTING = "service_keys"
MAX_REPOS = 50
REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


def _mcp_dir(rel: str) -> str:
    """Serveurs MCP installés pour cette installation : <dossier de données>/mcp/…"""
    return (Path(os.getenv("GATEWAY_DATA_DIR", "data")) / "mcp" / rel).as_posix()


def _node() -> str:
    return os.getenv("MCP_NODE", "C:/Program Files/nodejs/node.exe")


SERVICES = {
    "brave": {
        "label": "Brave Search",
        "url": "https://brave.com/search/api/",
        "env": "BRAVE_API_KEY",
        "scope": False,
        "command": lambda: [
            _node(),
            os.getenv("MCP_BRAVE_JS", _mcp_dir("brave/node_modules/@brave/brave-search-mcp-server/dist/index.js")),
        ],
        "args": [],
    },
    "github": {
        "label": "GitHub",
        "url": "https://github.com/settings/tokens",
        "env": "GITHUB_PERSONAL_ACCESS_TOKEN",
        "scope": True,
        "command": lambda: [os.getenv("MCP_GITHUB_BIN", _mcp_dir("github/bin/github-mcp-server.exe"))],
        "args": ["stdio", "--read-only"],
    },
}


class ServiceError(ValueError):
    """Erreur de saisie d'une clé (message destiné à l'administrateur)."""


def provider_name(kid: str) -> str:
    """Nom sous lequel le secret est stocké (et chiffré : c'est aussi le contexte du chiffrement)."""
    return f"{PREFIX}{kid}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _keys(store) -> list[dict]:
    data = store.get_setting(SETTING) or {}
    return list(data.get("keys") or [])


def _save(store, keys: list[dict]) -> None:
    store.set_setting(SETTING, {"keys": keys})


def clean_scope(service: str, scope) -> dict:
    """Accès d'une clé : {"mode": "all"} ou {"mode": "repos", "repos": ["owner/repo", ...]}."""
    if not SERVICES[service]["scope"] or scope in (None, "", {}):
        return {"mode": "all"}
    if not isinstance(scope, dict) or scope.get("mode") not in ("all", "repos"):
        raise ServiceError("'scope' : mode « all » (tous les dépôts) ou « repos » (dépôts précis).")
    if scope["mode"] == "all":
        return {"mode": "all"}
    raw = scope.get("repos") or []
    if not isinstance(raw, list):
        raise ServiceError("'scope.repos' doit être une liste de dépôts « owner/repo ».")
    repos = list(dict.fromkeys(str(r).strip() for r in raw if str(r).strip()))
    if not repos:
        raise ServiceError("Indiquez au moins un dépôt (owner/repo), ou choisissez « tous les dépôts ».")
    if len(repos) > MAX_REPOS:
        raise ServiceError(f"Au plus {MAX_REPOS} dépôts par clé.")
    bad = [r for r in repos if not REPO.match(r)]
    if bad:
        raise ServiceError(f"Dépôt invalide : « {bad[0]} » (attendu : owner/repo).")
    return {"mode": "repos", "repos": repos}


def _new_id(store, service: str, label: str) -> str:
    slug = _slug(label)
    base = (slug if slug.startswith(service) else f"{service}-{slug}")[:24].rstrip("-") or f"{service}-cle"
    taken = {k["id"] for k in _keys(store)}
    kid, n = base, 2
    while kid in taken:
        suffix = f"-{n}"
        kid = base[: 24 - len(suffix)] + suffix
        n += 1
    return kid


def create_key(store, cipher, service: str, label: str, secret: str, scope) -> dict:
    if service not in SERVICES:
        raise ServiceError(f"Service inconnu « {service} ».")
    label = label.strip()[:40]
    if not label:
        raise ServiceError("L'étiquette est requise (ex. « GitHub perso »).")
    if not secret or len(secret.strip()) < 8:
        raise ServiceError("La clé est requise (au moins 8 caractères).")
    secret = secret.strip()
    keys = _keys(store)
    kid = _new_id(store, service, label)
    entry = {"id": kid, "service": service, "label": label, "scope": clean_scope(service, scope)}
    name = provider_name(kid)
    store.set_credential(name, cipher.encrypt(secret, name), mask(secret), cipher.fingerprint)
    _save(store, keys + [entry])
    return entry


def delete_key(store, kid: str) -> bool:
    keys = _keys(store)
    left = [k for k in keys if k["id"] != kid]
    if len(left) == len(keys):
        return False
    store.delete_credential(provider_name(kid))
    _save(store, left)
    return True


def secret_for(store, cipher, kid: str) -> str | None:
    name = provider_name(kid)
    raw = store.get_credential(name)
    return cipher.decrypt(raw, name) if raw else None


def overview(store) -> dict:
    """Ce que la page affiche : les services disponibles, et les clés (aperçu masqué, accès, configurée ou non)."""
    stored = {c["provider"]: c for c in store.list_credentials()}
    keys = []
    for k in _keys(store):
        c = stored.get(provider_name(k["id"]))
        keys.append(
            {
                **k,
                "configured": c is not None,
                "hint": c["hint"] if c else None,
                "updated_at": c["updated_at"] if c else None,
            }
        )
    catalog = [{"id": sid, "label": s["label"], "url": s["url"], "scope": s["scope"]} for sid, s in SERVICES.items()]
    return {"catalog": catalog, "keys": keys}


def server_configs(store, cipher) -> dict[str, dict]:
    """Un serveur MCP par clé configurée, prêt pour mcp.load (la clé n'existe qu'ici, au lancement)."""
    out: dict[str, dict] = {}
    for k in _keys(store):
        secret = secret_for(store, cipher, k["id"])
        if not secret:
            continue
        spec = SERVICES[k["service"]]
        cmd = spec["command"]()
        scope = k.get("scope") or {"mode": "all"}
        out[k["id"]] = {
            "command": cmd[0],
            "args": [*cmd[1:], *spec["args"]],
            "env": {spec["env"]: secret},
            "label": k["label"],
            **({"scope": scope} if scope.get("mode") == "repos" else {}),
        }
    return out
