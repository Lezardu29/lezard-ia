"""Compatibilité des éléments de la marketplace entre eux, et bilan de ce qui est installé sur ce PC.

Chaque élément s'installe à part (son dossier, sa version : winget, npm, uv, extension VS Code, compléments de Claude
Code) : en installer beaucoup ne casse pas Lezard IA, et chacun se désinstalle depuis la marketplace. Ce qui peut gêner,
c'est ce qui tourne ou agit EN MÊME TEMPS : la mémoire et la carte graphique, les serveurs MCP (tous lancés avec
Lezard IA), le nombre d'outils proposés à une IA locale, et les compléments qui donnent des consignes contraires.

RULES : groupes d'éléments (« type:id », comme la marketplace) qui se gênent ou font double emploi quand au moins
`min` d'entre eux sont installés. `level` : « conflit » (à éviter ensemble), « attention » (ensemble, avec une
précaution), « doublon » (compatibles, mais l'un suffit). Les éléments absents du catalogue sont ignorés.
"""

from __future__ import annotations

from collections.abc import Iterable

GENERAL = (
    "Chaque outil s'installe à part, dans son propre dossier et à sa version : en installer beaucoup ne casse pas "
    "Lezard IA, et chacun se désinstalle d'un clic ici. Ce qui peut gêner, c'est ce qui tourne en même temps : la "
    "mémoire et la carte graphique, les serveurs MCP (tous démarrés avec Lezard IA) et le nombre d'outils proposés à "
    "une IA locale. Le bilan « Sur ce PC » vous prévient quand c'est le cas."
)

KIND_NOTES = {
    "mcp": (
        "Démarré avec Lezard IA, comme tous les serveurs MCP déclarés (un petit programme chacun, quelques dizaines de "
        "Mo de mémoire). Il ne sert dans le chat que si vous l'activez pour la conversation ou avec /mcp."
    ),
    "app": "Application séparée : elle ne change rien au fonctionnement de Lezard IA, elle s'y branche.",
    "tool": "Outil séparé : il ne change rien au fonctionnement de Lezard IA.",
    "component": "Fonction de Lezard IA elle-même, testée avec toutes les autres fonctions.",
}

RULES = (
    {
        "ids": ("tool:superpowers", "tool:superclaude", "tool:bmad", "tool:wshobson", "tool:speckit"),
        "level": "conflit",
        "title": "Méthodes de travail pour Claude Code",
        "text": "Chacune donne à Claude Code sa propre façon de travailler (étapes, commandes, rôles). Plusieurs à la "
        "fois se contredisent et remplissent sa mémoire de consignes : il suit l'une, puis l'autre, et une IA locale "
        "s'y perd. Gardez-en une seule active.",
    },
    {
        "ids": ("mcp:memoire", "mcp:agentmemory", "tool:agentmemory", "tool:claudemem"),
        "level": "doublon",
        "title": "Plusieurs mémoires",
        "text": "Chacune garde ses propres souvenirs : avec plusieurs, l'IA note la même chose deux fois ou ne retrouve "
        "pas ce qu'elle a rangé dans l'autre. Une seule suffit.",
    },
    {
        "ids": (
            "mcp:brave",
            "mcp:tavily",
            "mcp:exa",
            "mcp:firecrawl",
            "mcp:perplexity",
            "mcp:searxng",
            "component:web",
        ),
        "level": "doublon",
        "title": "Plusieurs recherches sur Internet",
        "text": "Elles marchent ensemble, mais l'IA doit choisir entre des outils qui font la même chose et se trompe "
        "plus souvent. La recherche de Lezard IA (/web) suffit en général ; n'activez qu'une des autres dans une "
        "conversation.",
    },
    {
        "ids": ("mcp:playwright", "mcp:chromedevtools"),
        "level": "doublon",
        "title": "Deux pilotes de navigateur",
        "text": "Tous deux ouvrent et commandent un navigateur. Ensemble, ils proposent plus de 50 outils : trop pour une "
        "IA locale. Activez-en un seul par conversation.",
    },
    {
        "ids": ("mcp:desktopcommander", "mcp:fichiers", "component:pc"),
        "level": "attention",
        "title": "Plusieurs accès à vos fichiers et à votre PC",
        "text": "Ils se complètent, mais chacun peut lire ou modifier vos fichiers ; Desktop Commander peut aussi lancer "
        "des commandes. Ne les activez ensemble que pour une tâche qui en a besoin, dans un dossier de travail.",
    },
    {
        "ids": ("app:comfyui", "component:ollama"),
        "level": "attention",
        "title": "Images et IA locale sur la même carte graphique",
        "text": "ComfyUI et les modèles d'Ollama se partagent la mémoire de la carte graphique : utilisés en même temps, "
        "l'un des deux ralentit beaucoup ou s'arrête faute de mémoire. Pas de souci l'un après l'autre.",
    },
    {
        "ids": ("app:continue", "app:cline", "app:kilocode"),
        "level": "attention",
        "title": "Plusieurs assistants dans VS Code",
        "text": "Ils s'installent ensemble sans problème. Mais deux complétions automatiques actives à la fois se "
        "marchent dessus (suggestions en double, saisie ralentie) : laissez la complétion à un seul.",
    },
    {
        "ids": ("app:openwebui", "app:librechat", "app:anythingllm", "app:jan", "app:chatbox", "app:cherrystudio"),
        "level": "doublon",
        "title": "Plusieurs interfaces de chat",
        "text": "Compatibles : toutes se branchent sur Lezard IA et peuvent rester installées. Elles font à peu près la "
        "même chose que le chat de Lezard IA ; Open WebUI et LibreChat tournent en service et gardent de la mémoire "
        "occupée tant qu'ils sont démarrés.",
    },
    {
        "ids": ("tool:rokit", "tool:rojo", "tool:wally", "tool:stylua", "tool:selene"),
        "level": "attention",
        "title": "Rokit et les outils Roblox installés à part",
        "text": "Rokit installe lui-même Rojo, Wally, StyLua ou selene à la version de chaque projet. Avec les mêmes "
        "outils installés à part, deux versions existent : dans un dossier qui a un rokit.toml, c'est celle de Rokit "
        "qui sert. Ce n'est gênant que si les versions diffèrent beaucoup.",
    },
)

MCP_MANY = 6  # serveurs MCP déclarés : au-delà, démarrage plus long et mémoire occupée en permanence
TOOLS_MANY = 40  # outils MCP en tout : au-delà, une IA locale choisit mal si on lui propose tout (/mcp seul)


def _present(rule: dict, known: set[str]) -> tuple[str, ...]:
    return tuple(i for i in rule["ids"] if i in known)


def notes_for(key: str, known: Iterable[str]) -> list[dict]:
    """Règles qui concernent un élément (« type:id ») : avec quels autres éléments du catalogue il se gêne ou fait
    double emploi."""
    known = set(known)
    out = []
    for r in RULES:
        ids = _present(r, known)
        if key in ids and len(ids) >= 2:
            out.append(
                {"level": r["level"], "title": r["title"], "text": r["text"], "with": [i for i in ids if i != key]}
            )
    return out


def report(installed: Iterable[str], known: Iterable[str], mcp_tools: dict[str, int] | None = None) -> dict:
    """Bilan de ce PC : règles dont au moins deux éléments sont installés, nombre de serveurs MCP et de leurs outils."""
    have, known = set(installed), set(known)
    warnings = []
    for r in RULES:
        both = [i for i in _present(r, known) if i in have]
        if len(both) >= r.get("min", 2):
            warnings.append({"level": r["level"], "title": r["title"], "text": r["text"], "items": both})
    servers = sorted(i.split(":", 1)[1] for i in have if i.startswith("mcp:"))
    tools = sum((mcp_tools or {}).get(s, 0) for s in servers)
    if len(servers) >= MCP_MANY:
        warnings.append(
            {
                "level": "attention",
                "title": f"{len(servers)} serveurs MCP",
                "text": "Tous démarrent avec Lezard IA : le démarrage est plus long et chacun garde un peu de mémoire. "
                "Désinstallez ceux dont vous ne vous servez plus.",
                "items": [f"mcp:{s}" for s in servers],
            }
        )
    if tools >= TOOLS_MANY:
        warnings.append(
            {
                "level": "attention",
                "title": f"{tools} outils MCP en tout",
                "text": "Proposés tous à la fois (/mcp sans nom de serveur), ils dépassent ce qu'une IA locale sait "
                "trier : elle choisit le mauvais outil ou n'en choisit aucun. Activez seulement les serveurs utiles à "
                "la conversation, ou nommez le serveur : /mcp fichiers …",
                "items": [f"mcp:{s}" for s in servers],
            }
        )
    order = {"conflit": 0, "attention": 1, "doublon": 2}
    warnings.sort(key=lambda w: order[w["level"]])
    return {
        "installed": len(have),
        "mcp_servers": len(servers),
        "mcp_tools": tools,
        "warnings": warnings,
        "ok": not any(w["level"] == "conflit" for w in warnings),
    }
