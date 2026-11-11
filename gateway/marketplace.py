"""Marketplace (page /marketplace, clé maître) : tout ce qui s'installe dans Lezard IA, avec licences et droits d'auteur.

- Modèles : bibliothèque Ollama (ollama_library) classée selon le matériel (model_catalog) ; la licence de chaque
  modèle est le texte publié avec lui dans le registre d'Ollama (couche « license » du manifeste), lu à la demande et
  gardé 7 jours. `classify` en tire un résumé indicatif (usage commercial, obligations) : seul le texte fait foi.
- Composants de Lezard IA (voix locale, multi-agents, recherche web, contrôle du PC, Ollama) : installés par le
  lanceur de l'installation Windows (lezard.py), comme depuis le menu Démarrer.
- Serveurs MCP : liste choisie de serveurs officiels, version npm figée (publiée depuis au moins 14 jours, comme
  Ollama) ; « installer » écrit leur déclaration dans data/mcp.json. La clé d'un service (21st.dev, Apify) y est
  écrite dans « env », sur ce PC seulement, et n'est jamais renvoyée par l'API.
- Outils pour agents (HyperFrames, REA, Orca…) : ils s'utilisent avec Claude Code, Codex ou en application ; la page
  donne leur licence, leur commande d'installation et leur lien avec Lezard IA.
- Applications à brancher (Open WebUI, Continue…) : elles s'installent de leur côté ; la page donne leur licence et
  le guide de branchement du tableau de bord.
- « commands » : les commandes qu'un élément ajoute (chat de Lezard IA, Claude Code ou terminal), avec leur usage et
  un exemple, montrées dans sa fenêtre « Détails ». Relevées dans la documentation de chaque projet le 2026-11-01.
- Bundles : des éléments à installer ensemble, proposés selon ce PC (mémoire, carte graphique, Windows) et les
  logiciels qu'on y utilise (pas d'outils pour Claude Code sans Claude Code). Un modèle y est désigné par son usage
  (« model:code ») : le meilleur pour ce PC, choisi par model_catalog.recommend.
Les licences des logiciels tiers ont été relevées sur leurs dépôts (fichier LICENSE) le 2026-10-30, celles des
éléments ajoutés ensuite le jour de leur ajout (2026-11-02).
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from collections.abc import Callable

import httpx

from . import mcp
from .gpus import usable
from .model_catalog import CATALOG, USE_LABELS

REGISTRY = "https://registry.ollama.ai/v2/library"
MANIFEST_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
LICENSE_SETTING = "marketplace_licenses"
LICENSE_TTL = 7 * 24 * 3600
MAX_LICENSE_BYTES = 200_000
MAX_CACHED = 400
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TAG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
DISCLAIMER = "Résumé indicatif, pas un avis juridique : seul le texte complet de la licence fait foi."


class MarketplaceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ---------- licences : résumé à partir du texte ----------
# (motif, nom, usage commercial : « oui » | « conditions » | « non », obligations principales)
RULES = (
    (
        r"LLAMA \d[\d.]* COMMUNITY LICENSE|Llama \d[\d.]* Community License",
        "Llama Community License",
        "conditions",
        "Usage commercial permis sous 700 millions d'utilisateurs actifs par mois (au-delà : licence de Meta). Mention "
        "« Built with Llama », copie de la licence en cas de redistribution, politique d'usage acceptable de Meta.",
    ),
    (
        r"LLAMA 2 COMMUNITY LICENSE",
        "Llama 2 Community License",
        "conditions",
        "Usage commercial permis sous 700 millions d'utilisateurs actifs par mois ; politique d'usage acceptable de Meta.",
    ),
    (
        r"Gemma Terms of Use",
        "Gemma Terms of Use",
        "conditions",
        "Usage commercial permis ; politique d'usages interdits de Google, à transmettre avec toute redistribution.",
    ),
    (r"Qwen RESEARCH LICENSE", "Qwen Research License", "non", "Recherche et usage non commercial seulement."),
    (
        r"Qwen LICENSE AGREEMENT|Tongyi Qianwen LICENSE",
        "Qwen License",
        "conditions",
        "Usage commercial permis sous 100 millions d'utilisateurs actifs par mois (au-delà : licence d'Alibaba).",
    ),
    (
        r"MISTRAL AI NON-PRODUCTION LICENSE|Mistral AI Research License",
        "Mistral AI Non-Production / Research License",
        "non",
        "Essais, recherche et usage personnel ; pas de production commerciale sans licence de Mistral AI.",
    ),
    (
        r"Creative Commons Attribution-NonCommercial",
        "Creative Commons BY-NC 4.0",
        "non",
        "Usage non commercial seulement, avec attribution (souvent avec une politique d'usage acceptable).",
    ),
    (
        r"Creative Commons Attribution-ShareAlike",
        "Creative Commons BY-SA 4.0",
        "oui",
        "Attribution ; les œuvres dérivées gardent la même licence.",
    ),
    (r"Creative Commons Attribution 4\.0", "Creative Commons BY 4.0", "oui", "Attribution de l'auteur."),
    (
        r"DEEPSEEK LICENSE AGREEMENT",
        "DeepSeek License",
        "conditions",
        "Usage commercial permis ; restrictions d'usage (annexe A) à respecter et à transmettre.",
    ),
    (
        r"OpenRAIL|RAIL License|Responsible AI License",
        "OpenRAIL",
        "conditions",
        "Usage commercial permis ; restrictions d'usage à respecter et à transmettre.",
    ),
    (
        r"NVIDIA Open Model License",
        "NVIDIA Open Model License",
        "conditions",
        "Usage commercial permis ; attribution et respect des garde-fous du modèle.",
    ),
    (
        r"Falcon[- ]\S* ?(TII )?License|TII Falcon License",
        "TII Falcon License",
        "conditions",
        "Usage commercial permis sous conditions (politique d'usage acceptable de TII).",
    ),
    (
        r"Apache License\s+Version 2\.0",
        "Apache-2.0",
        "oui",
        "Libre, usage commercial compris ; garder la licence et les mentions (NOTICE) en cas de redistribution.",
    ),
    (
        r"Permission is hereby granted, free of charge",
        "MIT",
        "oui",
        "Libre, usage commercial compris ; garder la mention de copyright en cas de redistribution.",
    ),
    (
        r"Redistribution and use in source and binary forms",
        "BSD",
        "oui",
        "Libre, usage commercial compris ; garder la mention de copyright.",
    ),
    (
        r"GNU AFFERO GENERAL PUBLIC LICENSE",
        "AGPL-3.0",
        "conditions",
        "Copyleft fort, y compris pour un service en ligne : les modifications doivent être publiées sous la même licence.",
    ),
    (
        r"GNU GENERAL PUBLIC LICENSE",
        "GPL",
        "conditions",
        "Copyleft : une redistribution modifiée doit être publiée sous la même licence.",
    ),
)
_COPYRIGHT = re.compile(r"(?:Copyright|©)(?:\s*(?:\(c\)|©))*\s*([^\n]{3,120})", re.I)


def classify(text: str | None) -> dict:
    """Résumé d'une licence d'après son texte : nom, usage commercial, obligations, titulaire des droits."""
    if not text or not text.strip():
        return {
            "name": "Non précisée",
            "commercial": "inconnu",
            "summary": "Aucune licence publiée avec ce modèle : vérifiez sur la page de son éditeur avant tout usage.",
            "copyright": None,
            "disclaimer": DISCLAIMER,
        }
    head = text[:20_000]
    unknown = ("", "Licence propre", "inconnu", "Licence propre à l'éditeur : lisez le texte complet.")
    _, name, commercial, summary = next((r for r in RULES if re.search(r[0], head, re.I)), unknown)
    m = _COPYRIGHT.search(head)
    holder = m and re.sub(r"[.,]?\s*All rights( reserved)?\.?$", "", m.group(1).strip(), flags=re.I).rstrip(".")
    holder = holder if holder and "notice" not in holder.lower() else None
    return {"name": name, "commercial": commercial, "summary": summary, "copyright": holder, "disclaimer": DISCLAIMER}


async def model_license(client: httpx.AsyncClient, store, name: str, tag: str = "latest") -> dict:
    """Licence d'un modèle de la bibliothèque Ollama, lue dans le registre (sans rien télécharger d'autre)."""
    if not _NAME.match(name or "") or not _TAG.match(tag or ""):
        raise MarketplaceError(400, "Nom de modèle invalide (ex. llama3.1, taille 8b).")
    key = f"{name}:{tag}"
    cache = store.get_setting(LICENSE_SETTING) or {}
    hit = cache.get(key)
    if hit and time.time() - hit.get("fetched_at", 0) < LICENSE_TTL:
        return hit
    try:
        r = await client.get(f"{REGISTRY}/{name}/manifests/{tag}", headers={"Accept": MANIFEST_TYPE}, timeout=20)
        if r.status_code == 404:
            raise MarketplaceError(404, f"Modèle « {key} » absent du registre d'Ollama.")
        r.raise_for_status()
        layers = r.json().get("layers") or []
        texts = []
        for layer in [x for x in layers if str(x.get("mediaType", "")).endswith(".license")][:3]:
            if int(layer.get("size") or 0) > MAX_LICENSE_BYTES:
                texts.append("(texte de licence trop long pour être affiché : voir la page du modèle sur ollama.com)")
                continue
            b = await client.get(f"{REGISTRY}/{name}/blobs/{layer['digest']}", follow_redirects=True, timeout=30)
            b.raise_for_status()
            texts.append(b.content[:MAX_LICENSE_BYTES].decode("utf-8", errors="replace").strip())
    except (httpx.HTTPError, ValueError, KeyError) as e:
        if hit:  # registre injoignable : la dernière lecture sert
            return {**hit, "stale": True}
        raise MarketplaceError(502, f"Registre d'Ollama injoignable ({type(e).__name__}) : réessayez plus tard.")
    size = sum(int(x.get("size") or 0) for x in layers if str(x.get("mediaType", "")).endswith(".model"))
    text = "\n\n----------\n\n".join(texts)
    out = {
        "model": key,
        **classify(text),
        "text": text or None,
        "download_bytes": size or None,
        "source": f"https://ollama.com/library/{name}:{tag}",
        "fetched_at": time.time(),
    }
    cache[key] = out
    if len(cache) > MAX_CACHED:  # les plus anciennes lectures s'effacent
        for k in sorted(cache, key=lambda k: cache[k].get("fetched_at", 0))[: len(cache) - MAX_CACHED]:
            cache.pop(k)
    store.set_setting(LICENSE_SETTING, cache)
    return out


# ---------- étoiles GitHub : relevé daté (python -m gateway.marketplace_export le rafraîchit) ----------
STARS_FILE = Path(__file__).with_name("marketplace_stars.json")
_GITHUB = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)")


def github_repo(entry: dict) -> str | None:
    """Dépôt GitHub d'un élément : « github » s'il est donné, sinon sa première adresse github.com."""
    if entry.get("github"):
        return entry["github"]
    lic = entry.get("license")
    urls = [entry.get("url"), lic.get("url") if isinstance(lic, dict) else None]  # compétence : nom seulement
    urls += [lic.get("url") for lic in entry.get("licenses", ())]
    for url in urls:
        m = _GITHUB.match(url or "")
        if m:
            return m.group(1).removesuffix(".git")
    return None


def stars_snapshot() -> dict:
    try:
        return json.loads(STARS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"date": None, "repos": {}}


def fmt_stars(n: int) -> str:
    return f"{n / 1000:.1f}".replace(".", ",").removesuffix(",0") + " k" if n >= 1000 else str(n)


def with_stars(entry: dict, snap: dict | None = None) -> dict:
    """L'élément, avec ses étoiles GitHub relevées (nombre, texte, date du relevé)."""
    snap = snap or stars_snapshot()
    repo = github_repo(entry)
    count = snap.get("repos", {}).get(repo) if repo else None
    return {
        **entry,
        "github": repo,
        "stars": fmt_stars(count) if count is not None else None,
        "stars_count": count,
        "stars_date": snap.get("date") if count is not None else None,
    }


# ---------- composants de Lezard IA ----------
def _lic(name: str, holder: str, url: str, commercial: str = "oui", summary: str = "") -> dict:
    return {"name": name, "copyright": holder, "url": url, "commercial": commercial, "summary": summary}


WHERE = {
    "chat": "Chat de Lezard IA",
    "claude": "Claude Code",
    "terminal": "Terminal",
    "app": "Dans l'application",
    "vscode": "VS Code",
    "browser": "Navigateur",
    "python": "Code Python",
}


def _cmd(use: str, what: str, example: str | None = None, where: str = "chat") -> dict:
    """Une commande ajoutée par un élément : sa syntaxe, à quoi elle sert, un exemple et où la taper."""
    return {"use": use, "what": what, "example": example, "where": WHERE[where]}


def _mcp_cmd(example: str) -> list[dict]:
    return [
        _cmd(
            "/mcp <demande>",
            "L'IA utilise ce serveur (et vos autres serveurs MCP) pour cette demande ; chaque appel d'outil attend "
            "votre accord.",
            example,
        )
    ]


MIT_NOTE = "Libre, usage commercial compris ; garder la mention de copyright en cas de redistribution."
APACHE_NOTE = "Libre, usage commercial compris ; garder la licence et les mentions (NOTICE) en cas de redistribution."

COMPONENTS = (
    {
        "id": "voix",
        "name": "Dictée et lecture vocales",
        "description": "Dictée transcrite sur ce PC (faster-whisper) et lecture des réponses à voix haute (Kokoro). "
        "L'audio ne quitte jamais la machine.",
        "size": "~90 Mo, plus les modèles de voix au premier usage",
        "action": ["pack", "install", "voix"],
        "restart": True,
        "commands": [
            _cmd(
                "Bouton micro (à côté de « Envoyer »)",
                "Dicter un message : appuyer pour commencer, appuyer à nouveau pour terminer (sur téléphone : maintenir "
                "pour parler).",
            ),
            _cmd(
                "Bouton « Écouter » (sous chaque réponse)", "Lire la réponse à voix haute ; appuyer à nouveau l'arrête."
            ),
        ],
        "licenses": [
            _lic("MIT", "SYSTRAN (faster-whisper)", "https://github.com/SYSTRAN/faster-whisper", summary=MIT_NOTE),
            _lic("MIT", "thewh1teagle (kokoro-onnx)", "https://github.com/thewh1teagle/kokoro-onnx", summary=MIT_NOTE),
            _lic("Apache-2.0", "hexgrad (voix Kokoro-82M)", "https://github.com/hexgrad/kokoro", summary=APACHE_NOTE),
        ],
    },
    {
        "id": "multi",
        "name": "Multi-agents CrewAI",
        "description": "Ajoute CrewAI : des agents plus poussés pour les très gros modèles (70 milliards de paramètres "
        "ou plus, ou modèles en ligne de pointe). La commande /multi fonctionne déjà sans, avec l'orchestrateur "
        "intégré de Lezard IA.",
        "size": "~220 Mo",
        "action": ["pack", "install", "multi"],
        "restart": True,
        "commands": [
            _cmd(
                "/multi [auto | on | off]",
                "Multi-agents : auto selon la complexité de la demande, on pour toujours répartir entre plusieurs IA, "
                "off pour une IA seule. Sans argument : le réglage actuel.",
                "/multi auto",
            )
        ],
        "licenses": [_lic("MIT", "crewAI Inc.", "https://github.com/crewAIInc/crewAI", summary=MIT_NOTE)],
    },
    {
        "id": "web",
        "name": "Recherche sur Internet",
        "description": "Commande /web du chat : l'IA cherche sur Internet pour une demande.",
        "size": "rien à télécharger",
        "action": ["tools", "web=on"],
        "restart": True,
        "commands": [
            _cmd(
                "/web <question>",
                "L'IA cherche sur Internet avant de répondre, pour ce message seulement.",
                "/web quel temps fera-t-il à Brest demain ?",
            )
        ],
        "licenses": [],
    },
    {
        "id": "pc",
        "name": "Contrôle du PC",
        "description": "Commande /pc du chat : l'IA agit sur ce PC (clics, saisie, applications). Chaque action "
        "attend votre accord.",
        "size": "rien à télécharger",
        "action": ["tools", "pc=on"],
        "restart": True,
        "commands": [
            _cmd(
                "/pc <tâche>",
                "L'IA agit sur ce PC (souris, clavier, applications) pour cette demande ; chaque action attend votre "
                "accord.",
                "/pc ouvre le bloc-notes et écris la liste des courses",
            ),
            _cmd(
                "/retiens <préférence>",
                "L'IA mémorise une préférence pour ses prochaines actions sur ce PC.",
                "/retiens j'utilise Firefox, pas Edge",
            ),
            _cmd("/memoire", "Ce que l'IA sait de ce PC : vos préférences et le guide de vos applications, numérotés."),
            _cmd(
                "/oublie <n° | tout>", "Efface un souvenir (son numéro est donné par /memoire), ou tous.", "/oublie 2"
            ),
        ],
        "licenses": [],
    },
    {
        "id": "ollama",
        "name": "Ollama",
        "description": "Le moteur qui fait tourner les modèles sur ce PC. Installateur officiel, version figée et "
        "vérifiée par Lezard IA.",
        "size": "~1,5 Go",
        "action": ["ollama-install"],
        "restart": False,
        "commands": [
            _cmd("ollama list", "Les modèles téléchargés sur ce PC, avec leur taille.", where="terminal"),
            _cmd(
                "ollama pull <modèle>",
                "Télécharger un modèle (la page Modèles de Lezard IA le fait aussi).",
                "ollama pull gemma3:12b",
                "terminal",
            ),
            _cmd(
                "ollama ps",
                "Les modèles chargés en mémoire en ce moment (carte graphique ou processeur).",
                where="terminal",
            ),
            _cmd(
                "ollama rm <modèle>",
                "Supprimer un modèle pour libérer de la place.",
                "ollama rm gemma3:12b",
                "terminal",
            ),
        ],
        "licenses": [_lic("MIT", "Ollama", "https://github.com/ollama/ollama", summary=MIT_NOTE)],
    },
)


def component(cid: str) -> dict:
    found = next((c for c in COMPONENTS if c["id"] == cid), None)
    if not found:
        raise MarketplaceError(404, f"Composant « {cid} » inconnu.")
    return found


# ---------- serveurs MCP ----------
MCP_VERSION = "2.0.0"  # publiée le 2026-10-19 (les versions datées, 2026.8.31, sont dépréciées) ; Node.js 24 ou plus
MCP_LICENSE = _lic(
    "MIT / Apache-2.0",
    "Model Context Protocol (Anthropic et contributeurs)",
    "https://github.com/modelcontextprotocol/servers",
    summary="Libre, usage commercial compris. Projet en transition de MIT vers Apache-2.0 : chaque contribution garde "
    "l'une ou l'autre ; garder les mentions en cas de redistribution.",
)
MCP_SERVERS = (
    {
        "id": "memoire",
        "package": "@modelcontextprotocol/server-memory",
        "commands": _mcp_cmd("/mcp retiens que mon projet s'appelle Lezard et qu'il est écrit en Python"),
        "size": "moins de 1 Mo",
        "name": "Mémoire (graphe de connaissances)",
        "description": "L'IA retient des faits entre les conversations (personnes, projets, préférences), dans un "
        "fichier local.",
        "needs_folder": False,
        "version": MCP_VERSION,
        "license": MCP_LICENSE,
    },
    {
        "id": "reflexion",
        "package": "@modelcontextprotocol/server-sequential-thinking",
        "commands": _mcp_cmd("/mcp réfléchis étape par étape : comment organiser un déménagement en 3 semaines ?"),
        "size": "moins de 1 Mo",
        "name": "Réflexion pas à pas",
        "description": "L'IA découpe un problème difficile en étapes qu'elle peut revoir et corriger.",
        "needs_folder": False,
        "version": MCP_VERSION,
        "license": MCP_LICENSE,
    },
    {
        "id": "fichiers",
        "package": "@modelcontextprotocol/server-filesystem",
        "commands": _mcp_cmd("/mcp liste les PDF du dossier et résume le plus récent"),
        "size": "moins de 1 Mo",
        "name": "Fichiers d'un dossier",
        "description": "L'IA lit, cherche et écrit des fichiers dans UN dossier que vous choisissez, jamais ailleurs.",
        "needs_folder": True,
        "version": MCP_VERSION,
        "license": MCP_LICENSE,
    },
    {
        "id": "playwright",
        "package": "@playwright/mcp",
        "commands": _mcp_cmd("/mcp ouvre lezard-ia.fr et résume la page d'accueil"),
        "size": "moins de 1 Mo, plus ~150 Mo de navigateur au premier usage",
        "name": "Playwright (navigateur)",
        "description": "L'IA pilote un vrai navigateur : ouvrir des pages, cliquer, remplir des formulaires, lire et "
        "capturer le contenu. Utile pour tester un site ou automatiser une démarche web.",
        "needs_folder": False,
        "version": "0.0.83",  # publiée le 2026-09-28
        "license": _lic(
            "Apache-2.0", "Microsoft Corporation", "https://github.com/microsoft/playwright-mcp", summary=APACHE_NOTE
        ),
    },
    {
        "id": "magic21",
        "package": "@21st-dev/magic",
        "commands": _mcp_cmd("/mcp crée un bouton d'abonnement en React avec Tailwind"),
        "size": "moins de 1 Mo",
        "name": "21st.dev Magic (composants d'interface)",
        "description": "L'IA cherche et génère des composants React / Tailwind soignés parmi plus de 10 000, depuis le "
        "service 21st.dev.",
        "needs_folder": False,
        "version": "0.2.3",  # publiée le 2026-09-09
        "secret": {"env": "API_KEY_21ST", "label": "Clé API 21st.dev", "url": "https://21st.dev/mcp"},
        "service": "Service en ligne de 21st.dev : vos demandes lui sont envoyées ; clé gratuite, offres payantes au-delà.",
        "license": _lic("ISC", "21st.dev", "https://github.com/21st-dev/magic-mcp", summary=MIT_NOTE),
    },
    {
        "id": "apify",
        "package": "@apify/actors-mcp-server",
        "commands": _mcp_cmd("/mcp trouve les 10 premières boulangeries de Brest sur Google Maps"),
        "size": "6 Mo",
        "name": "Apify (extraction de données web)",
        "description": "L'IA lance des « Actors » d'Apify Store pour extraire des données de sites, réseaux sociaux, "
        "moteurs de recherche ou cartes, et lit les résultats structurés.",
        "needs_folder": False,
        "version": "0.18.4",  # publiée le 2026-10-16
        "secret": {
            "env": "APIFY_TOKEN",
            "label": "Jeton API Apify",
            "url": "https://console.apify.com/settings/integrations",
        },
        "service": "Service en ligne d'Apify : chaque exécution consomme des crédits (offre gratuite mensuelle, puis "
        "payant). Respectez les conditions d'utilisation des sites dont vous extrayez les données.",
        "license": _lic("MIT", "Apify", "https://github.com/apify/apify-mcp-server", summary=MIT_NOTE),
    },
    {
        "id": "agentmemory",
        "package": "@agentmemory/mcp",
        "commands": _mcp_cmd("/mcp qu'avions-nous décidé la dernière fois pour la page d'accueil ?"),
        "size": "~9 Mo",
        "name": "agentmemory (mémoire des agents)",
        "description": "Mémoire persistante : l'IA enregistre ce qu'elle apprend pendant une session et le retrouve "
        "aux suivantes (recherche dans ses souvenirs), sur ce PC.",
        "needs_folder": False,
        "version": "0.9.30",  # publiée le 2026-10-06
        "license": _lic("Apache-2.0", "Rohit Ghumare", "https://github.com/rohitg00/agentmemory", summary=APACHE_NOTE),
    },
    {
        "id": "context7",
        "package": "@upstash/context7-mcp",
        "commands": _mcp_cmd("/mcp avec la documentation de FastAPI, comment déclarer une route asynchrone ?"),
        "size": "moins de 1 Mo",
        "name": "Context7 (documentation à jour)",
        "description": "L'IA lit la documentation actuelle d'une bibliothèque de code (versions, exemples) avant de "
        "répondre, au lieu de s'appuyer sur ce qu'elle a appris il y a des mois.",
        "needs_folder": False,
        "version": "4.4.4",  # publiée le 2026-10-19
        "service": "Service en ligne d'Upstash : le nom des bibliothèques cherchées lui est envoyé ; gratuit, avec "
        "des limites d'usage.",
        "license": _lic("MIT", "Upstash, Inc.", "https://github.com/upstash/context7", summary=MIT_NOTE),
    },
    {
        "id": "chromedevtools",
        "package": "chrome-devtools-mcp",
        "args": ["--no-usage-statistics"],  # statistiques d'usage envoyées à Google par défaut : coupées
        # 32 outils : gemma4 ne trouvait plus « new_page » ; ceux d'une inspection courante (essai réel 2026-11-08)
        "only_tools": [
            *("new_page", "navigate_page", "list_pages", "select_page", "close_page", "wait_for"),
            *("take_snapshot", "take_screenshot", "evaluate_script", "click", "fill", "press_key"),
            *("list_console_messages", "list_network_requests", "performance_start_trace"),
            *("performance_stop_trace", "performance_analyze_insight"),
        ],
        "commands": _mcp_cmd("/mcp ouvre http://localhost:8000 et dis-moi pourquoi la page met du temps à charger"),
        "size": "~14 Mo, plus Google Chrome sur ce PC",
        "name": "Chrome DevTools (inspecter une page)",
        "description": "L'IA ouvre une page dans Chrome et l'inspecte comme un développeur : erreurs de la console, "
        "requêtes réseau, performances, captures. Les statistiques d'usage envoyées à Google sont coupées.",
        "needs_folder": False,
        "version": "1.11.0",  # publiée le 2026-10-15
        "license": _lic(
            "Apache-2.0", "Google LLC", "https://github.com/ChromeDevTools/chrome-devtools-mcp", summary=APACHE_NOTE
        ),
    },
    {
        "id": "desktopcommander",
        "package": "@wonderwhy-er/desktop-commander",
        "commands": _mcp_cmd("/mcp lance les tests du projet dans C:\\Projets\\site et résume les erreurs"),
        "size": "~4 Mo",
        "name": "Desktop Commander (terminal et fichiers)",
        "description": "L'IA lance des commandes dans le terminal, lit et modifie des fichiers et cherche dans vos "
        "dossiers. Puissant : chaque appel attend votre accord ; sa télémétrie facultative se coupe dans ses réglages "
        "(telemetryEnabled).",
        "needs_folder": False,
        "version": "0.3.0",  # publiée le 2026-10-13
        "license": _lic(
            "MIT",
            "Eduard Ruzga et contributeurs",
            "https://github.com/wonderwhy-er/DesktopCommanderMCP",
            summary=MIT_NOTE,
        ),
    },
    {
        "id": "brave",
        "package": "@brave/brave-search-mcp-server",
        "commands": _mcp_cmd("/mcp cherche les dernières nouvelles sur Ollama"),
        "size": "moins de 1 Mo",
        "name": "Brave Search (recherche web)",
        "description": "L'IA cherche sur Internet avec le moteur indépendant de Brave : pages, actualités, images, "
        "vidéos.",
        "needs_folder": False,
        "version": "2.1.4",  # publiée le 2026-09-17
        "secret": {
            "env": "BRAVE_API_KEY",
            "label": "Clé API Brave Search",
            "url": "https://api-dashboard.search.brave.com",
        },
        "service": "Service en ligne de Brave : vos recherches lui sont envoyées ; crédit gratuit chaque mois, payant "
        "au-delà.",
        "license": _lic(
            "MIT",
            "Anthropic, PBC (serveur repris par Brave Software)",
            "https://github.com/brave/brave-search-mcp-server",
            summary=MIT_NOTE,
        ),
    },
    {
        "id": "notion",
        "package": "@notionhq/notion-mcp-server",
        "commands": _mcp_cmd("/mcp ajoute « appeler le plombier » à ma page Notion « À faire »"),
        "size": "~3 Mo",
        "name": "Notion",
        "description": "L'IA lit, cherche et écrit dans vos pages et bases Notion, limitées à celles que vous "
        "partagez avec votre intégration.",
        "needs_folder": False,
        "version": "2.5.2",  # publiée le 2026-09-20
        "secret": {
            "env": "NOTION_TOKEN",
            "label": "Jeton d'intégration Notion",
            "url": "https://www.notion.so/profile/integrations",
        },
        "service": "Service en ligne de Notion : l'IA y lit et y écrit avec votre jeton ; partagez seulement les "
        "pages utiles avec l'intégration.",
        "license": _lic(
            "MIT", "Notion Labs, Inc.", "https://github.com/makenotion/notion-mcp-server", summary=MIT_NOTE
        ),
    },
    {
        "id": "firecrawl",
        "package": "firecrawl-mcp",
        "commands": _mcp_cmd("/mcp récupère le contenu de la page tarifs de exemple.com en texte propre"),
        "size": "moins de 1 Mo",
        "name": "Firecrawl (pages web en texte)",
        "description": "L'IA transforme une page ou un site entier en texte propre (sans menus ni publicités), "
        "cherche sur le web et extrait des données structurées.",
        "needs_folder": False,
        "version": "3.33.3",  # publiée le 2026-10-19
        "secret": {
            "env": "FIRECRAWL_API_KEY",
            "label": "Clé API Firecrawl",
            "url": "https://www.firecrawl.dev/app/api-keys",
        },
        "service": "Service en ligne de Firecrawl : chaque page lue consomme des crédits (crédits gratuits, puis "
        "payant). Respectez les conditions d'utilisation des sites lus.",
        "license": _lic(
            "MIT", "vrknetha (Firecrawl)", "https://github.com/firecrawl/firecrawl-mcp-server", summary=MIT_NOTE
        ),
    },
    {
        "id": "tavily",
        "package": "tavily-mcp",
        "commands": _mcp_cmd("/mcp compare les prix des cartes graphiques RTX 5070 cette semaine"),
        "size": "moins de 1 Mo",
        "name": "Tavily (recherche pour IA)",
        "description": "Recherche web pensée pour les IA : résultats résumés et sourcés, extraction du contenu des "
        "pages trouvées.",
        "needs_folder": False,
        "version": "0.2.22",  # publiée le 2026-08-05
        "secret": {"env": "TAVILY_API_KEY", "label": "Clé API Tavily", "url": "https://app.tavily.com"},
        "service": "Service en ligne de Tavily : vos recherches lui sont envoyées ; crédits gratuits chaque mois, "
        "payant au-delà.",
        "license": _lic("MIT", "Tavily", "https://github.com/tavily-ai/tavily-mcp", summary=MIT_NOTE),
    },
    {
        "id": "exa",
        "package": "exa-mcp-server",
        "commands": _mcp_cmd("/mcp trouve des articles récents sur la quantification des modèles de langage"),
        "size": "~2 Mo",
        "name": "Exa (recherche par le sens)",
        "description": "Moteur de recherche qui comprend le sens de la demande : articles, code, entreprises, "
        "personnes, avec le contenu des pages.",
        "needs_folder": False,
        "version": "3.4.2",  # publiée le 2026-10-06
        "secret": {"env": "EXA_API_KEY", "label": "Clé API Exa", "url": "https://dashboard.exa.ai/api-keys"},
        "service": "Service en ligne d'Exa : vos recherches lui sont envoyées ; crédits gratuits, puis payant.",
        "license": _lic("MIT", "Exa Labs, Inc.", "https://github.com/exa-labs/exa-mcp-server", summary=MIT_NOTE),
    },
    {
        "id": "searxng",
        "package": "mcp-searxng",
        "commands": _mcp_cmd("/mcp cherche les horaires de la médiathèque de Quimper"),
        "size": "moins de 1 Mo",
        "name": "SearXNG (votre moteur de recherche)",
        "description": "L'IA cherche sur Internet avec VOTRE instance SearXNG (métamoteur libre, hébergé par vous ou "
        "par une instance de confiance) et lit les pages trouvées : aucun compte, aucune clé.",
        "needs_folder": False,
        "version": "2.7.0",  # publiée le 2026-10-20 ; Node.js 22 ou plus
        "secret": {"env": "SEARXNG_URL", "label": "Adresse de votre SearXNG", "url": "https://docs.searxng.org"},
        "license": _lic("MIT", "Ihor Sokoliuk", "https://github.com/ihor-sokoliuk/mcp-searxng", summary=MIT_NOTE),
    },
    {
        "id": "n8n",
        "package": "n8n-mcp",
        "env": {
            "MCP_MODE": "stdio",
            "LOG_LEVEL": "error",
            "DISABLE_CONSOLE_OUTPUT": "true",
            "N8N_MCP_TELEMETRY_DISABLED": "true",
        },  # fmt: skip
        "commands": _mcp_cmd("/mcp quel nœud n8n utiliser pour envoyer un message Slack quand un formulaire arrive ?"),
        "size": "~80 Mo",
        "name": "n8n (automatisations)",
        "description": "L'IA connaît les 500+ nœuds de n8n (paramètres, exemples, modèles de workflows) et vous aide à "
        "construire des automatisations. Sa télémétrie, active par défaut, est coupée.",
        "needs_folder": False,
        "version": "2.94.3",  # publiée le 2026-10-19
        "license": _lic("MIT", "Romuald Czlonkowski", "https://github.com/czlonkowski/n8n-mcp", summary=MIT_NOTE),
    },
    {
        "id": "figma",
        "package": "figma-developer-mcp",
        "args": ["--stdio"],
        "commands": _mcp_cmd("/mcp transforme cette maquette Figma en page HTML : https://www.figma.com/design/…"),
        "size": "moins de 1 Mo",
        "name": "Figma (maquettes vers code)",
        "description": "L'IA lit une maquette Figma (mise en page, couleurs, textes) pour la reproduire fidèlement en "
        "code.",
        "needs_folder": False,
        "version": "0.13.2",  # publiée le 2026-06-18
        "secret": {
            "env": "FIGMA_API_KEY",
            "label": "Jeton d'accès Figma",
            "url": "https://www.figma.com/developers/api#access-tokens",
        },
        "service": "Service en ligne de Figma : l'IA lit vos fichiers avec votre jeton (lecture seule suffit).",
        "license": _lic("MIT", "Graham Lipsman", "https://github.com/GLips/Figma-Context-MCP", summary=MIT_NOTE),
    },
    {
        "id": "git",
        "package": "@cyanheads/git-mcp-server",
        "env": {"MCP_TRANSPORT_TYPE": "stdio"},
        "folder_env": "GIT_BASE_DIR",
        "commands": _mcp_cmd("/mcp résume les 10 derniers commits du projet et ce qui reste à commiter"),
        "size": "~7 Mo, plus Git sur ce PC",
        "name": "Git (dépôts de code)",
        "description": "L'IA consulte et manipule vos dépôts Git (état, historique, différences, branches, commits), "
        "limitée au dossier que vous choisissez.",
        "needs_folder": True,
        "version": "2.15.3",  # publiée le 2026-08-24
        "license": _lic(
            "Apache-2.0", "Casey Hand (cyanheads)", "https://github.com/cyanheads/git-mcp-server", summary=APACHE_NOTE
        ),
    },
    {
        "id": "airtable",
        "package": "airtable-mcp-server",
        "commands": _mcp_cmd("/mcp liste les clients de ma base Airtable inscrits ce mois-ci"),
        "size": "moins de 1 Mo",
        "name": "Airtable",
        "description": "L'IA lit et met à jour vos bases Airtable (tables, enregistrements, champs), limitée à celles "
        "que votre jeton autorise.",
        "needs_folder": False,
        "version": "1.14.0",  # publiée le 2026-07-27
        "secret": {
            "env": "AIRTABLE_API_KEY",
            "label": "Jeton d'accès Airtable",
            "url": "https://airtable.com/create/tokens/new",
        },
        "service": "Service en ligne d'Airtable : l'IA y lit et y écrit avec votre jeton ; donnez-lui seulement les "
        "droits utiles.",
        "license": _lic(
            "MIT", "Adam Jones (domdomegg)", "https://github.com/domdomegg/airtable-mcp-server", summary=MIT_NOTE
        ),
    },
    {
        "id": "perplexity",
        "package": "@perplexity-ai/mcp-server",
        "commands": _mcp_cmd(
            "/mcp fais une recherche approfondie sur les aides à l'achat d'un vélo électrique en 2026"
        ),
        "size": "moins de 1 Mo",
        "name": "Perplexity (recherche avec réponses sourcées)",
        "description": "L'IA pose ses questions à Perplexity : recherche web, réponses sourcées, recherche "
        "approfondie et raisonnement.",
        "needs_folder": False,
        "version": "1.3.0",  # publiée le 2026-09-25
        "secret": {
            "env": "PERPLEXITY_API_KEY",
            "label": "Clé API Perplexity",
            "url": "https://www.perplexity.ai/account/api",
        },
        "service": "Service en ligne de Perplexity, payant à l'usage : vos questions lui sont envoyées.",
        "license": _lic(
            "MIT", "Perplexity AI", "https://github.com/perplexityai/modelcontextprotocol", summary=MIT_NOTE
        ),
    },
)
# dans le chat : chaque serveur a sa commande, et /mcp seul explique sans rien envoyer à l'IA
for _s in MCP_SERVERS:
    _example = next(c["example"] for c in _s["commands"] if c.get("example")).removeprefix("/mcp ")
    _s["commands"] = [
        _cmd(
            f"/mcp {_s['id']} <demande>",
            "L'IA n'utilise que ce serveur pour cette demande ; chaque appel d'outil affiche le serveur et attend votre "
            "accord.",
            f"/mcp {_s['id']} {_example}",
        ),
        _cmd(f"/mcp {_s['id']}", "Explique ce serveur et chacun de ses outils, sans rien envoyer à l'IA."),
        _cmd("/mcp", "Liste vos serveurs, leur état et la commande de chacun."),
    ]

# ---------- recherche sur Internet : plusieurs éléments proches, comparés dans leur fenêtre « Détails » ----------
# (élément « kind:id », coût, ce qui part de ce PC, point fort) ; offres relevées sur le site de chaque service le
# 2026-11-02, susceptibles de changer.
WEB_SEARCH = (
    (
        "component:web",
        "Gratuit, sans clé",
        "Vos recherches, vers DuckDuckGo (ou votre SearXNG, réglage SEARXNG_URL)",
        "Déjà dans Lezard IA : le plus simple, et le plus discret avec votre propre SearXNG",
    ),
    (
        "mcp:brave",
        "Crédit gratuit chaque mois, puis payant",
        "Vos recherches, vers Brave (index indépendant de Google et Bing)",
        "Actualités, images et vidéos en plus des pages",
    ),
    (
        "mcp:tavily",
        "Crédits gratuits chaque mois, puis payant",
        "Vos recherches, vers Tavily",
        "Résultats déjà résumés et sourcés pour l'IA : moins de pages à lire",
    ),
    (
        "mcp:exa",
        "Crédits gratuits, puis payant",
        "Vos recherches, vers Exa",
        "Recherche par le sens : articles, code, entreprises et personnes proches de la demande",
    ),
    (
        "mcp:searxng",
        "Gratuit, sans clé",
        "Vos recherches, vers votre instance SearXNG (puis les moteurs qu'elle interroge)",
        "Le plus discret : vous choisissez l'instance et les moteurs",
    ),
    (
        "mcp:perplexity",
        "Payant à l'usage",
        "Vos questions, vers Perplexity",
        "Réponses rédigées et sourcées, recherche approfondie",
    ),
    (
        "mcp:firecrawl",
        "Crédits gratuits, puis payant",
        "Les adresses lues et vos recherches, vers Firecrawl",
        "Lire un site entier en texte propre et en extraire des données",
    ),
)


def web_search_comparison() -> list[dict]:
    names = {f"component:{c['id']}": c["name"] for c in COMPONENTS} | {f"mcp:{s['id']}": s["name"] for s in MCP_SERVERS}
    return [{"key": k, "name": names[k], "cost": c, "sends": s, "best": b} for k, c, s, b in WEB_SEARCH]


def mcp_server(sid: str) -> dict:
    found = next((s for s in MCP_SERVERS if s["id"] == sid), None)
    if not found:
        raise MarketplaceError(404, f"Serveur MCP « {sid} » inconnu.")
    return found


def _read_mcp_config(path: Path) -> dict:
    if not path.is_file():
        return {"servers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mcp.parse_config(data)
    except (OSError, ValueError) as e:
        raise MarketplaceError(409, f"{path} illisible ({e}) : corrigez-le avant d'ajouter un serveur.")
    return data


def installed_mcp() -> set[str]:
    try:
        return set(_read_mcp_config(mcp.config_path())["servers"])
    except MarketplaceError:
        return set()


def check_mcp(sid: str, folder: str | None = None, secret: str | None = None) -> None:
    """Vérifie dossier et clé avant d'installer Node.js : une erreur de saisie ne coûte pas une installation."""
    add_mcp(sid, "npx", folder, secret, write=False)


def add_mcp(sid: str, npx: str, folder: str | None = None, secret: str | None = None, write: bool = True) -> dict:
    """Déclare un serveur de la liste dans data/mcp.json (version figée) ; renvoie sa déclaration, sans la clé."""
    s = mcp_server(sid)
    args = ["-y", f"{s['package']}@{s['version']}", *s.get("args", ())]
    if s["needs_folder"]:
        target = Path(folder or "").expanduser()
        if folder and any(c in folder for c in '&|^%<>"!'):
            # npx.cmd passe par cmd.exe : un dossier nommé « x&calc » lancerait une commande
            raise MarketplaceError(
                400, 'Ce dossier contient un caractère refusé (& | ^ % < > " !) : choisissez-en un autre.'
            )
        if not folder or not target.is_absolute() or not target.is_dir():
            raise MarketplaceError(
                400, "Indiquez le chemin complet d'un dossier existant de ce PC (ex. C:\\Users\\…\\Documents)."
            )
        if not s.get("folder_env"):
            args.append(str(target))
    cfg = {"command": npx, "args": args}
    env = dict(s.get("env", {}))  # réglages fixes (télémétrie coupée, transport stdio…)
    if s["needs_folder"] and s.get("folder_env"):  # dossier donné par une variable plutôt qu'en argument
        env[s["folder_env"]] = str(target)
    if s.get("secret"):
        secret = (secret or "").strip()
        if not 8 <= len(secret) <= 300 or any(c.isspace() for c in secret):
            raise MarketplaceError(
                400, f"{s['secret']['label']} requise : créez-la sur {s['secret']['url']}, puis collez-la."
            )
        env[s["secret"]["env"]] = secret
    if env:
        cfg["env"] = env
    if s.get("only_tools"):  # serveur aux nombreux outils : ceux d'un usage courant (une IA locale s'y perd)
        cfg["only_tools"] = list(s["only_tools"])
    if not write:
        return {**cfg, "env": sorted(cfg.get("env", {}))}
    path = mcp.config_path()
    data = _read_mcp_config(path)
    data["servers"][sid] = cfg
    mcp.parse_config(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**cfg, "env": sorted(cfg.get("env", {}))}  # noms des variables seulement : la clé ne repart jamais


def remove_mcp(sid: str) -> bool:
    mcp_server(sid)
    path = mcp.config_path()
    data = _read_mcp_config(path)
    if data["servers"].pop(sid, None) is None:
        return False
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


# ---------- applications à brancher (elles s'installent de leur côté) ----------
APPS = (
    {
        "id": "openwebui",
        "name": "Open WebUI",
        "description": "Interface de chat complète, lancée par Lezard IA et déjà branchée sur ses modèles.",
        "commands": [
            _cmd(
                "http://localhost:3000",
                "Ouvrir Open WebUI après /openwebui dans le chat (première fois : créez le compte administrateur).",
                where="browser",
            ),
        ],
        "guide": "openwebui",
        "url": "https://github.com/open-webui/open-webui",
        "license": _lic(
            "Open WebUI License (BSD-3 + marque)",
            "Open WebUI Inc. (Timothy Jaeryang Baek)",
            "https://github.com/open-webui/open-webui/blob/main/LICENSE",
            "conditions",
            "Usage commercial permis ; interdiction de retirer ou remplacer la marque « Open WebUI » au-delà de 50 "
            "utilisateurs par période de 30 jours, sauf autorisation ou licence entreprise.",
        ),
    },
    {
        "id": "continue",
        "name": "Continue",
        "description": "Assistant de code dans VS Code et JetBrains.",
        "commands": [
            _cmd("Ctrl+L", "Envoyer le code sélectionné dans le chat de Continue (Cmd+L sur Mac).", where="vscode"),
            _cmd("Ctrl+J", "La même chose dans JetBrains (Cmd+J sur Mac).", where="app"),
        ],
        "guide": "continue",
        "url": "https://github.com/continuedev/continue",
        "license": _lic(
            "Apache-2.0", "Continue Dev, Inc.", "https://github.com/continuedev/continue", summary=APACHE_NOTE
        ),
    },
    {
        "id": "cursor",
        "name": "Cursor",
        "description": "Éditeur de code avec IA ; il appelle la gateway depuis ses serveurs (adresse Internet requise).",
        "commands": [
            _cmd("Ctrl+I", "Ouvrir ou fermer le panneau de l'agent (Cmd+I sur Mac).", where="app"),
            _cmd("Ctrl+L", "Démarrer une discussion avec le code sélectionné (Cmd+L sur Mac).", where="app"),
        ],
        "guide": "cursor",
        "url": "https://cursor.com",
        "github": "cursor/cursor",  # dépôt public de l'éditeur (suivi des problèmes)
        "license": _lic(
            "Logiciel propriétaire",
            "Anysphere, Inc.",
            "https://cursor.com/terms-of-service",
            "conditions",
            "Gratuit ou payant selon l'offre ; conditions d'utilisation de l'éditeur.",
        ),
    },
    {
        "id": "openclaw",
        "name": "OpenClaw",
        "description": "Assistant qui agit depuis votre ordinateur et vos messageries, branché sur Lezard IA.",
        "commands": [
            _cmd(
                "openclaw onboard",
                "Pour l'utiliser aussi hors de Lezard IA : réglages guidés (modèle, service local, dossier de travail, "
                "messageries).",
                where="terminal",
            ),
            _cmd("openclaw chat", "Discuter avec l'assistant dans le terminal.", where="terminal"),
            _cmd("openclaw dashboard", "Ouvrir son tableau de bord dans le navigateur.", where="terminal"),
            _cmd("openclaw status", "État du service, des messageries et des modèles.", where="terminal"),
            _cmd("openclaw doctor", "Vérifie l'installation et propose des corrections.", where="terminal"),
        ],
        "guide": "openclaw",
        "url": "https://github.com/openclaw/openclaw",
        "license": _lic("MIT", "OpenClaw contributors", "https://github.com/openclaw/openclaw", summary=MIT_NOTE),
        "warning": "N'installez pas de compétences tierces de son catalogue (ClawHub) sans les lire : des compétences "
        "malveillantes y ont été signalées.",
    },
    {
        "id": "comfyui",
        "name": "ComfyUI",
        "description": "Génération d'images sur ce PC (commande /image du chat) : installez-le, puis COMFYUI_URL dans .env.",
        "commands": [
            _cmd(
                "/image <description>",
                "Génère une image avec votre ComfyUI (son adresse dans COMFYUI_URL, fichier .env).",
                "/image un lézard vert sur une pierre au soleil",
            )
        ],
        "guide": None,
        "url": "https://github.com/Comfy-Org/ComfyUI",
        "license": _lic(
            "GPL-3.0",
            "Comfy Org et contributeurs",
            "https://github.com/Comfy-Org/ComfyUI/blob/master/LICENSE",
            "conditions",
            "Copyleft : une version modifiée et redistribuée doit être publiée sous GPL-3.0. Les modèles "
            "d'images ont leurs propres licences.",
        ),
    },
    {
        "id": "python",
        "name": "SDK OpenAI pour Python",
        "description": "Appeler Lezard IA depuis vos scripts Python.",
        "commands": [
            _cmd(
                'OpenAI(base_url="<adresse de la gateway>/v1", api_key="<votre clé>")',
                "Le client branché sur Lezard IA ; ensuite client.chat.completions.create(model=…, messages=[…]).",
                'client = OpenAI(base_url="http://localhost:8000/v1", api_key="sk-app-…")',
                "python",
            )
        ],
        "guide": "python",
        "url": "https://github.com/openai/openai-python",
        "license": _lic("Apache-2.0", "OpenAI", "https://github.com/openai/openai-python", summary=APACHE_NOTE),
    },
    {
        "id": "langchain",
        "name": "LangChain",
        "description": "Bibliothèque pour enchaîner modèles, documents et outils.",
        "commands": [
            _cmd(
                'ChatOpenAI(base_url="<adresse de la gateway>/v1", api_key="<votre clé>", model="<modèle>")',
                'Un modèle de Lezard IA dans LangChain (paquet langchain-openai) ; ensuite llm.invoke("…").',
                'llm = ChatOpenAI(base_url="http://localhost:8000/v1", api_key="sk-app-…", model="gemma3:12b")',
                "python",
            )
        ],
        "guide": "langchain",
        "url": "https://github.com/langchain-ai/langchain",
        "license": _lic("MIT", "LangChain, Inc.", "https://github.com/langchain-ai/langchain", summary=MIT_NOTE),
    },
    {
        "id": "openhuman",
        "name": "OpenHuman",
        "description": "Assistant de bureau open source : il se connecte à vos services (mails, agenda, documents…), "
        "garde une mémoire de ce qu'il apprend sur vous et peut utiliser vos modèles locaux d'Ollama.",
        "commands": [
            _cmd(
                "Menu Démarrer → OpenHuman",
                "Ouvrir l'application, puis connecter les services de votre choix.",
                where="app",
            )
        ],
        "guide": None,
        "url": "https://github.com/tinyhumansai/openhuman",
        "license": _lic(
            "GPL-3.0",
            "TinyHumans AI et contributeurs",
            "https://github.com/tinyhumansai/openhuman/blob/main/LICENSE",
            "conditions",
            "Copyleft : usage libre, y compris commercial ; une version modifiée et redistribuée doit être publiée "
            "sous GPL-3.0 avec son code source.",
        ),
        "warning": "Encore jeune (version bêta). Les services que vous lui connectez lui donnent accès à vos données : "
        "ne branchez que ceux dont vous avez besoin.",
    },
    {
        "id": "anythingllm",
        "name": "AnythingLLM",
        "description": "Application de bureau pour discuter avec vos documents (PDF, Word, sites) et lancer des "
        "agents ; branchez-la sur Lezard IA comme fournisseur « Generic OpenAI ».",
        "commands": [
            _cmd(
                "Menu Démarrer → AnythingLLM",
                "Ouvrir l'application ; créez un espace de travail et déposez-y vos documents pour en discuter.",
                where="app",
            )
        ],
        "guide": "anythingllm",
        "url": "https://github.com/Mintplex-Labs/anything-llm",
        "license": _lic("MIT", "Mintplex Labs Inc.", "https://github.com/Mintplex-Labs/anything-llm", summary=MIT_NOTE),
    },
    {
        "id": "librechat",
        "name": "LibreChat",
        "description": "Interface de chat complète et multi-utilisateurs, alternative à Open WebUI : agents, "
        "fichiers, conversations ; installée sans Docker et déjà branchée sur Lezard IA.",
        "commands": [
            _cmd("/librechat", "Le démarrer depuis le chat de Lezard IA (sa base MongoDB, puis lui).", where="chat"),
            _cmd("http://localhost:3080", "Ouvrir LibreChat (première fois : créez votre compte).", where="browser"),
        ],
        "guide": "librechat",
        "url": "https://github.com/LibreChat-AI/LibreChat",
        "license": _lic("MIT", "LibreChat", "https://github.com/LibreChat-AI/LibreChat", summary=MIT_NOTE),
    },
    {
        "id": "jan",
        "name": "Jan",
        "description": "Application de chat de bureau, simple et hors ligne ; Lezard IA s'y ajoute comme fournisseur "
        "compatible OpenAI.",
        "commands": [
            _cmd(
                "Menu Démarrer → Jan",
                "Ouvrir l'application, puis choisir un modèle de Lezard IA en haut de la discussion.",
                where="app",
            )
        ],
        "guide": "jan",
        "url": "https://github.com/janhq/jan",
        "license": _lic(
            "Apache-2.0",
            "Menlo Research",
            "https://github.com/janhq/jan",
            summary=APACHE_NOTE + " Mention de Menlo Research demandée dans la documentation.",
        ),
    },
    {
        "id": "cline",
        "name": "Cline",
        "description": "Agent de code dans VS Code : il lit le projet, modifie les fichiers et lance des commandes, "
        "avec votre accord à chaque étape. Fournisseur « OpenAI Compatible » : Lezard IA.",
        "commands": [
            _cmd(
                "Icône Cline (barre latérale)",
                "Ouvrir Cline et lui décrire la tâche ; ou Ctrl+Maj+P puis « Cline » pour ses commandes.",
                where="vscode",
            )
        ],
        "guide": "cline",
        "url": "https://github.com/cline/cline",
        "license": _lic("Apache-2.0", "Cline Bot Inc.", "https://github.com/cline/cline", summary=APACHE_NOTE),
    },
    {
        "id": "kilocode",
        "name": "Kilo Code",
        "description": "Agent de code dans VS Code (modes architecte, code, débogage) ; fournisseur « OpenAI "
        "Compatible » : Lezard IA.",
        "commands": [
            _cmd(
                "Icône Kilo Code (barre latérale)",
                "Ouvrir Kilo Code, choisir le mode (architecte, code, débogage) et décrire la tâche ; ou Ctrl+Maj+P puis "
                "« Kilo Code ».",
                where="vscode",
            )
        ],
        "guide": "kilocode",
        "url": "https://github.com/Kilo-Org/kilocode",
        "license": _lic("MIT", "Kilo Code", "https://github.com/Kilo-Org/kilocode", summary=MIT_NOTE),
    },
    {
        "id": "aider",
        "name": "Aider",
        "description": "Agent de code dans le terminal, qui travaille avec Git (chaque modification est un commit) ; "
        "branché sur Lezard IA par OPENAI_API_BASE.",
        "commands": [
            _cmd(
                "aider --model openai/<modèle> <fichiers>",
                "Démarrer Aider sur des fichiers, avec un modèle de Lezard IA (OPENAI_API_BASE et OPENAI_API_KEY "
                "réglés sur votre gateway).",
                "aider --model openai/qwen3-coder app.py",
                "terminal",
            )
        ],
        "guide": "aider",
        "url": "https://github.com/Aider-AI/aider",
        "license": _lic("Apache-2.0", "Paul Gauthier", "https://github.com/Aider-AI/aider", summary=APACHE_NOTE),
    },
    {
        "id": "opencode",
        "name": "OpenCode",
        "description": "Agent de code dans le terminal, très suivi ; Lezard IA s'y déclare comme fournisseur "
        "compatible OpenAI (fichier opencode.json).",
        "commands": [_cmd("opencode", "Ouvrir l'agent dans le dossier du projet.", where="terminal")],
        "guide": "opencode",
        "url": "https://github.com/anomalyco/opencode",
        "license": _lic("MIT", "opencode", "https://github.com/anomalyco/opencode", summary=MIT_NOTE),
    },
    {
        "id": "goose",
        "name": "Goose",
        "description": "Agent de bureau qui exécute des tâches (code, fichiers, recherches) et s'étend par serveurs "
        "MCP ; fournisseur « OpenAI Compatible » : Lezard IA.",
        "commands": [
            _cmd(
                "%LOCALAPPDATA%\\Programs\\Goose\\dist-windows\\Goose.exe",
                "Lancer Goose (Win+R puis cette adresse), puis lui décrire la tâche.",
                where="app",
            )
        ],
        "guide": "goose",
        "url": "https://github.com/aaif-goose/goose",
        "license": _lic("Apache-2.0", "Block, Inc.", "https://github.com/aaif-goose/goose", summary=APACHE_NOTE),
    },
    {
        "id": "pageassist",
        "name": "Page Assist",
        "description": "Extension de navigateur : discuter de la page ouverte ou de vos documents avec vos modèles "
        "locaux d'Ollama, dans un panneau latéral.",
        "commands": [
            _cmd("Ctrl+Maj+Y", "Ouvrir le panneau latéral, pour discuter de la page ouverte.", where="browser"),
            _cmd("Ctrl+Maj+L", "Ouvrir Page Assist en plein onglet.", where="browser"),
        ],
        "guide": None,
        "url": "https://github.com/n4ze3m/page-assist",
        "license": _lic("MIT", "Muhammed Nazeem (n4ze3m)", "https://github.com/n4ze3m/page-assist", summary=MIT_NOTE),
    },
    {
        "id": "chatbox",
        "name": "Chatbox (édition communautaire)",
        "description": "Application de chat de bureau ; Lezard IA s'y ajoute comme fournisseur compatible OpenAI.",
        "commands": [
            _cmd("Menu Démarrer → Chatbox", "Ouvrir l'application et choisir un modèle de Lezard IA.", where="app")
        ],
        "guide": "chatbox",
        "url": "https://github.com/chatboxai/chatbox",
        "license": _lic(
            "GPL-3.0",
            "Benn Huang et contributeurs",
            "https://github.com/chatboxai/chatbox/blob/main/LICENSE",
            "conditions",
            "Copyleft : une version modifiée et redistribuée doit être publiée sous GPL-3.0 avec son code source.",
        ),
        "warning": "Seule l'édition communautaire (0.10.4, de 2024) est libre et c'est elle qui est installée : "
        "l'application Chatbox actuelle est propriétaire.",
    },
    {
        "id": "meetily",
        "name": "Meetily",
        "description": "Comptes rendus de réunion : transcription sur ce PC (Whisper) et résumé par un modèle local "
        "d'Ollama. L'audio ne quitte pas la machine.",
        "commands": [
            _cmd(
                "Menu Démarrer → Meetily",
                "Ouvrir l'application, lancer l'enregistrement de la réunion, puis demander le résumé.",
                where="app",
            )
        ],
        "guide": None,
        "url": "https://github.com/Zackriya-Solutions/meetily",
        "license": _lic("MIT", "Zackriya Solutions", "https://github.com/Zackriya-Solutions/meetily", summary=MIT_NOTE),
        "warning": "Prévenez les participants avant d'enregistrer une réunion : leur accord est requis.",
    },
    {
        "id": "qwencode",
        "name": "Qwen Code",
        "description": "Agent de code dans le terminal (de l'équipe Qwen), qui accepte tout fournisseur compatible "
        "OpenAI : Lezard IA par OPENAI_BASE_URL.",
        "commands": [_cmd("qwen", "Ouvrir l'agent dans le dossier du projet.", where="terminal")],
        "guide": "qwencode",
        "url": "https://github.com/QwenLM/qwen-code",
        "license": _lic(
            "Apache-2.0",
            "Google LLC et Qwen Team (Alibaba)",
            "https://github.com/QwenLM/qwen-code",
            summary=APACHE_NOTE,
        ),
    },
    {
        "id": "zed",
        "name": "Zed",
        "description": "Éditeur de code rapide avec un agent intégré ; Lezard IA s'y ajoute comme fournisseur "
        "compatible OpenAI.",
        "commands": [
            _cmd(
                "Icône ✨ (barre d'état)",
                "Ouvrir le panneau de l'agent ; ou Ctrl+Maj+P puis « agent: new thread ». Ctrl+N y démarre une "
                "nouvelle discussion.",
                where="app",
            )
        ],
        "guide": "zed",
        "url": "https://github.com/zed-industries/zed",
        "license": _lic(
            "GPL-3.0 (certaines parties Apache-2.0)",
            "Zed Industries, Inc.",
            "https://github.com/zed-industries/zed",
            "conditions",
            "Copyleft : usage libre, y compris commercial ; une version modifiée et redistribuée doit être publiée "
            "sous GPL-3.0 avec son code source.",
        ),
    },
    {
        "id": "cherrystudio",
        "name": "Cherry Studio",
        "description": "Application de bureau de chat : plusieurs fournisseurs côte à côte, assistants, base de "
        "connaissances et serveurs MCP ; Lezard IA s'y ajoute comme fournisseur compatible OpenAI.",
        "commands": [
            _cmd(
                "Menu Démarrer → Cherry Studio",
                "Ouvrir l'application et choisir un modèle de Lezard IA dans la discussion.",
                where="app",
            )
        ],
        "guide": "cherrystudio",
        "url": "https://github.com/CherryHQ/cherry-studio",
        "license": _lic(
            "AGPL-3.0",
            "CherryHQ et contributeurs",
            "https://github.com/CherryHQ/cherry-studio/blob/main/LICENSE",
            "conditions",
            "Copyleft fort, y compris pour un service en ligne : usage commercial permis, une version modifiée doit "
            "être publiée sous AGPL-3.0.",
        ),
    },
)


# ---------- outils pour agents (Claude Code, Codex…) : licence, installation et lien avec Lezard IA ----------
AGENT_TOOLS = (
    {
        "id": "hyperframes",
        "name": "HyperFrames",
        "description": "Écrire une vidéo en HTML et la rendre en MP4 : l'agent compose titres, animations et montages, "
        "le rendu se fait sur votre machine.",
        "install": "claude plugin marketplace add heygen-com/hyperframes\nclaude plugin install hyperframes@hyperframes",
        "commands": [
            _cmd(
                "/hyperframes:hyperframes <demande>",
                "La compétence principale : créer, modifier, animer ou rendre une vidéo.",
                "/hyperframes:hyperframes fais une intro de 10 secondes avec un titre en fondu et une musique",
                "claude",
            ),
            _cmd(
                "npx hyperframes preview",
                "Aperçu de la vidéo dans le navigateur, mis à jour à chaque modification.",
                where="terminal",
            ),
            _cmd("npx hyperframes render", "Rendu de la vidéo en MP4 (Node.js 22 et FFmpeg).", where="terminal"),
        ],
        "needs": "Node.js 22 ou plus ; existe aussi en application de bureau (hyperframes.studio).",
        "lezard": "Compétence pour Claude Code et les agents compatibles, pas pour le chat de Lezard IA.",
        "url": "https://github.com/heygen-com/hyperframes",
        "license": _lic("Apache-2.0", "HeyGen", "https://github.com/heygen-com/hyperframes", summary=APACHE_NOTE),
    },
    {
        "id": "hyperframes-cli",
        "name": "HyperFrames pour tous les agents",
        "description": "La même création de vidéos, en commande : n'importe quel agent (OpenCode, Goose, Aider, Cline…), "
        "branché sur un modèle de Lezard IA, écrit la vidéo en HTML et la rend en MP4 sur ce PC.",
        "commands": [
            _cmd("hyperframes init ma-video", "Crée le dossier d'une nouvelle vidéo.", where="terminal"),
            _cmd(
                "hyperframes preview",
                "Aperçu dans le navigateur, mis à jour à chaque modification de l'agent.",
                where="terminal",
            ),
            _cmd("hyperframes render -o video.mp4", "Rendu en MP4 (FFmpeg et Chrome).", where="terminal"),
            _cmd("hyperframes lint", "Vérifie la vidéo avant le rendu ; l'agent peut lire --json.", where="terminal"),
            _cmd("hyperframes doctor", "Vérifie que Chrome, FFmpeg et Node.js sont prêts.", where="terminal"),
        ],
        "needs": "Node.js 22 ou plus et FFmpeg (installés d'abord s'ils manquent) ; Chrome pour le rendu.",
        "lezard": "Fonctionne avec un modèle local : demandez par exemple à OpenCode, ouvert dans le dossier de la "
        "vidéo, « fais une intro de 10 secondes avec un titre en fondu ». La commande hyperframes publish envoie la "
        "vidéo en ligne chez HeyGen : elle n'est jamais nécessaire.",
        "url": "https://github.com/heygen-com/hyperframes",
        "license": _lic("Apache-2.0", "HeyGen", "https://github.com/heygen-com/hyperframes", summary=APACHE_NOTE),
    },
    {
        "id": "watch",
        "name": "Watch (regarder une vidéo)",
        "description": "Commande /watch : l'agent télécharge une vidéo, en extrait des images et la transcription, "
        "puis la résume, la vérifie ou l'analyse.",
        "install": "claude plugin marketplace add bradautomates/claude-video\nclaude plugin install watch@claude-video",
        "commands": [
            _cmd(
                "/watch <adresse ou fichier> [--start 2:15 --end 2:45]",
                "L'agent extrait les images et la transcription d'une vidéo (ou d'un passage), puis la résume ou "
                "répond à vos questions. Aussi /watch:watch.",
                "/watch https://www.youtube.com/watch?v=… résume les points clés",
                "claude",
            )
        ],
        "needs": "ffmpeg et yt-dlp pour le traitement local ; une clé Gemini en option.",
        "lezard": "Compétence pour Claude Code et les agents compatibles. Respectez les droits d'auteur des vidéos "
        "analysées.",
        "url": "https://github.com/bradautomates/claude-video",
        "license": _lic("MIT", "Bradley Bonanno", "https://github.com/bradautomates/claude-video", summary=MIT_NOTE),
    },
    {
        "id": "rea",
        "name": "REA (Reverse Engineer Anything)",
        "description": "L'agent analyse une application sans son code source, de son comportement jusqu'aux binaires.",
        "install": "npx rea-agents@latest setup",
        "commands": [
            _cmd(
                "Demandez-le en langage naturel",
                "Claude Code utilise REA de lui-même (son serveur MCP) quand vous demandez d'analyser une application.",
                "Comprends comment fonctionne la recherche de l'application Notes et montre-moi les preuves",
                "claude",
            ),
            _cmd(
                "npx -y rea-agents@latest analyze-javascript-application <dossier> --json",
                "Analyse directe d'une application JavaScript ou Electron décompressée (ou de son fichier ASAR).",
                where="terminal",
            ),
        ],
        "needs": "Node.js et npm. Ghidra (décompilateur de la NSA, Apache 2.0) et Java 21 (Eclipse Temurin) sont "
        "installés avec lui.",
        "lezard": "Dans le chat de Lezard IA : /rea, avec l'IA choisie dans le chat.",
        "url": "https://github.com/morluto/rea",
        "license": _lic("MIT", "morluto", "https://github.com/morluto/rea", summary=MIT_NOTE),
        "warning": "N'analysez que des logiciels que vous avez le droit d'étudier : leur licence (CLUF) peut interdire "
        "la rétro-ingénierie, et en Europe la loi ne la permet que dans des cas précis (interopérabilité, étude du "
        "fonctionnement).",
    },
    {
        "id": "gws",
        "name": "Google Workspace CLI",
        "description": "Un outil en ligne de commande pour Drive, Gmail, Agenda, Sheets, Docs… utilisable par un agent.",
        "install": "npm install -g @googleworkspace/cli",
        "commands": [
            _cmd(
                "gws auth login",
                "Autoriser votre compte Google, une fois.",
                "gws auth login -s drive,gmail",
                "terminal",
            ),
            _cmd(
                "gws <service> <ressource> <méthode>",
                "Appeler une API Google (Drive, Gmail, Agenda, Sheets, Docs…) ; --params pour les paramètres.",
                "gws drive files list --params '{\"pageSize\": 5}'",
                "terminal",
            ),
            _cmd(
                "gws gmail +send",
                "Envoyer un mail.",
                'gws gmail +send --to alice@example.com --subject "Bonjour" --body "Salut !"',
                "terminal",
            ),
            _cmd("gws calendar +agenda", "Votre agenda.", "gws calendar +agenda --today", "terminal"),
        ],
        "needs": "Node.js 18 ou plus, et un compte Google autorisé (connexion OAuth).",
        "lezard": "Pour Claude Code et les agents de code ; vos données passent par les API de Google.",
        "url": "https://github.com/googleworkspace/cli",
        "license": _lic("Apache-2.0", "Google LLC", "https://github.com/googleworkspace/cli", summary=APACHE_NOTE),
    },
    {
        "id": "findskills",
        "name": "Find Skills",
        "description": "Trouver et installer des compétences d'agents depuis l'annuaire ouvert des skills.",
        "install": "npx skills find\nnpx skills add vercel-labs/skills --skill find-skills",
        "commands": [
            _cmd(
                "npx skills find [mot]",
                "Chercher une compétence dans l'annuaire ouvert (skills.sh). Dans Claude Code, demandez simplement "
                "« trouve une compétence pour … ».",
                "npx skills find pdf",
                "terminal",
            ),
            _cmd(
                "npx skills add <source> --skill <nom>",
                "Installer une compétence trouvée ; -g pour tous vos projets.",
                "npx skills add vercel-labs/agent-skills -g",
                "terminal",
            ),
            _cmd("npx skills list", "Les compétences installées.", where="terminal"),
        ],
        "needs": "Node.js 22.20 ou plus.",
        "lezard": "Pour Claude Code et les agents compatibles. Lisez une compétence avant de l'installer : elle donne "
        "des consignes à votre agent.",
        "url": "https://github.com/vercel-labs/skills",
        "license": _lic("MIT", "Vercel, Inc.", "https://github.com/vercel-labs/skills", summary=MIT_NOTE),
    },
    {
        "id": "orca",
        "name": "Orca",
        "description": "Environnement pour faire travailler plusieurs agents de code en parallèle (Claude Code, Codex…), "
        "chacun dans sa branche, sur ordinateur et téléphone.",
        "install": "https://onorca.dev/download",
        "commands": [
            _cmd(
                "Menu Démarrer → Orca",
                "Ouvrir Orca, ajouter un projet, puis lancer un agent (en discussion ou dans son terminal) ; chaque agent "
                "travaille dans sa propre copie du projet.",
                where="app",
            )
        ],
        "needs": "Windows, macOS ou Linux ; vos propres abonnements aux agents.",
        "lezard": "Application à part. Les agents qu'elle lance peuvent utiliser Lezard IA comme fournisseur s'ils "
        "acceptent une adresse compatible OpenAI.",
        "url": "https://github.com/stablyai/orca",
        "license": _lic("MIT", "Lovecast Inc.", "https://github.com/stablyai/orca", summary=MIT_NOTE),
    },
    {
        "id": "codegraph",
        "name": "CodeGraph",
        "description": "Carte de votre code (fonctions, appels, dépendances) tenue à jour à chaque modification : "
        "l'agent trouve ce qu'il cherche sans relire tous les fichiers, avec bien moins de tokens.",
        "install": "npm install --global @colbymchenry/codegraph@1.6.2\ncodegraph install --yes",
        "commands": [
            _cmd(
                "codegraph init",
                "Une fois par projet, dans son dossier : construit la carte du code.",
                where="terminal",
            ),
            _cmd("codegraph sync", "Met la carte à jour après des modifications.", where="terminal"),
            _cmd(
                "codegraph query <nom>",
                "Chercher une fonction, une classe ou une variable.",
                "codegraph query login",
                "terminal",
            ),
            _cmd(
                "codegraph impact <nom>",
                "Ce qui serait touché si vous modifiiez ce symbole.",
                "codegraph impact handleLogin",
                "terminal",
            ),
        ],
        "needs": "Node.js ; un agent de code (Claude Code, Cursor, Codex CLI, Gemini CLI…).",
        "lezard": "Pour les agents de code : branché tout seul sur ceux trouvés sur ce PC. Le graphe reste sur ce PC.",
        "url": "https://github.com/colbymchenry/codegraph",
        "license": _lic("MIT", "Colby McHenry", "https://github.com/colbymchenry/codegraph", summary=MIT_NOTE),
    },
    {
        "id": "agentmemory",
        "name": "agentmemory",
        "description": "Mémoire persistante pour les agents de code : ce que l'agent apprend pendant une session est "
        "retrouvé aux suivantes, au lieu d'un fichier de notes limité.",
        "install": "claude mcp add --scope user agentmemory -- cmd /c npx -y @agentmemory/mcp@0.9.30",
        "commands": [
            _cmd(
                "Demandez-le en langage naturel",
                "Claude Code utilise ses outils de mémoire (memory_save, memory_recall…) : demandez-lui de retenir ou "
                "de retrouver quelque chose.",
                "Qu'avions-nous décidé la dernière fois pour la page d'accueil ?",
                "claude",
            ),
            _cmd("/mcp", "Vérifier que agentmemory est bien connecté.", where="claude"),
        ],
        "needs": "Node.js 20 ou plus et Claude Code. Le serveur complet (visionneuse, API) demande WSL2 sous Windows.",
        "lezard": "Branché sur Claude Code en serveur MCP. Existe aussi pour le chat de Lezard IA (onglet Serveurs MCP).",
        "url": "https://github.com/rohitg00/agentmemory",
        "license": _lic("Apache-2.0", "Rohit Ghumare", "https://github.com/rohitg00/agentmemory", summary=APACHE_NOTE),
    },
    {
        "id": "ccrouter",
        "name": "Claude Code Router",
        "description": "Faire tourner Claude Code sur d'autres modèles que ceux d'Anthropic, dont ceux de Lezard IA : "
        "une adresse locale reçoit les demandes de Claude Code et les envoie au fournisseur choisi, selon vos règles.",
        "install": "npm install --global --ignore-scripts @musistudio/claude-code-router@3.1.3, puis réparation de ses modules natifs par Lezard IA",
        "guide": "ccrouter",
        "commands": [
            _cmd(
                "ccr ui",
                "Ouvrir les réglages (http://127.0.0.1:3458) : ajoutez Lezard IA dans Providers (adresse de votre "
                "gateway, protocole OpenAI Chat, votre clé).",
                where="terminal",
            ),
            _cmd("ccr code", "Lancer Claude Code à travers le routeur, sur les modèles choisis.", where="terminal"),
        ],
        "needs": "Node.js et Claude Code.",
        "lezard": "Lezard IA devient un fournisseur de Claude Code : modèles locaux ou réseau, sans abonnement Anthropic "
        "pour les tâches que vous lui confiez.",
        "url": "https://github.com/musistudio/claude-code-router",
        "license": _lic("MIT", "musistudio", "https://github.com/musistudio/claude-code-router", summary=MIT_NOTE),
    },
    {
        "id": "superpowers",
        "name": "Superpowers",
        "description": "Compétences de méthode pour Claude Code : réfléchir avant de coder, écrire un plan, tester "
        "d'abord, déboguer avec méthode, relire son travail. Elles s'activent seules : demandez « faisons une liste de "
        "tâches en React » et Claude Code commence par préciser le besoin avec vous.",
        "install": "claude plugin marketplace add obra/superpowers-marketplace\n"
        "claude plugin install superpowers@superpowers-marketplace",
        "commands": [
            _cmd(
                "Rien à taper : décrivez ce que vous voulez construire",
                "Les compétences s'activent seules : Claude Code précise d'abord le besoin avec vous, écrit un plan, "
                "puis code en testant.",
                "Faisons une liste de tâches en React",
                "claude",
            ),
        ],
        "needs": "Claude Code.",
        "lezard": "Pour Claude Code. Les compétences « Cadrer », « Plan », « TDD » et « Débogage » du chat de Lezard "
        "IA en reprennent l'esprit.",
        "url": "https://github.com/obra/superpowers",
        "license": _lic("MIT", "Jesse Vincent", "https://github.com/obra/superpowers", summary=MIT_NOTE),
    },
    {
        "id": "speckit",
        "name": "Spec Kit",
        "description": "Développement guidé par une spécification (GitHub) : l'agent écrit d'abord ce qu'il faut "
        "faire, puis le plan et les tâches, avant le code.",
        "install": "uv tool install specify-cli==1.2.2\nspecify init mon-projet --integration claude",
        "commands": [
            _cmd(
                "specify init <dossier> --integration claude",
                "Préparer un projet (nouveau, ou « . » pour le dossier courant) pour Claude Code.",
                "specify init . --integration claude",
                "terminal",
            ),
            _cmd(
                "/speckit-specify <besoin>",
                "Écrire la spécification : ce qu'il faut faire et pourquoi, sans parler technique.",
                "/speckit-specify une application de liste de courses partagée en famille",
                "claude",
            ),
            _cmd("/speckit-plan", "Le plan technique, à partir de la spécification.", where="claude"),
            _cmd("/speckit-tasks", "Découper le plan en tâches.", where="claude"),
            _cmd("/speckit-implement", "Réaliser les tâches.", where="claude"),
        ],
        "needs": "uv (installé avec lui) ; Claude Code ou un autre agent de code.",
        "lezard": "Pour Claude Code et les agents de code, projet par projet.",
        "url": "https://github.com/github/spec-kit",
        "license": _lic("MIT", "GitHub, Inc.", "https://github.com/github/spec-kit", summary=MIT_NOTE),
    },
    {
        "id": "wshobson",
        "name": "Agents spécialisés (wshobson/agents)",
        "description": "Une bibliothèque de 94 extensions pour Claude Code : sous-agents experts (Python, "
        "JavaScript, sécurité, bases de données…), compétences et commandes. Quatre généralistes sont installées.",
        "install": "claude plugin marketplace add wshobson/agents\n"
        "claude plugin install debugging-toolkit@claude-code-workflows\n"
        "claude plugin install comprehensive-review@claude-code-workflows\n"
        "claude plugin install python-development@claude-code-workflows\n"
        "claude plugin install javascript-typescript@claude-code-workflows",
        "commands": [
            _cmd(
                "/plugin install <extension>@claude-code-workflows",
                "Ajouter une autre extension de la bibliothèque (liste : /plugin).",
                "/plugin install security-scanning@claude-code-workflows",
                "claude",
            ),
        ],
        "needs": "Claude Code.",
        "lezard": "Pour Claude Code. N'installez que les extensions utiles : chacune ajoute des consignes à votre agent.",
        "url": "https://github.com/wshobson/agents",
        "license": _lic("MIT", "Seth Hobson", "https://github.com/wshobson/agents", summary=MIT_NOTE),
    },
    {
        "id": "ccusage",
        "name": "ccusage",
        "description": "Ce que Claude Code consomme : tokens et coût estimé par jour, par mois ou par session, lus "
        "dans ses journaux sur ce PC.",
        "install": "npm install --global ccusage@20.0.37",
        "commands": [
            _cmd("ccusage daily", "Consommation jour par jour.", where="terminal"),
            _cmd("ccusage monthly", "Consommation mois par mois.", where="terminal"),
            _cmd("ccusage session", "Consommation par conversation.", where="terminal"),
            _cmd("ccusage blocks --live", "Le bloc de 5 heures en cours, mis à jour en direct.", where="terminal"),
        ],
        "needs": "Node.js ; Claude Code déjà utilisé sur ce PC.",
        "lezard": "Pour suivre Claude Code ; rien n'est envoyé, tout est lu sur ce PC.",
        "url": "https://github.com/ccusage/ccusage",
        "license": _lic("MIT", "ryoppippi", "https://github.com/ccusage/ccusage", summary=MIT_NOTE),
    },
    {
        "id": "orchestre",
        "name": "Chef d'orchestre",
        "description": "Claude Code dépense moins de tokens : le modèle fort (Fable, Opus) découpe le travail, écrit "
        "la consigne de chaque étape et choisit son modèle ; les étapes simples partent vers les modèles locaux de "
        "ce PC, gratuits. Une étape que son modèle ne sait pas faire revient au modèle fort, qui l'aide ou la "
        "reprend.",
        "install": "claude mcp add --scope user orchestre -- python mcp_orchestre.py . (serveur MCP fourni avec "
        "Lezard IA), puis la méthode « orchestre » écrite dans les compétences de Claude Code",
        "commands": [
            _cmd(
                "/orchestre <demande>",
                "Claude annonce son plan (étape, modèle, pourquoi), confie les étapes simples aux modèles locaux, "
                "vérifie, reprend ce qui bloque, et termine par une vérification de l'ensemble et un bilan.",
                "/orchestre documente chaque module du dossier src dans docs/modules.md",
                "claude",
            ),
            _cmd(
                "Demandez-le en langage naturel",
                "« Orchestre ça », « délègue aux petits modèles », « économise les tokens » déclenchent la même "
                "méthode.",
                "Résume ces dix fichiers de journal en déléguant aux petits modèles.",
                "claude",
            ),
            _cmd(
                "/mcp",
                "Vérifier que orchestre est bien connecté (outils modeles, confier_etape et bilan).",
                where="claude",
            ),
        ],
        "needs": "Claude Code, et Ollama avec au moins un modèle (bundle « Pour bien démarrer »).",
        "lezard": "Écrit par Lezard IA. Les étapes confiées tournent dans l'Ollama de ce PC : rien ne sort, rien ne "
        "coûte. Le modèle déjà chargé est pris d'abord, pour ne pas en charger deux sur la carte graphique. Les "
        "fichiers sont lus et écrits dans le projet ouvert seulement. Avec les variables LEZARD_IA_URL et "
        "LEZARD_IA_KEY, les étapes passent par Lezard IA (tous ses modèles, « auto » compris).",
        "url": "https://github.com/Lezardu29/lezard-ia",
        "license": _lic(
            "Lezard IA", "Lezard IA", "https://github.com/Lezardu29/lezard-ia", summary="Fonction de Lezard IA."
        ),
    },
    {
        "id": "interrupteur",
        "name": "Interrupteur Lezard IA",
        "description": "Une commande dans Claude Code pour choisir comment la session travaille : /lezard on met "
        "Lezard IA entre Claude et vos modèles locaux (résumés de fichiers, étapes confiées, brouillons contrôlés et "
        "retouchés, vérification finale), /lezard off revient à Claude seul. Éteint, il ne pèse presque rien.",
        "install": "claude mcp add --scope user lezard -- python mcp_orchestre.py --serveur lezard . (serveur MCP "
        "fourni avec Lezard IA), puis les méthodes « lezard » et « orchestre » dans les compétences de Claude Code",
        "commands": [
            _cmd("/lezard on", "Active les outils de Lezard IA pour cette session.", where="claude"),
            _cmd("/lezard off", "Revient à Claude seul, comme d'habitude.", where="claude"),
            _cmd("/lezard", "Dit si Lezard IA est actif ou éteint.", where="claude"),
            _cmd(
                "/lezard eco",
                "Les étapes confiées passent par Lezard IA, qui prend le modèle le moins cher capable (local "
                "d'abord, cloud s'il le faut) ; /lezard rapide prend le plus rapide, /lezard local revient à "
                "Ollama seul.",
                where="claude",
            ),
            _cmd(
                "/lezard toujours",
                "Actif dans toutes les nouvelles sessions ; /lezard jamais pour revenir à éteint par défaut.",
                where="claude",
            ),
        ],
        "needs": "Claude Code, et Ollama avec au moins un modèle.",
        "lezard": "Écrit par Lezard IA. Réunit le chef d'orchestre, la mémoire des résumés et l'atelier de "
        "brouillons derrière un seul interrupteur : inutile d'installer ces fiches en plus. Le modèle reste "
        "Claude, sur votre compte ; seul ce qu'il confie part vers l'Ollama de ce PC.",
        "url": "https://github.com/Lezardu29/lezard-ia",
        "license": _lic(
            "Lezard IA", "Lezard IA", "https://github.com/Lezardu29/lezard-ia", summary="Fonction de Lezard IA."
        ),
    },
    {
        "id": "resumes",
        "name": "Mémoire des résumés",
        "description": "Claude Code ne relit plus vos gros fichiers : un modèle local en écrit le résumé (rôle, "
        "éléments principaux avec leur ligne), gardé en mémoire tant que le fichier ne change pas. Les sessions "
        "suivantes le reprennent tout de suite, sans dépenser un token.",
        "install": "claude mcp add --scope user resumes -- python mcp_orchestre.py --serveur resumes . (serveur MCP "
        "fourni avec Lezard IA)",
        "commands": [
            _cmd(
                "Demandez-le en langage naturel",
                "Claude appelle resumer_fichier ou resumer_dossier (40 fichiers en un appel) au lieu de lire.",
                "Résume le dossier src sans le lire, puis dis-moi où est gérée la connexion.",
                "claude",
            ),
            _cmd("/mcp", "Vérifier que resumes est bien connecté.", where="claude"),
        ],
        "needs": "Claude Code, et Ollama avec au moins un modèle.",
        "lezard": "Écrit par Lezard IA. Les résumés sont faits par l'Ollama de ce PC et rangés dans votre dossier "
        "utilisateur (.lezard-ia/resumes), par empreinte du fichier : un fichier modifié est résumé de nouveau. "
        "Seuls les fichiers du projet ouvert sont lus.",
        "url": "https://github.com/Lezardu29/lezard-ia",
        "license": _lic(
            "Lezard IA", "Lezard IA", "https://github.com/Lezardu29/lezard-ia", summary="Fonction de Lezard IA."
        ),
    },
    {
        "id": "brouillons",
        "name": "Atelier de brouillons",
        "description": "Deux outils pour que Claude Code ne réécrive pas ce qu'un modèle moins fort a produit : un "
        "contrôle sans IA (chaque nom cité existe-t-il dans le code, à la bonne ligne ? les numéros de ligne faux "
        "sont corrigés tout seuls) et une retouche confiée à un modèle local, d'après quelques lignes de "
        "corrections.",
        "install": "claude mcp add --scope user brouillons -- python mcp_orchestre.py --serveur brouillons . "
        "(serveur MCP fourni avec Lezard IA)",
        "commands": [
            _cmd(
                "Demandez-le en langage naturel",
                "Claude appelle controler (écarts seulement) puis retoucher (le fichier est corrigé sur place).",
                "Contrôle docs/architecture.md contre les fichiers de src et fais corriger ce qui est faux.",
                "claude",
            ),
            _cmd("/mcp", "Vérifier que brouillons est bien connecté.", where="claude"),
        ],
        "needs": "Claude Code ; Ollama avec au moins un modèle pour la retouche (le contrôle marche sans).",
        "lezard": "Écrit par Lezard IA. Le contrôle ne juge pas le sens des phrases, seulement l'existence des noms "
        "et des lignes cités. Seuls les fichiers du projet ouvert sont lus et modifiés.",
        "url": "https://github.com/Lezardu29/lezard-ia",
        "license": _lic(
            "Lezard IA", "Lezard IA", "https://github.com/Lezardu29/lezard-ia", summary="Fonction de Lezard IA."
        ),
    },
    {
        "id": "claude-etapes",
        "name": "Assistants Claude moins chers",
        "description": "Deux assistants pour Claude Code : « etape-moyenne » (Claude Sonnet) et « etape-simple » "
        "(Claude Haiku). Le modèle fort leur confie ce qui est trop difficile pour un modèle local mais ne demande "
        "pas le meilleur modèle, et garde le plan et la vérification.",
        "install": "Deux fichiers écrits dans les assistants de Claude Code (.claude/agents de votre dossier "
        "utilisateur) : etape-moyenne.md et etape-simple.md",
        "commands": [
            _cmd(
                "Demandez-le en langage naturel",
                "Claude confie l'étape à l'assistant voulu, qui rend un compte rendu court ou dit qu'il bloque.",
                "Écris les tests de ce module avec l'assistant etape-moyenne.",
                "claude",
            ),
            _cmd("/agents", "Voir les assistants installés et leur modèle.", where="claude"),
        ],
        "needs": "Claude Code avec un compte ou une clé qui donne accès à Sonnet et Haiku.",
        "lezard": "Écrit par Lezard IA. Ces assistants tournent chez Anthropic, sur des modèles moins chers : ils "
        "consomment votre compte Claude, moins que le modèle fort.",
        "url": "https://github.com/Lezardu29/lezard-ia",
        "license": _lic(
            "Lezard IA", "Lezard IA", "https://github.com/Lezardu29/lezard-ia", summary="Fonction de Lezard IA."
        ),
    },
    {
        "id": "claude-allege",
        "name": "Claude Code allégé",
        "description": "Coupe les connecteurs de votre compte claude.ai dans Claude Code : leurs outils (parfois "
        "plusieurs centaines) alourdissent chaque session, même quand ils ne servent pas. Indispensable pour "
        "faire tourner Claude Code sur un modèle local.",
        "install": "Variable de votre compte Windows : ENABLE_CLAUDEAI_MCP_SERVERS=false",
        "commands": [
            _cmd(
                "Rien à taper",
                "Après l'installation, fermez puis rouvrez VS Code et vos terminaux : Claude Code démarre sans les "
                "connecteurs de claude.ai. Désinstallez pour les retrouver.",
                where="claude",
            ),
            _cmd("/mcp", "Vérifier que les connecteurs « claude.ai » n'apparaissent plus.", where="claude"),
        ],
        "needs": "Windows et Claude Code.",
        "lezard": "Écrit par Lezard IA. Vos serveurs MCP installés sur ce PC ne sont pas touchés ; seuls les "
        "connecteurs de claude.ai (Google Drive, ElevenLabs…) sont coupés, dans Claude Code seulement.",
        "url": "https://github.com/Lezardu29/lezard-ia",
        "license": _lic(
            "Lezard IA", "Lezard IA", "https://github.com/Lezardu29/lezard-ia", summary="Fonction de Lezard IA."
        ),
    },
    {
        "id": "repomix",
        "name": "Repomix",
        "description": "Empaqueter un projet de code en un seul fichier (avec sa structure) à donner à une IA, en "
        "écartant les fichiers ignorés et en signalant les secrets.",
        "install": "npm install --global repomix@1.18.1",
        "commands": [
            _cmd("repomix", "Dans le dossier du projet : écrit repomix-output.xml.", where="terminal"),
            _cmd(
                "repomix --style markdown --compress",
                "Sortie en Markdown, code réduit à sa structure (bien moins de tokens).",
                where="terminal",
            ),
            _cmd(
                "repomix --remote <dépôt>",
                "Empaqueter un dépôt GitHub sans le cloner.",
                "repomix --remote yamadashy/repomix",
                "terminal",
            ),
        ],
        "needs": "Node.js.",
        "lezard": "Le fichier produit se joint à une conversation de Lezard IA comme n'importe quel document.",
        "url": "https://github.com/yamadashy/repomix",
        "license": _lic("MIT", "Kazuki Yamada", "https://github.com/yamadashy/repomix", summary=MIT_NOTE),
    },
    {
        "id": "serena",
        "name": "Serena",
        "description": "L'agent comprend le code par symboles (fonctions, classes, références) comme un éditeur, "
        "au lieu de lire des fichiers entiers : recherches et modifications précises, moins de tokens.",
        "install": "uv tool install -p 3.13 serena-agent==2.1.0\n"
        "claude mcp add --scope user serena -- serena start-mcp-server --context claude-code --project-from-cwd",
        "commands": [
            _cmd(
                "Activate the project <dossier>",
                "Dans Claude Code, la première fois pour un projet : Serena le découvre (onboarding), puis s'en sert "
                "pour chercher et modifier le code.",
                "Activate the project C:\\Projets\\site",
                "claude",
            ),
            _cmd(
                "serena project index",
                "Dans le dossier d'un gros projet, une fois : prépare l'index pour des recherches plus rapides.",
                where="terminal",
            ),
        ],
        "needs": "uv (installé avec lui) et Claude Code ; certains langages demandent leur serveur de langage.",
        "lezard": "Branché sur Claude Code en serveur MCP, pour tous vos projets.",
        "url": "https://github.com/oraios/serena",
        "license": _lic(
            "GPL-3.0 (partie SolidLSP : MIT)",
            "Jain & Panchenko IT-Berater Partnerschaft",
            "https://github.com/oraios/serena/blob/main/LICENSE",
            "conditions",
            "Copyleft : usage libre, y compris commercial ; une version modifiée et redistribuée doit être publiée "
            "sous GPL-3.0 avec son code source.",
        ),
    },
    {
        "id": "ccstatusline",
        "name": "ccstatusline",
        "description": "Une ligne d'état sous la saisie de Claude Code : modèle, branche Git, tokens et coût de la "
        "session, limite de 5 heures… réglable élément par élément.",
        "install": "npm install --global ccstatusline@2.2.32\nccstatusline",
        "commands": [
            _cmd(
                "ccstatusline", "Choisir et ranger les éléments de la ligne d'état, puis l'installer.", where="terminal"
            )
        ],
        "needs": "Node.js et Claude Code.",
        "lezard": "Pour Claude Code ; tout est lu sur ce PC.",
        "url": "https://github.com/sirmalloc/ccstatusline",
        "license": _lic(
            "MIT", "Matthew Breedlove (sirmalloc)", "https://github.com/sirmalloc/ccstatusline", summary=MIT_NOTE
        ),
    },
    {
        "id": "bmad",
        "name": "BMAD Method",
        "description": "Une équipe d'agents spécialisés (analyste, chef de produit, architecte, développeur, testeur) "
        "qui mène un projet de l'idée au code, étape par étape.",
        "install": "npm install --global bmad-method@6.12.4\nbmad-method install",
        "commands": [
            _cmd(
                "bmad-method install",
                "Dans le dossier d'un projet : installe la méthode pour vos agents.",
                where="terminal",
            ),
            _cmd("bmad status", "Demandez-le à l'agent : versions et prochaine étape.", where="claude"),
        ],
        "needs": "Node.js 20.12 ou plus, et uv (installé avec lui).",
        "lezard": "Pour Claude Code et les agents de code, projet par projet.",
        "url": "https://github.com/bmad-code-org/BMAD-METHOD",
        "license": _lic("MIT", "BMad Code, LLC", "https://github.com/bmad-code-org/BMAD-METHOD", summary=MIT_NOTE),
    },
    {
        "id": "cctemplates",
        "name": "Claude Code Templates",
        "description": "Un catalogue de centaines d'éléments prêts pour Claude Code (agents, commandes, réglages, "
        "crochets, serveurs MCP), installés en une commande.",
        "install": "npm install --global claude-code-templates@1.30.0",
        "commands": [
            _cmd("cct", "Parcourir le catalogue et choisir quoi installer.", where="terminal"),
            _cmd(
                "cct --agent <catégorie/nom> --yes",
                "Installer un agent précis (aussi --command, --mcp, --hook, --setting).",
                "cct --agent development-tools/code-reviewer --yes",
                "terminal",
            ),
        ],
        "needs": "Node.js et Claude Code.",
        "lezard": "Pour Claude Code. Lisez ce que vous installez : chaque élément donne des consignes à votre agent.",
        "url": "https://github.com/davila7/claude-code-templates",
        "license": _lic(
            "MIT", "Daniel Ávila (davila7)", "https://github.com/davila7/claude-code-templates", summary=MIT_NOTE
        ),
    },
    {
        "id": "claudemem",
        "name": "claude-mem",
        "description": "Mémoire automatique pour Claude Code : il résume ce qu'il fait pendant une session et le "
        "retrouve aux suivantes, sans rien à demander.",
        "install": "npx --yes claude-mem@15.0.1 install --provider claude",
        "commands": [
            _cmd(
                "Rien à taper : la mémoire se remplit seule",
                "Pour retrouver une décision passée, demandez-la : Claude Code fouille sa mémoire (compétence "
                "mem-search).",
                "Comment avions-nous réglé le bug de connexion la semaine dernière ?",
                "claude",
            ),
            _cmd("/learn-codebase", "Facultatif : charger d'un coup la mémoire de tout le projet.", where="claude"),
            _cmd("npx claude-mem doctor", "Vérifie l'installation et propose des corrections.", where="terminal"),
        ],
        "needs": "Node.js 20.12 ou plus et Claude Code.",
        "lezard": "Installé en mode local (--provider claude) : les résumés sont faits par votre abonnement Claude et "
        "gardés sur ce PC.",
        "warning": "Son installateur propose aussi un service en ligne payant (après 30 jours d'essai) : l'installation "
        "depuis Lezard IA ne l'active pas.",
        "url": "https://github.com/thedotmack/claude-mem",
        "license": _lic(
            "Apache-2.0", "thedotmack et contributeurs", "https://github.com/thedotmack/claude-mem", summary=APACHE_NOTE
        ),
    },
    {
        "id": "happy",
        "name": "Happy (Claude Code sur téléphone)",
        "description": "Suivre et piloter vos sessions Claude Code depuis votre téléphone (iOS, Android) : notifications, "
        "réponses, approbations, en déplacement.",
        "install": "npm install --global happy@1.2.5\nhappy claude",
        "commands": [
            _cmd(
                "happy claude",
                "Lancer Claude Code à travers Happy, puis scanner le code avec l'application.",
                where="terminal",
            )
        ],
        "needs": "Node.js, Claude Code, et l'application Happy sur le téléphone.",
        "lezard": "Complète l'accès à distance de Lezard IA pour Claude Code. Les échanges passent par le relais de "
        "Happy, chiffrés de bout en bout.",
        "url": "https://github.com/slopus/happy",
        "license": _lic("MIT", "Happy Coder Contributors", "https://github.com/slopus/happy", summary=MIT_NOTE),
    },
    {
        "id": "cloudcli",
        "name": "CloudCLI (Claude Code dans le navigateur)",
        "description": "Une interface web pour Claude Code et Codex : sessions, fichiers, terminal et Git dans le "
        "navigateur, aussi sur tablette ou téléphone.",
        "install": "npm install --global --ignore-scripts @cloudcli-ai/cloudcli@1.37.5, puis réparation de ses "
        "modules natifs par Lezard IA\ncloudcli",
        "commands": [_cmd("cloudcli", "Démarrer l'interface, puis ouvrir http://localhost:3001.", where="terminal")],
        "needs": "Node.js et Claude Code (ou Codex).",
        "lezard": "Application à part, sur ce PC. Ne l'ouvrez pas sur Internet sans protection : elle donne accès à vos "
        "fichiers et à un terminal.",
        "url": "https://github.com/siteboon/claudecodeui",
        "license": _lic(
            "AGPL-3.0",
            "Siteboon (CloudCLI)",
            "https://github.com/siteboon/claudecodeui/blob/main/LICENSE",
            "conditions",
            "Copyleft fort, y compris pour un service en ligne : une version modifiée doit être publiée sous AGPL-3.0.",
        ),
    },
    {
        "id": "superclaude",
        "name": "SuperClaude",
        "description": "Un cadre pour Claude Code : commandes spécialisées (/sc:…), modes de comportement et personas "
        "d'experts pour l'analyse, la conception, le code et les tests.",
        "install": "uv tool install superclaude==4.3.0\nsuperclaude install",
        "commands": [
            _cmd("superclaude install", "Installer les commandes et les modes dans Claude Code.", where="terminal"),
            _cmd("/sc:help", "La liste des commandes SuperClaude.", where="claude"),
        ],
        "needs": "uv (installé avec lui) et Claude Code.",
        "lezard": "Pour Claude Code. Il ajoute beaucoup de consignes : à essayer sur un projet avant de le garder.",
        "url": "https://github.com/SuperClaude-Org/SuperClaude_Framework",
        "license": _lic(
            "MIT",
            "SuperClaude Framework Contributors",
            "https://github.com/SuperClaude-Org/SuperClaude_Framework",
            summary=MIT_NOTE,
        ),
    },
    {
        "id": "markitdown",
        "name": "MarkItDown (documents en Markdown)",
        "description": "Convertir PDF, Word, Excel, PowerPoint, images, HTML ou fichiers audio en Markdown propre, "
        "le format que les IA lisent le mieux (Microsoft).",
        "install": 'uv tool install "markitdown[all]==0.1.8"',
        "commands": [
            _cmd(
                "markitdown <fichier> -o <fichier.md>",
                "Convertir un document en Markdown.",
                "markitdown rapport.pdf -o rapport.md",
                "terminal",
            )
        ],
        "needs": "uv (installé avec lui).",
        "lezard": "Le Markdown produit se joint à une conversation de Lezard IA, ou se donne à un agent de code.",
        "url": "https://github.com/microsoft/markitdown",
        "license": _lic("MIT", "Microsoft Corporation", "https://github.com/microsoft/markitdown", summary=MIT_NOTE),
    },
)

# Fiches écrites et testées, pas encore affichées : mesurées le 2026-11-11, aucune n'a montré de gain dans une
# vraie session (résumés jamais appelés, assistants et réglage jamais essayés pour de vrai). Leurs recettes
# restent dans installers ; retirer un nom d'ici suffit à publier la fiche.
EN_ATTENTE = ("resumes", "claude-etapes", "claude-allege")
AGENT_TOOLS = tuple(t for t in AGENT_TOOLS if t["id"] not in EN_ATTENTE)


# ---------- bundles : plusieurs éléments à installer ensemble, proposés selon ce PC ----------
# « when » : ram_gb (mémoire vive totale), vram_gb (carte graphique), windows, programs (au moins un trouvé sur ce PC),
# fast (chaque modèle du bundle tient dans la carte graphique). Un modèle est désigné par son usage : « model:chat »,
# ou fixé : « model:code=qwen3.8:27b » (ce modèle-là, quel que soit le PC ; écarté seulement s'il n'y tient pas).
BUNDLES = (
    {
        "id": "essentiel",
        "name": "Pour bien démarrer",
        "description": "Le moteur des modèles locaux, le meilleur modèle de discussion pour ce PC, la recherche sur "
        "Internet et la réflexion pas à pas.",
        "for": "Tout le monde : le socle pour discuter avec une IA sur son PC.",
        "items": ("component:ollama", "model:chat", "component:web", "mcp:reflexion"),
        "when": {},
    },
    {
        "id": "voix",
        "name": "Parler avec l'IA",
        "description": "Dictée et lecture des réponses à voix haute sur ce PC, avec un modèle de discussion adapté.",
        "for": "Les PC avec au moins 8 Go de mémoire vive (la voix tourne sur le processeur).",
        "items": ("component:ollama", "component:voix", "model:chat"),
        "when": {"ram_gb": 8},
    },
    {
        "id": "assistant-pc",
        "name": "Assistant qui agit sur le PC",
        "description": "L'IA ouvre vos applications, clique et tape pour vous (avec votre accord à chaque action), "
        "cherche sur Internet et se souvient de vos habitudes.",
        "for": "Les PC Windows.",
        "items": ("component:pc", "component:web", "mcp:memoire"),
        "when": {"windows": True},
    },
    {
        "id": "equipe",
        "name": "Équipe d'IA",
        "description": "Plusieurs IA se répartissent une demande complexe (agents CrewAI pour les très gros "
        "modèles), avec un modèle de raisonnement et la réflexion pas à pas.",
        "for": "Les PC puissants : 16 Go de mémoire vive et une carte graphique qui fait tourner le modèle.",
        "items": ("component:ollama", "component:multi", "model:reasoning", "mcp:reflexion"),
        "when": {"ram_gb": 16, "fast": True},
    },
    {
        "id": "code-local",
        "name": "Coder avec une IA locale",
        "description": "Continue dans VS Code, branché sur le meilleur modèle de code pour ce PC et un modèle "
        "d'embeddings pour chercher dans votre projet. Rien ne sort du PC.",
        "for": "Les développeurs dont la carte graphique fait tourner un modèle de code.",
        "items": ("component:ollama", "app:continue", "model:code", "model:embeddings"),
        "when": {"fast": True},
    },
    {
        "id": "claude-code",
        "name": "Boîte à outils Claude Code",
        "description": "La carte de votre code (bien moins de tokens), une mémoire entre les sessions et la recherche "
        "de compétences, pour Claude Code.",
        "for": "Ceux qui utilisent Claude Code sur ce PC.",
        "items": ("tool:codegraph", "tool:agentmemory", "tool:findskills"),
        "when": {"programs": ("claude",)},
    },
    {
        "id": "claude-video",
        "name": "Vidéo avec Claude Code",
        "description": "Claude Code écrit des vidéos en HTML et les rend en MP4, et regarde des vidéos pour les "
        "résumer ou les analyser.",
        "for": "Ceux qui utilisent Claude Code sur ce PC.",
        "items": ("tool:hyperframes", "tool:watch"),
        "when": {"programs": ("claude",)},
    },
    {
        "id": "video",
        "name": "Créer des vidéos avec n'importe quelle IA",
        "description": "Un agent (OpenCode) branché sur un modèle de Lezard IA écrit la vidéo en HTML et la rend en "
        "MP4 sur ce PC (HyperFrames, avec FFmpeg), et la voix de Lezard IA peut lire le commentaire. Sans Claude Code, "
        "et avec un modèle local ou en ligne.",
        "for": "Les PC avec au moins 16 Go de mémoire vive (l'agent, son modèle et le rendu tournent ensemble).",
        "items": ("component:ollama", "model:code", "app:opencode", "tool:hyperframes-cli", "component:voix"),
        "when": {"ram_gb": 16},
    },
    {
        "id": "claude-lezard",
        "name": "Claude Code sur Lezard IA",
        "description": "Claude Code branché sur les modèles de Lezard IA par un routeur, avec des méthodes de travail "
        "éprouvées et le suivi de sa consommation.",
        "for": "Ceux qui utilisent Claude Code sur ce PC.",
        "items": ("tool:ccrouter", "tool:superpowers", "tool:ccusage"),
        "when": {"programs": ("claude",)},
    },
    {
        "id": "claude-qwen",
        "name": "Claude Code avec Qwen 3.8 (local)",
        "description": "Claude Code et son extension VS Code branchés sur Qwen 3.8 27B, qui tourne sur ce PC : il "
        "reçoit tous les outils de Claude Code (fichiers, terminal, recherche, sous-agents), des méthodes de travail "
        "éprouvées, la carte de votre code et une mémoire entre les sessions. Le fichier de réglages de l'extension "
        "est dans Tableau de bord → Brancher un outil → Claude Code. Rien ne sort du PC.",
        "for": "Ceux qui utilisent Claude Code, avec 32 Go de mémoire vive ; confortable avec une carte graphique "
        "de 24 Go, lent en dessous.",
        "items": (
            "component:ollama",
            "model:code=qwen3.8:27b",
            "tool:superpowers",
            "tool:codegraph",
            "tool:agentmemory",
        ),
        "guide": "claudecode",
        "when": {"ram_gb": 32, "programs": ("claude",)},
    },
    {
        "id": "claude-econome",
        "name": "Claude Code économe",
        "description": "Claude Code garde le travail difficile et confie le reste, quand vous l'activez par "
        "/lezard on : étapes simples aux modèles locaux, brouillons contrôlés sans dépenser un token, vérification "
        "finale ; avec la carte de votre code et le suivi de sa consommation.",
        "for": "Ceux qui utilisent Claude Code sur ce PC, avec une carte graphique qui fait tourner un modèle local.",
        "items": (
            "component:ollama",
            "model:chat",
            "tool:interrupteur",
            "tool:codegraph",
            "tool:ccusage",
        ),
        "when": {"programs": ("claude",), "fast": True},
    },
    {
        "id": "claude-partout",
        "name": "Claude Code partout",
        "description": "Suivre et piloter Claude Code depuis le téléphone ou le navigateur, avec une ligne d'état qui "
        "montre le modèle, la branche et le coût de la session.",
        "for": "Ceux qui utilisent Claude Code sur ce PC.",
        "items": ("tool:happy", "tool:cloudcli", "tool:ccstatusline"),
        "when": {"programs": ("claude",)},
    },
    {
        "id": "navigateur",
        "name": "Navigateur piloté par l'IA",
        "description": "Recherche sur Internet et un vrai navigateur que l'IA pilote : ouvrir des pages, remplir des "
        "formulaires, lire et capturer le contenu.",
        "for": "Les PC avec au moins 8 Go de mémoire vive (un navigateur tourne à côté de l'IA).",
        "items": ("component:web", "mcp:playwright"),
        "when": {"ram_gb": 8},
    },
    {
        "id": "images",
        "name": "Création d'images",
        "description": "ComfyUI pour générer des images sur ce PC (commande /image), et un modèle qui sait les décrire.",
        "for": "Les PC Windows avec une carte graphique NVIDIA d'au moins 6 Go.",
        "items": ("app:comfyui", "component:ollama", "model:vision"),
        "when": {"vram_gb": 6, "windows": True, "nvidia": True},
    },
)
PROGRAM_NAMES = {"claude": "Claude Code"}
_GB = 1e9


def _gb(n: float) -> str:
    return f"{n / _GB:.0f} Go"


def _part(key: str) -> dict:
    kind, _, item = key.partition(":")
    if kind != "model":
        return {"kind": kind, "id": item}
    use, _, pinned = item.partition("=")
    part = {"kind": kind, "use": use, "use_label": USE_LABELS[use]}
    if pinned:
        label = next((m.label for m in CATALOG if m.model == pinned), pinned)
        part.update(id=pinned.split(":")[0], tag=pinned, label=label)
    return part


def public_bundles() -> list[dict]:
    """Pour les visiteurs et le site : les bundles sans rien de ce PC (un modèle reste désigné par son usage)."""
    return [
        {**{k: v for k, v in b.items() if k not in ("items", "when")}, "parts": [_part(i) for i in b["items"]]}
        for b in BUNDLES
    ]


def bundles_for(reco: dict, hw: dict, found: Callable[[str], str | None], platform: str = sys.platform) -> list[dict]:
    """Chaque bundle jugé pour ce PC : proposé ou non (et pourquoi), avec ses modèles choisis pour ce matériel.
    `reco` : model_catalog.recommend (meilleur modèle par usage) ; `hw` : relevé du matériel ; `found` : programme
    trouvé sur ce PC (installers.program)."""
    ram = (hw.get("ram") or {}).get("total") or 0
    cards = usable(hw.get("gpus") or [])  # carte dédiée, de tout fabricant ; jamais la partie graphique du processeur
    vram = max((g["vram_total"] for g in cards), default=0)
    nvidia_vram = max((g["vram_total"] for g in cards if g.get("vendor", "nvidia") == "nvidia"), default=0)
    models = {m["model"]: m for m in reco.get("models") or []}
    out = []
    for b in BUNDLES:
        when, why, why_not = b["when"], [], []
        if when.get("windows") and platform != "win32":
            why_not.append("Windows seulement.")
        if when.get("ram_gb") and ram:
            if ram < when["ram_gb"] * _GB * 0.95:  # 16 Go annoncés : un peu moins vus par Windows
                why_not.append(f"Il faut au moins {when['ram_gb']} Go de mémoire vive (ce PC : {_gb(ram)}).")
            else:
                why.append(f"{_gb(ram)} de mémoire vive.")
        if when.get("vram_gb"):
            need = nvidia_vram if when.get("nvidia") else vram  # ComfyUI (CUDA) : carte NVIDIA
            if need < when["vram_gb"] * _GB * 0.95:
                other = next((g for g in cards if g.get("vendor", "nvidia") != "nvidia"), None)
                why_not.append(
                    f"Il faut une carte graphique {'NVIDIA ' if when.get('nvidia') else ''}d'au moins "
                    f"{when['vram_gb']} Go"
                    + (
                        f" (ce PC : {_gb(need)})."
                        if need
                        else f" ({other['name']} : ComfyUI calcule avec CUDA, propre à NVIDIA)."
                        if when.get("nvidia") and other
                        else " (aucune sur ce PC)."
                    )
                )
            else:
                why.append(f"Carte graphique de {_gb(need)}.")
        if when.get("programs"):
            names = " ou ".join(PROGRAM_NAMES.get(p, p) for p in when["programs"])
            if any(found(p) for p in when["programs"]):
                why.append(f"{names} est installé sur ce PC.")
            else:
                why_not.append(f"{names} n'est pas installé sur ce PC : ces outils sont faits pour lui.")
        parts = []
        for key in b["items"]:
            part = _part(key)
            if part["kind"] == "model":
                tag = part.get("tag") or (reco.get("best") or {}).get(part["use"])
                if not tag:
                    why_not.append(f"Aucun modèle « {part['use_label']} » ne tient sur ce PC.")
                    continue
                m = models.get(tag, {})
                if when.get("fast") and m.get("fit") != "vram":
                    why_not.append(f"{m.get('label', tag)} ne tient pas dans la carte graphique : il serait lent.")
                elif m.get("fit") == "too_big":
                    why_not.append(f"{m.get('label', tag)} ne tient pas dans la mémoire de ce PC.")
                elif not when.get("fast") and m.get("fit") not in (None, "vram"):  # modèle fixé : prévenir
                    why.append(f"{m.get('label', tag)} dépasse la carte graphique : il marchera, lentement.")
                part.update(id=tag.split(":")[0], tag=tag, label=m.get("label", tag))  # installé : vu par la page
            parts.append(part)
        out.append(
            {
                **{k: v for k, v in b.items() if k not in ("items", "when")},
                "parts": parts,
                "suggested": not why_not,
                "why": why,
                "why_not": why_not,
            }
        )
    return out


# ---------- outils dans le chat : « /<commande> » pour chaque outil installé (chat_tools.py) ----------
# kind « agent » : une demande en langage courant, l'outil répond dans la conversation. runner : openclaw, aider,
#   opencode, qwen ; claude : Claude Code de ce PC avec l'extension installée (prefix : sa commande, ajoutée devant
#   la demande). Tous branchés sur Lezard IA, avec le modèle choisi dans le chat, sans compte extérieur.
# kind « cli » : les arguments du programme, comme dans un terminal ; sa sortie s'affiche dans la conversation.
# kind « open » : ouvre l'application sur ce PC (start : nom du menu Démarrer ; path ; vscode ; url) ;
#   kind « serve » : lance le programme (service) puis donne son adresse.
# mcp : extension qui passe par un serveur MCP (son nom pour Claude Code) : seul ce serveur est gardé avec une IA
# locale (chat_tools.mcp_args).
# Sans commande : bibliothèques (python, langchain), ComfyUI (déjà /image), Page Assist (extension du navigateur :
# Ctrl+Maj+Y), happy (appairage du téléphone) et ccstatusline (barre d'état de Claude Code) : rien à lancer ici.
CHAT = {
    # agents : une demande, une réponse
    "openclaw": {"kind": "agent", "runner": "openclaw", "example": "range les fichiers de mon bureau par type"},
    "aider": {"kind": "agent", "runner": "aider", "example": "écris un script Python qui renomme mes photos par date"},
    "opencode": {"kind": "agent", "runner": "opencode", "example": "crée une page HTML avec un compteur"},
    "qwencode": {"kind": "agent", "runner": "qwen", "command": "qwen", "example": "explique ce que fait ce code : …"},
    "hyperframes": {
        "kind": "agent",
        "runner": "claude",
        "prefix": "/hyperframes:hyperframes ",
        # dossier de la demande contrôlé par l'outil HyperFrames (s'il est installé) ; erreurs renvoyées à l'IA
        "prepare": {
            "program": "hyperframes",
            "args": ["init", "{name}", "--non-interactive", "--example", "blank"],
            "unless": "index.html",
            "note": "Le projet HyperFrames est déjà créé et valide dans {folder} (index.html et hyperframes.json). "
            "Ne lance pas « hyperframes init » : modifie directement {folder}\\index.html (et ajoute des fichiers si "
            "besoin), en gardant ses attributs data-composition-id, data-width, data-height et window.__timelines.",
            "tool": "hyperframes-cli",
        },
        "check": {"program": "hyperframes", "args": ["lint"], "label": "hyperframes lint", "tool": "hyperframes-cli"},
        "example": "fais une intro de 10 secondes avec un titre en fondu",
    },
    "watch": {
        "kind": "agent",
        "runner": "claude",
        # son script demande des commandes : Lezard IA le lance lui-même (chat_tools._watched), moteur local
        "watch": True,
        "example": "https://www.youtube.com/watch?v=… résume",
    },
    "rea": {
        "kind": "agent",
        "runner": "claude",
        "mcp": "rea",
        # descriptions d'outils courtes (sinon 3,4 millions de caractères pour ses 207 outils) ; une IA locale ne voit
        # que ceux d'une analyse de programme (environ 3 000 jetons au lieu de 57 000)
        "mcp_env": {"REA_MCP_INPUT_SCHEMA_PROFILE": "compact", "REA_MCP_OUTPUT_SCHEMA_PROFILE": "off"},
        "only_tools": [
            "open_binary",
            "close_binary",
            "binary_overview",
            "list_strings",
            "search_strings",
            "list_procedures",
            "search_procedures",
            "procedure_pseudo_code",
            "procedure_callers",
            "inspect_artifact",
            "analyze_javascript_application",
        ],
        "drop_args": ["document"],  # sous Windows : le programme ouvert, pas son chemin (voir mcp_filter.py)
        # le décompilateur fourni par sa recette (installers.GHIDRA, JAVA21), donné à REA à chaque demande
        "env": {
            "GHIDRA_INSTALL_DIR": r"%LOCALAPPDATA%\Programs\Ghidra\ghidra_12.1.4_PUBLIC",
            "JAVA_HOME": r"%LOCALAPPDATA%\Programs\Java21\jdk-21.0.13+9",
        },
        "example": r"décompile C:\Outils\mon-programme.exe et explique ce que fait sa fonction principale",
    },
    "agentmemory": {
        "kind": "agent",
        "runner": "claude",
        # sans son serveur (rien à faire tourner à part), sa recherche veut TOUS les mots dans le souvenir
        # (une IA locale pose la question entière) : le relais la refait mot par mot (mcp_filter.py)
        "any_word": ["memory_recall", "memory_smart_search"],
        "mcp": "agentmemory",
        "example": "qu'avions-nous décidé pour la page d'accueil ?",
    },
    # methods : avec une IA locale, Lezard IA lui donne d'office la méthode de Superpowers qui convient (chat_tools)
    "superpowers": {
        "kind": "agent",
        "runner": "claude",
        "methods": True,
        "example": "faisons une liste de tâches en React",
    },
    "speckit": {
        "kind": "agent",
        "runner": "claude",
        # ses commandes (/speckit-…) vivent dans le projet : il y est initialisé, et Claude Code y travaille
        "in_folder": True,
        "prepare": {
            "program": "specify",
            "args": ["init", "--here", "--force", "--non-interactive", "--integration", "claude"],
            "cwd": "folder",
            "unless": ".specify",
            "note": "Spec Kit est initialisé dans {folder} (dossier .specify et commandes /speckit-…).",
            "tool": "speckit",
        },
        "example": r"/speckit-specify une liste de courses partagée, dans C:\Projets\courses",
    },
    "wshobson": {"kind": "agent", "runner": "claude", "example": "fais une revue de sécurité de ce dossier"},
    "serena": {
        "kind": "agent",
        "runner": "claude",
        "mcp": "serena",
        "example": "Activate the project C:\\Projets\\site",
    },
    "bmad": {
        "kind": "agent",
        "runner": "claude",
        # ses agents et méthodes vivent dans le projet : installés là en français, et Claude Code y travaille
        "in_folder": True,
        "prepare": {
            "program": "bmad",
            "args": [
                *("install", "--directory", ".", "--modules", "bmm", "--tools", "claude-code", "--yes"),
                *("--communication-language", "Français", "--document-output-language", "Français"),
            ],
            "cwd": "folder",
            "unless": "_bmad",
            "note": "BMAD est installé dans {folder} (dossier _bmad, compétences bmad-… dans .claude/skills).",
            "tool": "bmad",
        },
        "example": r"brief produit d'une application de recettes, dans C:\Projets\recettes",
    },
    "claudemem": {
        "kind": "agent",
        "runner": "claude",
        "mcp": "claude-mem",
        "allow": ["mcp__plugin_claude-mem_mcp-search"],  # son serveur MCP vient de l'extension elle-même
        # une IA locale ne voit que ses outils de mémoire (« smart_search » cherche dans le code : elle s'y trompait)
        "deny_local": [
            f"mcp__plugin_claude-mem_mcp-search__{name}"
            for name in (
                "smart_search",
                "smart_unfold",
                "smart_outline",
                "dumb_search",
                "build_corpus",
                "rebuild_corpus",
                "query_corpus",
                "reprime_corpus",
                "prime_corpus",
                "list_corpora",
                "work_state_write",
                "work_state_read",
                "get_tool_uses",
                "session_start_context",
            )
        ],
        "example": "comment avions-nous réglé le bug de connexion ?",
    },
    "superclaude": {"kind": "agent", "runner": "claude", "example": "/sc:help"},
    # programmes en ligne de commande : arguments → sortie
    "gws": {"kind": "cli", "program": "gws", "example": "calendar +agenda --today"},
    "findskills": {"kind": "cli", "program": "npx", "args": ["--yes", "skills"], "example": "find pdf"},
    "codegraph": {"kind": "cli", "program": "codegraph", "example": "query login"},
    # Claude Code Router : ses fournisseurs se règlent dans son interface (base SQLite, pas de commande pour cela)
    "ccrouter": {
        "kind": "serve",
        "program": "ccr",
        # service détaché ; il écrit l'adresse de son interface avec son jeton d'accès, ouverte telle quelle
        "args": ["start", "--no-open"],
        "detached": True,
        "command": "ccr",
        "url": "http://127.0.0.1:3458",
    },
    "ccusage": {"kind": "cli", "program": "ccusage", "example": "daily"},
    "repomix": {"kind": "cli", "program": "repomix", "example": "--remote yamadashy/repomix --stdout"},
    "hyperframes-cli": {"kind": "cli", "program": "hyperframes", "command": "hyperframescli", "example": "doctor"},
    "cctemplates": {
        "kind": "cli",
        "program": "cct",
        "command": "cct",
        # --directory : le projet (sans lui, l'agent irait dans le dossier de travail de Lezard IA)
        "example": r"--agent development-tools/code-reviewer --yes --directory C:\Projets\site",
    },
    "markitdown": {"kind": "cli", "program": "markitdown", "example": "C:\\Users\\moi\\Documents\\rapport.pdf"},
    "ollama": {"kind": "cli", "program": "ollama", "example": "list"},
    # « claude » : son Claude Code passe par Lezard IA (chat_tools._serve)
    "cloudcli": {"kind": "serve", "program": "cloudcli", "url": "http://localhost:3001", "claude": True},
    # applications : ouvertes sur ce PC
    # lancé par Lezard IA (paquet Python, sans Docker), branché d'office sur lui (chat_tools._serve)
    "openwebui": {
        "kind": "serve",
        "program": "open-webui",
        "args": ["serve", "--host", "127.0.0.1", "--port", "3000"],
        "url": "http://localhost:3000",
        "openwebui": True,
    },
    # lancé par Lezard IA sans Docker (librechat_run.py : MongoDB puis Node.js), branché d'office (app_connect)
    "librechat": {"kind": "serve", "script": "librechat_run", "folder": "LibreChat", "url": "http://localhost:3080"},
    "continue": {"kind": "open", "vscode": True},
    "cline": {"kind": "open", "vscode": True},
    "kilocode": {"kind": "open", "vscode": True},
    "cursor": {"kind": "open", "start": "Cursor"},
    "zed": {"kind": "open", "start": "Zed"},
    "jan": {"kind": "open", "start": "Jan"},
    "chatbox": {"kind": "open", "start": "Chatbox"},
    "anythingllm": {"kind": "open", "start": "AnythingLLM"},
    "openhuman": {"kind": "open", "start": "OpenHuman"},
    "meetily": {"kind": "open", "start": "Meetily"},
    "cherrystudio": {"kind": "open", "start": "Cherry Studio"},
    "orca": {"kind": "open", "start": "Orca"},
    "goose": {"kind": "open", "path": "%LOCALAPPDATA%\\Programs\\Goose\\dist-windows\\Goose.exe", "start": "Goose"},
}
CHAT_HELP = {  # précédé du nom de l'outil : « Aider répond dans la conversation… »
    "agent": "répond dans la conversation, branché sur Lezard IA (demande en langage courant)",
    "agent:claude": "répond dans la conversation, par Claude Code de ce PC branché sur Lezard IA (demande en langage "
    "courant)",
    "cli": "se lance avec ces arguments, comme dans un terminal ; sa sortie s'affiche dans la conversation",
    "serve": "démarre sur ce PC ; son adresse s'affiche dans la conversation",
    "open": "s'ouvre sur ce PC",
}


def chat_command(eid: str) -> str:
    """Nom de la commande « / » d'un élément (lettres seulement, comme les commandes du chat)."""
    return CHAT[eid].get("command") or eid


def chat_usage(eid: str) -> dict:
    """La commande du chat d'un élément, telle que la fiche et le menu « / » l'affichent."""
    spec, name = CHAT[eid], chat_command(eid)
    kind = spec["kind"]
    args = {"agent": " <demande>", "cli": " <arguments>"}.get(kind, "")
    what = CHAT_HELP.get(f"{kind}:{spec.get('runner')}") or CHAT_HELP[kind]
    example = f"/{name} {spec['example']}" if spec.get("example") else None
    return {"command": name, "kind": kind, "use": f"/{name}{args}", "what": what, "example": example}


def _with_chat_commands() -> None:
    """La commande du chat de chaque outil, en tête de ses commandes (fiche, case « Commande à utiliser »)."""
    for entry in (*APPS, *AGENT_TOOLS, *COMPONENTS):
        if entry["id"] in CHAT and not any(c["where"] == WHERE["chat"] for c in entry.get("commands", ())):
            u = chat_usage(entry["id"])
            what = f"Dans le chat de Lezard IA (clé maître) : {entry['name']} {u['what']}."
            entry["commands"] = [_cmd(u["use"], what, u["example"]), *entry.get("commands", ())]


_with_chat_commands()
