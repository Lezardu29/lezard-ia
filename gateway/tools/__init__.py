"""Outils exécutés par la gateway (function calling côté serveur, phases 21 à 26) : le modèle demande un outil,
la gateway l'exécute et lui renvoie le résultat, jusqu'à la réponse finale.

Clé maître, ou clé virtuelle avec la permission « tools ». Niveaux : basic, web (sauf TOOLS_WEB=false), pc (TOOLS_PC=true, Windows ;
modèle local uniquement et validation humaine avant chaque action)."""

from __future__ import annotations

import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


class ToolError(Exception):
    """Échec d'un outil : le message est renvoyé au modèle, qui peut s'adapter."""


class RuleRefusal(ToolError):
    """Refus d'une règle de la méthode (la souris d'abord…), pas un échec sur le PC : jamais retenu en mémoire."""


class StopRun(ToolError):
    """Arrêt d'urgence : la boucle s'arrête immédiatement."""


@dataclass
class ToolResult:
    text: str
    image: str | None = None  # data URI joint au modèle (capture d'écran)
    attach: str | None = None  # texte joint à l'image (liste des éléments), retiré du contexte avec elle
    no_effect: bool = False  # action faite, mais l'écran n'a pas changé (mémoire : compte comme une erreur)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[[dict], Awaitable[ToolResult]]
    level: str = "basic"  # basic | web | pc | mcp | docs
    confirm: bool = False  # validation humaine avant chaque exécution
    local_only: bool = False  # refusé avec un modèle cloud (l'écran ne quitte pas le PC)
    preview: Callable[[dict], str | None] | None = None  # image montrée pour la validation
    # capture encore valable (message au modèle) : refusée si cette réponse a déjà une capture récente
    fresh: Callable[[], str | None] | None = None

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


TOOLS: dict[str, Tool] = {}

PC_GUIDE = (
    "Tu peux contrôler ce PC Windows avec les outils pc_*. Commence par pc_screenshot. Ensuite, à chaque tour :\n"
    "1. Lis le texte joint à la capture : il donne la fenêtre au premier plan et les fenêtres ouvertes (fiable). "
    "Puis regarde l'image et écris dans 'reason' : quelle fenêtre est affichée, ce qu'on y lit, si c'est la bonne "
    "pour l'objectif, et l'élément visé avec son numéro.\n"
    "2. Si la fenêtre au premier plan n'est pas celle qu'il faut : clique son bouton dans la barre des tâches (liste "
    "des éléments), à défaut pc_focus ; application pas encore ouverte : pc_open avec son nom (le plus fiable), ou "
    "son icône si elle est visible. Ne fais jamais défiler ni ne clique dans une fenêtre qui n'est pas la bonne.\n"
    "3. Une seule action par tour ; chaque action renvoie une nouvelle capture : ne redemande pas pc_screenshot.\n"
    "4. LA SOURIS D'ABORD : chaque capture liste les éléments cliquables avec un numéro jaune. Pour ouvrir, choisir, "
    "valider ou aller quelque part, clique l'élément avec pc_click et 'element' = son numéro (icônes de la barre des "
    "tâches, menus, boutons, liens, onglets, dossiers). Double-clic ('button': 'double') pour ouvrir un fichier ou un "
    "dossier. Élément absent de la liste : 'cell' (grille), ou pc_zoom puis 'zoom'.\n"
    "5. Le clavier pour écrire du texte (pc_type, après avoir cliqué dans le champ), puis pc_key « enter » pour "
    "valider (recherche, formulaire, message). Sinon pc_key seulement si aucun élément ne convient.\n"
    "6. Si la capture indique que l'écran n'a pas changé, l'action a échoué : change d'approche, ne la répète pas.\n"
    "7. web_search et web_fetch lisent Internet depuis le serveur : ils n'ouvrent RIEN à l'écran. Pour ouvrir un site "
    "sur le PC : pc_open_url.\n"
    "8. N'annonce jamais une action que tu n'as pas faite avec un outil pc_* et vérifiée sur la capture suivante.\n"
    "9. Quand l'objectif est atteint, ou si tu es bloqué, arrête d'appeler des outils et réponds brièvement : ce que "
    "tu as fait et ce que tu as vu à l'écran."
)


def register(*tools: Tool) -> None:
    for t in tools:
        TOOLS[t.name] = t


def pc_enabled() -> bool:
    return sys.platform == "win32" and os.getenv("TOOLS_PC", "false").lower() == "true"


def web_enabled() -> bool:
    """Recherche web (/web) : activée sauf TOOLS_WEB=false (choisi à l'installation)."""
    return os.getenv("TOOLS_WEB", "true").lower() not in ("0", "false", "no", "off")


def available(ctx) -> dict[str, Tool]:
    """Outils permis : tous pour la clé maître ; pour une clé virtuelle, le niveau de sa permission « tools »
    (aucun par défaut)."""
    if ctx.is_admin:
        granted = "pc"
    else:
        granted = (getattr(ctx, "permissions", None) or {}).get("tools")
    if granted not in ("basic", "web", "pc"):
        return {}
    levels = {"basic"} | ({"web"} if granted in ("web", "pc") and web_enabled() else set())
    if granted == "pc" and pc_enabled():
        levels.add("pc")
    if ctx.is_admin:
        levels |= {"mcp", "docs"}  # serveurs MCP (mcp.py), Paperless et Nextcloud (sources.py) : clé maître seulement
    return {n: t for n, t in TOOLS.items() if t.level in levels}


from . import app_guides, basic, web_tools  # noqa: E402,F401  (enregistrement)

# arrêt d'urgence surveillé pendant toute une tâche sur le PC : () -> (message si déclenché, sinon None)
FAILSAFE: Callable[[], str | None] | None = None

if sys.platform == "win32":
    from . import pc  # noqa: E402

    FAILSAFE = lambda: pc.EMERGENCY if pc.in_corner() else None  # noqa: E731


def actor(ctx) -> str:
    """Qui agit sur le PC : chaque clé virtuelle a ses propres validations."""
    key_id = (ctx.key or {}).get("id")
    return f"{ctx.identity}:{key_id}" if key_id and not ctx.is_admin else ctx.identity
