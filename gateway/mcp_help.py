"""Explications des serveurs MCP et de leurs outils, en français, pour la commande /mcp du chat.

Les serveurs décrivent leurs outils en anglais, pour le modèle : ces descriptions-là ne changent pas. Ici, ce qu'une
personne lit. Un outil sans texte ici garde celui du serveur, signalé comme tel.
"""

from __future__ import annotations

from . import marketplace

# serveurs déclarés à la main dans data/mcp.json, absents du catalogue de la marketplace
SERVERS = {
    "heure": {
        "name": "Heure et fuseaux horaires",
        "description": "L'IA connaît l'heure exacte, partout dans le monde, et convertit d'un fuseau horaire à l'autre.",
        "example": "quelle heure est-il à Tokyo ?",
    },
    "rea": {
        "name": "REA (analyse de programmes)",
        "description": "L'IA analyse un programme sans son code source : format, textes qu'il contient, fonctions. "
        "Lire le code d'une fonction demande Ghidra, Hopper ou IDA en plus.",
        "example": "ouvre C:\\Windows\\System32\\where.exe et dis-moi son format et son architecture",
    },
}
# par nom d'outil, tel que le serveur le donne
TOOLS = {
    # heure
    "get_current_time": "Donne l'heure actuelle dans un fuseau horaire.",
    "convert_time": "Convertit une heure d'un fuseau horaire à un autre.",
    # git
    "git_status": "Montre l'état du dépôt : fichiers modifiés, ajoutés, non suivis.",
    "git_diff_unstaged": "Montre les modifications pas encore préparées pour un commit.",
    "git_diff_staged": "Montre les modifications préparées pour le prochain commit.",
    "git_diff": "Compare l'état actuel à une autre branche ou à un autre commit.",
    "git_commit": "Enregistre les modifications préparées dans un commit.",
    "git_add": "Prépare des fichiers pour le prochain commit.",
    "git_reset": "Retire du prochain commit tout ce qui était préparé (les fichiers ne changent pas).",
    "git_log": "Affiche l'historique des commits.",
    "git_create_branch": "Crée une nouvelle branche.",
    "git_checkout": "Change de branche.",
    "git_show": "Montre le contenu d'un commit, ou d'un fichier à un moment donné.",
    "git_branch": "Liste les branches du dépôt.",
    # fichiers
    "read_file": "Lit un fichier texte (ancien nom de read_text_file).",
    "read_text_file": "Lit un fichier texte en entier, ou seulement son début ou sa fin.",
    "read_media_file": "Lit une image ou un fichier audio.",
    "read_multiple_files": "Lit plusieurs fichiers d'un coup, pour les comparer par exemple.",
    "write_file": "Crée un fichier ou remplace entièrement un fichier existant, sans prévenir s'il existe déjà.",
    "edit_file": "Modifie des lignes précises d'un fichier texte et montre ce qui a changé.",
    "create_directory": "Crée un dossier, et les dossiers parents s'il en manque.",
    "list_directory": "Liste les fichiers et les dossiers d'un dossier.",
    "list_directory_with_sizes": "Liste le contenu d'un dossier avec la taille de chaque fichier.",
    "directory_tree": "Donne toute l'arborescence d'un dossier et de ses sous-dossiers.",
    "move_file": "Déplace ou renomme un fichier ou un dossier.",
    "search_files": "Cherche des fichiers et des dossiers d'après leur nom, dans tous les sous-dossiers.",
    "get_file_info": "Donne les informations d'un fichier : taille, dates, droits.",
    "list_allowed_directories": "Liste les dossiers auxquels l'IA a le droit d'accéder.",
    # mémoire
    "create_entities": "Ajoute à la mémoire de nouveaux éléments : une personne, un projet, une préférence…",
    "create_relations": "Note un lien entre deux éléments de la mémoire, par exemple « Lezard utilise Python ».",
    "add_observations": "Ajoute des faits à un élément déjà en mémoire.",
    "delete_entities": "Efface des éléments de la mémoire, avec leurs liens.",
    "delete_observations": "Efface certains faits d'un élément de la mémoire.",
    "delete_relations": "Efface des liens entre éléments de la mémoire.",
    "read_graph": "Lit toute la mémoire.",
    "search_nodes": "Cherche dans la mémoire à partir d'un mot ou d'une question.",
    "open_nodes": "Lit des éléments précis de la mémoire, d'après leur nom.",
    # réflexion
    "sequentialthinking": "Découpe un problème en étapes, que l'IA peut revoir et corriger avant de répondre.",
    # rea
    "open_binary": "Ouvre un programme ou une archive à analyser. C'est la première étape avec REA.",
    "close_binary": "Termine l'analyse et ferme le programme ouvert.",
    "binary_overview": "Résume le programme ouvert : nombre de fonctions et de textes, sections qui le composent.",
    "list_strings": "Liste les textes trouvés dans le programme.",
    "search_strings": "Cherche un mot parmi les textes du programme.",
    "list_procedures": "Liste les fonctions trouvées dans le programme.",
    "search_procedures": "Cherche une fonction d'après son nom.",
    "procedure_pseudo_code": "Reconstitue le code d'une fonction, sous une forme lisible (demande Ghidra, Hopper ou IDA).",
    "procedure_callers": "Indique quelles fonctions en appellent une autre.",
    "inspect_artifact": "Décrit le contenu d'une archive ou d'une application empaquetée.",
    "analyze_javascript_application": "Reconstitue la structure d'une application JavaScript, sans la lancer.",
}
SERVER_TEXT = "Texte fourni par le serveur : "
MAX_FALLBACK = 200


def tool_help(raw: str, description: str = "") -> str:
    """L'explication d'un outil ; à défaut, le début de la description du serveur, sans son préfixe « [MCP nom] »."""
    if raw in TOOLS:
        return TOOLS[raw]
    text = description.split("] ", 1)[-1].strip() if description.startswith("[MCP ") else description.strip()
    return SERVER_TEXT + text[:MAX_FALLBACK] if text else "Pas d'explication fournie par le serveur."


def server_view(name: str) -> dict:
    """Nom lisible, explication et exemple d'un serveur, d'après son nom dans data/mcp.json."""
    if name in SERVERS:
        return {"id": name, **SERVERS[name]}
    entry = next((s for s in marketplace.MCP_SERVERS if s["id"] == name), None)
    if not entry:
        return {"id": name, "name": name, "description": "Serveur déclaré dans data/mcp.json.", "example": None}
    example = next((c["example"] for c in entry.get("commands", ()) if c.get("example")), "")
    return {
        "id": name,
        "name": entry["name"],
        "description": entry["description"],
        "example": example.removeprefix("/mcp ") or None,
    }
