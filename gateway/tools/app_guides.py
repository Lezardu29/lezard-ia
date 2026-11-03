"""Guide des applications de ce PC pour le contrôle du PC : écrit par une IA forte (Claude Opus) à partir de
l'inventaire du PC (app_scan.py : installées, vraiment utilisées, ouvertes), lu par le modèle qui pilote le PC.
Le modèle de l'agent ne change pas et n'écrit rien ici : il lit seulement.

Fichier data/pc_apps.json (propre à ce PC, hors dépôt ; PC_APP_GUIDES pour un autre chemin) :
{"updated_at", "author", "apps": [{"id", "name", "usage" (souvent / parfois / rarement / jamais), "role",
 "aliases": [mots qui désignent l'application], "guide": [conseils courts]}]}

Sans encombrer le contexte : à chaque tâche, un index d'une ligne par application utilisée, puis le guide détaillé des
seules applications que la demande évoque (MAX_DETAILED) ; les autres se lisent à la demande (outil pc_app_guide).
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from pathlib import Path

from . import Tool, ToolError, ToolResult, register

log = logging.getLogger("gateway")

USAGES = ("souvent", "parfois", "rarement", "jamais")
MAX_DETAILED = 2  # guides détaillés joints d'office à une tâche
MAX_GUIDE_CHARS = 1500  # par application (le fichier est écrit à la main : garde-fou)
_cache: dict = {"key": None, "data": None}


def path() -> Path:
    return Path(os.getenv("PC_APP_GUIDES") or Path(os.getenv("GATEWAY_DATA_DIR", "data")) / "pc_apps.json")


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()


def load() -> list[dict]:
    """Applications du guide (relu seulement quand le fichier change) ; [] sans fichier ou s'il est abîmé."""
    p = path()
    try:
        stat = p.stat()
    except OSError:
        return []
    key = (str(p), stat.st_mtime_ns, stat.st_size)
    if _cache["key"] != key:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            apps = [a for a in data.get("apps", []) if isinstance(a, dict) and a.get("id") and a.get("name")]
        except (OSError, ValueError, AttributeError) as e:
            log.warning("Guide des applications illisible (%s) : %s", p, e)
            apps = []
        _cache.update(key=key, data=apps)
    return _cache["data"]


def find(name: str) -> dict | None:
    want = _plain(name).strip()
    for app in load():
        if want in (_plain(app["id"]), _plain(app["name"])) or want in map(_plain, app.get("aliases", [])):
            return app
    return None


def mentioned(task: str) -> list[dict]:
    """Applications que la demande évoque (nom ou alias, mot entier), les plus utilisées d'abord."""
    text = f" {re.sub(r'[^a-z0-9+#.]+', ' ', _plain(task))} "
    hits = []
    for app in load():
        names = [app["name"], app["id"], *app.get("aliases", [])]
        if any(f" {_plain(n).strip()} " in text for n in names if n and len(n) > 1):
            hits.append(app)
    rank = {u: i for i, u in enumerate(USAGES)}
    return sorted(hits, key=lambda a: rank.get(a.get("usage"), 9))


def detail(app: dict) -> str:
    lines = [f"{app['name']} ({app.get('role', '')}, utilisée {app.get('usage', '?')}) :".replace("(, ", "(")]
    lines += [f"- {g}" for g in app.get("guide", [])]
    return "\n".join(lines)[:MAX_GUIDE_CHARS]


def guide(task: str) -> str:
    """Bloc du message système : index des applications, guides détaillés de celles que la tâche évoque."""
    apps = load()
    if not apps:
        return ""
    used = [a for a in apps if a.get("usage") != "jamais"]
    unused = [a["name"] for a in apps if a.get("usage") == "jamais"]
    parts = [
        "Applications de ce PC (guide écrit d'après l'usage réel de l'utilisateur ; pc_app_guide donne le guide "
        "d'une application) :\n"
        + "\n".join(f"- {a['name']} : {a.get('role', '')} ({a.get('usage', '?')})" for a in used)
    ]
    if unused:
        parts.append(f"Installées mais pas utilisées récemment (ne pas les ouvrir sauf demande) : {', '.join(unused)}.")
    detailed = mentioned(task)[:MAX_DETAILED]
    if detailed:
        parts.append("Guide des applications de cette demande :\n" + "\n\n".join(detail(a) for a in detailed))
    return "\n\n".join(parts)


_SPEC = {
    "type": "object",
    "properties": {"app": {"type": "string", "description": "Nom de l'application (ex. Discord, VS Code)"}},
    "required": ["app"],
}


async def _run(args: dict) -> ToolResult:
    name = str(args.get("app") or "").strip()
    app = find(name) if name else None
    if app is None:
        known = ", ".join(a["name"] for a in load()) or "aucune"
        raise ToolError(f"Pas de guide pour « {name} ». Applications du guide : {known}.")
    return ToolResult(detail(app))


register(
    Tool(
        "pc_app_guide",
        "Guide d'une application de ce PC : comment l'ouvrir, s'y repérer et faire les actions courantes.",
        _SPEC,
        _run,
        level="pc",
    )
)
