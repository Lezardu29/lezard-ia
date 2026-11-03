"""Installation en un clic depuis la marketplace (clé maître) : recettes figées, jamais une commande saisie.

Chaque recette est une suite d'étapes (programme puis arguments), lancées l'une après l'autre sans interpréteur de
commandes. Le programme est cherché dans le PATH ; « {python} » est le Python de Lezard IA. Comme Ollama et les
serveurs MCP, les paquets npm sont figés sur une version publiée depuis plus de 14 jours ; les paquets Python y sont
plafonnés (une version déjà installée est gardée : d'autres paquets de ce Python en dépendent). winget et le
gestionnaire d'extensions de VS Code vérifient eux-mêmes ce qu'ils installent. Les modèles (Ollama), les
compétences et les serveurs MCP ont leur propre route.

Dépendances : un programme requis absent (Node.js, Claude Code, Docker, VS Code, FFmpeg, yt-dlp) est installé
d'abord, par winget (paquets officiels), puis le PATH de ce processus est relu pour le trouver aussitôt. La page les
annonce avant de confirmer (nom, taille, licence).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .marketplace import MarketplaceError

PYTHON = "{python}"
OPENWEBUI_IMAGE = "ghcr.io/open-webui/open-webui:v0.12.3"  # publiée le 2026-10-16
SETUP = str(Path(__file__).with_name("setup_download.py"))  # installateur officiel figé, vérifié, lancé sans question
LIBRECHAT_SETUP = str(Path(__file__).with_name("librechat_setup.py"))  # dépôt figé, secrets de ce PC, images figées
ORCA = (
    "https://github.com/stablyai/orca/releases/download/v1.4.229/orca-windows-setup.exe",  # publiée le 2026-10-17
    "fb759053a56ad019a2d52882abaf9c797e2adddf6f217a36f0b64505446d007d",
    "227899392",
)
OPENHUMAN = (
    "https://github.com/tinyhumansai/openhuman/releases/download/v0.64.15/OpenHuman_0.64.15_x64-setup.exe",  # 2026-10-09
    "fff6a9588c2f71334df8ec1f395b3bfae7395d77e84c97fef50950dd6411a2d6",
    "121433225",
)
ANYTHINGLLM = (
    "https://github.com/Mintplex-Labs/anything-llm/releases/download/v1.18.0/AnythingLLMDesktop.exe",  # 2026-10-13
    "b572dc48fbf6967bd3d7a408468fbdb7aa3adbc75d90fc52421385fd1a799e5e",
    "407381240",
)
JAN = (
    "https://github.com/janhq/jan/releases/download/v0.8.6/Jan_0.8.6_x64-setup.exe",  # publiée le 2026-10-09
    "f47f2d205eb92743bdd60ffe7d3b7b434b45a97a8dce092c1185c97d1f5a5268",
    "556441144",
)
MEETILY = (
    "https://github.com/Zackriya-Solutions/meetily/releases/download/v0.4.1/meetily_0.4.1_x64-setup.exe",  # 2026-09-12
    "6321068c98da8bf5392df3825aaa5be0e28349e54436f53d485c08c54b9c9315",
    "42483368",
)
CHERRY = (
    "https://github.com/CherryHQ/cherry-studio/releases/download/v2.1.7/Cherry-Studio-2.1.7-win-x64-setup.exe",  # 2026-10-19
    "3e0562ad5f588e8e3988a501549dce0d75183fb76ff3a616df1112bba11a83d1",
    "310808240",
)
GOOSE = (  # pas d'installateur avant la 1.57 (moins de 14 jours) : l'archive officielle, décompressée
    "https://github.com/aaif-goose/goose/releases/download/v1.55.0/Goose-win32-x64.zip",  # publiée le 2026-10-16
    "4a10e547730832afc2fb13d1c3c4e60b0332ce3e49fd3793cb04259206054c0c",
    "246032962",
)
LIBRECHAT_TAG = "v0.8.8"  # publiée le 2026-10-01
PAGE_ASSIST = "https://chromewebstore.google.com/detail/page-assist/jfgfiigpkhlkbnfnbobbkinehhfdhndo"
# où chaque sorte d'élément s'installe : affiché avant de confirmer (rien à déplacer ensuite)
NPM = "Outils Node.js de ce PC (installation globale de npm)"
CLAUDE = "Dans Claude Code (extensions de Claude Code)"
WINGET = "Programmes de Windows, avec son raccourci dans le menu Démarrer"
PIP = "Le Python de Lezard IA, sur ce PC"
USER_PIP = "Votre Python (celui de vos scripts), pour votre compte Windows"


# programme requis -> paquet qui l'installe (Windows, winget) ; affiché avant de confirmer
DEPENDENCIES: dict[str, dict] = {
    "node": {
        "name": "Node.js (LTS, 24 ou plus)",
        "winget": "OpenJS.NodeJS.LTS",
        "size": "~30 Mo",
        "license": "MIT",
        "provides": ("node", "npm", "npx"),
        "min_major": 24,  # serveurs MCP officiels 2.0.0 ; plus ancien : traité comme absent (winget le met à jour)
    },
    "claude": {
        "name": "Claude Code",
        "winget": "Anthropic.ClaudeCode",
        "size": "~70 Mo",
        "license": "Logiciel propriétaire (conditions d'Anthropic ; compte Claude requis)",
        "provides": ("claude",),
    },
    "docker": {
        "name": "Docker Desktop",
        "winget": "Docker.DockerDesktop",
        "size": "~600 Mo",
        "license": "Propriétaire : gratuit pour un usage personnel et les petites entreprises",
        "provides": ("docker",),
        "note": "Lancez Docker Desktop une fois (un redémarrage du PC peut être demandé), puis réinstallez l'élément.",
    },
    "code": {
        "name": "Visual Studio Code",
        "winget": "Microsoft.VisualStudioCode",
        "size": "~100 Mo",
        "license": "Licence Microsoft (gratuit)",
        "provides": ("code",),
    },
    "ffmpeg": {
        "name": "FFmpeg",
        "winget": "Gyan.FFmpeg",
        "size": "~90 Mo",
        "license": "GPL-3.0",
        "provides": ("ffmpeg",),
    },
    "yt-dlp": {
        "name": "yt-dlp",
        "winget": "yt-dlp.yt-dlp",
        "size": "~20 Mo",
        "license": "Unlicense (domaine public)",
        "provides": ("yt-dlp",),
    },
    "uv": {
        "name": "uv (gestionnaire Python d'Astral)",
        "winget": "astral-sh.uv",
        "size": "~20 Mo",
        "license": "MIT / Apache-2.0",
        "provides": ("uv", "uvx"),
    },
    "py": {
        "name": "Python 3.13 (pour vos scripts)",
        "winget": "Python.Python.3.13",
        "size": "~30 Mo",
        "license": "PSF-2.0",
        "provides": ("py",),
    },
    "git": {
        "name": "Git",
        "winget": "Git.Git",
        "size": "~65 Mo",
        "license": "GPL-2.0",
        "provides": ("git",),
    },
}
PROVIDER = {prog: dep for dep, d in DEPENDENCIES.items() for prog in d["provides"]}


def _winget(package: str) -> tuple[str, ...]:
    return (
        "winget",
        "install",
        "--id",
        package,
        "--exact",
        "--silent",
        "--disable-interactivity",
        "--accept-package-agreements",
        "--accept-source-agreements",
    )


def _pip(*packages: str) -> tuple[str, ...]:
    return (PYTHON, "-m", "pip", "install", "--disable-pip-version-check", *packages)


# Python de l'utilisateur (celui de ses scripts) : le Python de Lezard IA installé par l'installateur ou le zip est
# embarqué, sans pip, et ce qu'on y ajouterait ne servirait pas aux scripts de l'utilisateur
USER_PYTHON = ("py", "-3") if sys.platform == "win32" else ("python3",)


def _pip_user(*packages: str) -> tuple[str, ...]:
    return (*USER_PYTHON, "-m", "pip", "install", "--user", "--disable-pip-version-check", *packages)


# kind:id -> étapes ; « size » : téléchargement ; « where » : où ça s'installe ; « after » : ce qu'il reste à faire soi-même
# (connexion à un compte, branchement…)
RECIPES: dict[str, dict] = {
    "tool:hyperframes": {
        "steps": [
            ("claude", "plugin", "marketplace", "add", "heygen-com/hyperframes"),
            ("claude", "plugin", "install", "hyperframes@hyperframes"),
        ],
        "requires": ("node",),  # rendu des vidéos
        "size": "quelques Mo",
        "where": CLAUDE,
        "after": "Redémarrez Claude Code : la compétence HyperFrames est disponible.",
    },
    "tool:watch": {
        "steps": [
            ("claude", "plugin", "marketplace", "add", "bradautomates/claude-video"),
            ("claude", "plugin", "install", "watch@claude-video"),
        ],
        "requires": ("ffmpeg", "yt-dlp"),  # traitement des vidéos
        "size": "quelques Mo",
        "where": CLAUDE,
        "after": "Redémarrez Claude Code, puis /watch suivi de l'adresse d'une vidéo.",
    },
    "tool:rea": {
        "steps": [("npx", "--yes", "rea-agents@6.7.0", "setup", "--client", "claude_code", "--yes")],
        "size": "29 Mo",
        "where": "Dans Claude Code (serveur MCP et consignes de REA)",
        "after": "Redémarrez Claude Code. Hopper, Ghidra ou IDA restent à installer à part pour les binaires.",
    },
    "tool:gws": {
        "steps": [("npm", "install", "--global", "@googleworkspace/cli@0.22.5")],
        "size": "moins de 30 Mo",
        "where": NPM + " : commande gws",
        "after": "Dans un terminal : gws auth login, pour autoriser votre compte Google.",
    },
    "tool:findskills": {
        "steps": [
            (
                "npx",
                "--yes",
                "skills@1.7.2",
                "add",
                "vercel-labs/skills",
                "--skill",
                "find-skills",
                "--global",
                "--agent",
                "claude-code",
                "--yes",
            )
        ],
        "size": "1 Mo",
        "where": "Compétences de Claude Code (dossier ~/.claude/skills)",
        "after": "Redémarrez Claude Code, puis demandez-lui de trouver une compétence.",
    },
    "tool:orca": {  # pas de paquet winget : l'installateur officiel de GitHub, figé et vérifié
        "steps": [(PYTHON, SETUP, *ORCA, "/S")],
        "show": [
            "Téléchargement de l'installateur officiel d'Orca 1.4.229 (GitHub, empreinte vérifiée)",
            "Installation silencieuse, puis l'installateur est effacé",
        ],
        "size": "228 Mo",
        "where": "Programmes de Windows (dossier de l'utilisateur), avec son raccourci dans le menu Démarrer",
        "windows": True,
        "after": "Ouvrez Orca depuis le menu Démarrer.",
    },
    "tool:codegraph": {
        "steps": [
            ("npm", "install", "--global", "@colbymchenry/codegraph@1.6.2"),
            ("codegraph", "install", "--yes"),  # branché sur les agents trouvés sur ce PC
        ],
        "provides": ("codegraph",),
        "size": "~10 Mo",
        "where": NPM + " : commande codegraph, branchée sur vos agents de code",
        "after": "Dans chaque projet, une fois : codegraph init (construit la carte du code).",
    },
    "tool:agentmemory": {
        "steps": [
            (
                "claude",
                "mcp",
                "add",
                "--scope",
                "user",
                "agentmemory",
                "--",
                "cmd",
                "/c",
                "npx",
                "-y",
                "@agentmemory/mcp@0.9.30",
            ),
        ],
        "requires": ("node",),  # le serveur est lancé par npx
        "size": "~9 Mo",
        "where": "Serveur MCP de Claude Code (pour tous vos projets)",
        "after": "Redémarrez Claude Code : ses outils de mémoire sont disponibles.",
    },
    "tool:ccrouter": {
        "steps": [("npm", "install", "--global", "@musistudio/claude-code-router@3.1.3")],
        "requires": ("claude",),
        "size": "~37 Mo",
        "where": NPM + " : commande ccr",
        "after": "Dans un terminal : ccr ui, puis ajoutez Lezard IA dans Providers (adresse de votre gateway suivie de "
        "/v1, protocole OpenAI Chat, votre clé) ; ensuite ccr code lance Claude Code sur ces modèles.",
    },
    "tool:superpowers": {
        "steps": [
            ("claude", "plugin", "marketplace", "add", "obra/superpowers-marketplace"),
            ("claude", "plugin", "install", "superpowers@superpowers-marketplace"),
        ],
        "size": "quelques Mo",
        "where": CLAUDE,
        "after": "Redémarrez Claude Code : les compétences Superpowers se déclenchent d'elles-mêmes.",
    },
    "tool:speckit": {
        "steps": [("uv", "tool", "install", "specify-cli==1.2.2")],
        "size": "~10 Mo",
        "where": "Outils Python de ce PC (uv tool) : commande specify",
        "after": "Dans le dossier d'un projet : specify init . --integration claude, puis /speckit-specify dans "
        "Claude Code.",
    },
    "tool:wshobson": {
        "steps": [
            ("claude", "plugin", "marketplace", "add", "wshobson/agents"),
            *(
                ("claude", "plugin", "install", f"{p}@claude-code-workflows")
                for p in ("debugging-toolkit", "comprehensive-review", "python-development", "javascript-typescript")
            ),
        ],
        "size": "quelques Mo",
        "where": CLAUDE,
        "after": "Redémarrez Claude Code. /plugin pour ajouter d'autres extensions de la bibliothèque.",
    },
    "tool:ccusage": {
        "steps": [("npm", "install", "--global", "ccusage@20.0.37")],
        "size": "~1 Mo",
        "where": NPM + " : commande ccusage",
        "after": "Dans un terminal : ccusage daily.",
    },
    "tool:repomix": {
        "steps": [("npm", "install", "--global", "repomix@1.18.1")],
        "size": "~20 Mo",
        "where": NPM + " : commande repomix",
        "after": "Dans le dossier d'un projet : repomix.",
    },
    "tool:hyperframes-cli": {
        "steps": [("npm", "install", "--global", "hyperframes@0.8.177")],  # publiée le 2026-10-19
        "requires": ("ffmpeg",),  # rendu en MP4
        "size": "~60 Mo, plus Chrome pour le rendu s'il n'est pas sur ce PC",
        "where": NPM + " : commande hyperframes",
        "after": "Dans un terminal : hyperframes doctor (vérifie Chrome et FFmpeg), puis hyperframes init ma-video ; "
        "demandez ensuite la vidéo à votre agent (OpenCode, Goose, Aider…) ouvert dans ce dossier.",
    },
    "tool:ccstatusline": {
        "steps": [("npm", "install", "--global", "ccstatusline@2.2.32")],
        "size": "~3 Mo",
        "where": NPM + " : commande ccstatusline",
        "after": "Dans un terminal : ccstatusline, pour choisir les éléments de la ligne d'état et l'installer.",
    },
    "tool:bmad": {
        "steps": [("npm", "install", "--global", "bmad-method@6.12.4")],
        "requires": ("uv",),  # réglage des projets et scripts de la méthode
        "size": "~3 Mo",
        "where": NPM + " : commande bmad-method",
        "after": "Dans le dossier d'un projet : bmad-method install, puis demandez « bmad status » à votre agent.",
    },
    "tool:cctemplates": {
        "steps": [("npm", "install", "--global", "claude-code-templates@1.30.0")],
        "size": "~3 Mo",
        "where": NPM + " : commandes cct et claude-code-templates",
        "after": "Dans le dossier d'un projet : cct, pour parcourir le catalogue.",
    },
    "tool:claudemem": {
        "steps": [("npx", "--yes", "claude-mem@15.0.1", "install", "--provider", "host")],
        "requires": ("claude",),
        "size": "~25 Mo",
        "where": "Dans Claude Code (extension et crochets de claude-mem), mémoire gardée sur ce PC",
        "after": "Redémarrez Claude Code : la mémoire se remplit d'elle-même au fil des sessions.",
    },
    "tool:happy": {
        "steps": [("npm", "install", "--global", "happy@1.2.5")],
        "requires": ("claude",),
        "size": "~110 Mo",
        "where": NPM + " : commande happy",
        "after": "Installez l'application Happy sur votre téléphone, puis dans un terminal : happy claude, et scannez "
        "le code affiché.",
    },
    "tool:cloudcli": {
        "steps": [("npm", "install", "--global", "@cloudcli-ai/cloudcli@1.37.5")],
        "size": "~10 Mo",
        "where": NPM + " : commande cloudcli",
        "after": "Dans un terminal : cloudcli, puis ouvrez http://localhost:3001.",
    },
    "tool:superclaude": {
        "steps": [("uv", "tool", "install", "superclaude==4.3.0")],
        "size": "~5 Mo",
        "where": "Outils Python de ce PC (uv tool) : commande superclaude",
        "after": "Dans un terminal : superclaude install, puis /sc:help dans Claude Code.",
    },
    "tool:markitdown": {
        "steps": [("uv", "tool", "install", "markitdown[all]==0.1.8")],
        "size": "~150 Mo (lecteurs PDF, Office et audio compris)",
        "where": "Outils Python de ce PC (uv tool) : commande markitdown",
        "after": "Dans un terminal : markitdown document.pdf -o document.md.",
    },
    "tool:serena": {
        "steps": [
            ("uv", "tool", "install", "-p", "3.13", "serena-agent==2.1.0"),
            (
                "claude",
                "mcp",
                "add",
                "--scope",
                "user",
                "serena",
                "--",
                "serena",
                "start-mcp-server",
                "--context",
                "claude-code",
                "--project-from-cwd",
            ),
        ],
        "size": "~150 Mo (Python 3.13 compris s'il manque)",
        "where": "Outils Python de ce PC (uv tool) : commande serena, branchée sur Claude Code",
        "after": "Redémarrez Claude Code dans un projet : /mcp montre Serena.",
    },
    "app:anythingllm": {
        "steps": [(PYTHON, SETUP, *ANYTHINGLLM, "/S")],
        "show": [
            "Téléchargement de l'installateur officiel d'AnythingLLM 1.18.0 (GitHub, empreinte vérifiée)",
            "Installation silencieuse, puis l'installateur est effacé",
        ],
        "size": "407 Mo",
        "where": "Programmes de Windows (dossier de l'utilisateur), avec son raccourci dans le menu Démarrer",
        "windows": True,
        "after": "Ouvrez AnythingLLM, puis fournisseur « Generic OpenAI » : adresse de votre gateway suivie de /v1 et "
        "votre clé.",
    },
    "app:librechat": {
        "steps": [
            (PYTHON, LIBRECHAT_SETUP, LIBRECHAT_TAG, "LibreChat"),
            ("docker", "compose", "--project-directory", "LibreChat", "up", "--detach"),
        ],
        "show": [
            f"Téléchargement de LibreChat {LIBRECHAT_TAG} (dépôt officiel, dans le dossier LibreChat de votre dossier "
            "personnel)",
            "Réglages créés : secrets tirés au hasard pour ce PC (.env), images Docker à version figée",
            "docker compose up --detach : LibreChat démarre sur http://localhost:3080",
        ],
        "requires": ("git", "docker"),
        "size": "~50 Mo, puis ~3 Go d'images Docker",
        "where": "Le dossier LibreChat de votre dossier personnel, et des conteneurs Docker (http://localhost:3080)",
        "after": "Ouvrez http://localhost:3080 et créez votre compte, puis branchez Lezard IA avec le guide "
        "« LibreChat » (librechat.yaml).",
    },
    "app:jan": {
        "steps": [(PYTHON, SETUP, *JAN, "/S")],
        "show": [
            "Téléchargement de l'installateur officiel de Jan 0.8.6 (GitHub, empreinte vérifiée)",
            "Installation silencieuse, puis l'installateur est effacé",
        ],
        "size": "556 Mo",
        "where": "Programmes de Windows (dossier de l'utilisateur), avec son raccourci dans le menu Démarrer",
        "windows": True,
        "after": "Ouvrez Jan, puis Réglages > Fournisseurs : ajoutez Lezard IA (adresse de votre gateway suivie de /v1 "
        "et votre clé).",
    },
    "app:cline": {
        "steps": [("code", "--install-extension", "saoudrizwan.claude-dev")],
        "size": "~30 Mo",
        "where": "Dans VS Code (extensions)",
        "after": "Dans Cline : fournisseur « OpenAI Compatible », adresse de votre gateway suivie de /v1 et votre clé.",
    },
    "app:kilocode": {
        "steps": [("code", "--install-extension", "kilocode.Kilo-Code")],
        "size": "~40 Mo",
        "where": "Dans VS Code (extensions)",
        "after": "Dans Kilo Code : fournisseur « OpenAI Compatible », adresse de votre gateway suivie de /v1 et votre "
        "clé.",
    },
    "app:aider": {
        "steps": [("uv", "tool", "install", "--python", "3.12", "aider-chat==0.86.2")],
        "size": "~200 Mo (Python 3.12 compris s'il manque)",
        "where": "Outils Python de ce PC (uv tool) : commande aider",
        "after": "Réglez OPENAI_API_BASE (adresse de votre gateway suivie de /v1) et OPENAI_API_KEY, puis aider "
        "--model openai/<modèle> dans un projet Git.",
    },
    "app:opencode": {
        "steps": [("npm", "install", "--global", "opencode-ai@1.19.3")],
        "size": "~120 Mo",
        "where": NPM + " : commande opencode",
        "after": "Déclarez Lezard IA dans opencode.json (fournisseur compatible OpenAI), puis opencode dans un projet.",
    },
    "app:goose": {
        "steps": [(PYTHON, SETUP, *GOOSE, "Goose")],
        "show": [
            "Téléchargement de l'archive officielle de Goose 1.55.0 (GitHub, empreinte vérifiée)",
            "Décompression dans le dossier Programmes de l'utilisateur, puis l'archive est effacée",
        ],
        "size": "246 Mo à télécharger, ~660 Mo une fois décompressé",
        "where": "Le dossier %LOCALAPPDATA%\\Programs\\Goose (application prête à lancer)",
        "windows": True,
        "after": "Lancez dist-windows\\Goose.exe dans %LOCALAPPDATA%\\Programs\\Goose, puis fournisseur « OpenAI "
        "Compatible » : adresse de votre gateway et votre clé.",
    },
    "app:pageassist": {
        "steps": [(PYTHON, "-m", "webbrowser", "-t", PAGE_ASSIST)],  # une extension s'ajoute depuis le navigateur
        "show": ["Ouverture de la page de l'extension dans le Chrome Web Store (aussi pour Edge et Brave)"],
        "size": "~10 Mo",
        "where": "Extension de votre navigateur (Chrome, Edge, Brave ; Firefox : addons.mozilla.org)",
        "after": "Cliquez sur « Ajouter » dans la page ouverte ; Page Assist trouve Ollama tout seul.",
    },
    "app:chatbox": {
        "steps": [_winget("Bin-Huang.Chatbox.CommunityEdition")],
        "size": "~100 Mo",
        "where": WINGET,
        "after": "Ouvrez Chatbox, puis Réglages : fournisseur compatible OpenAI avec l'adresse de votre gateway et "
        "votre clé.",
    },
    "app:meetily": {
        "steps": [(PYTHON, SETUP, *MEETILY, "/S")],
        "show": [
            "Téléchargement de l'installateur officiel de Meetily 0.4.1 (GitHub, empreinte vérifiée)",
            "Installation silencieuse, puis l'installateur est effacé",
        ],
        "size": "42 Mo, plus le modèle de transcription au premier lancement",
        "where": "Programmes de Windows (dossier de l'utilisateur), avec son raccourci dans le menu Démarrer",
        "windows": True,
        "after": "Ouvrez Meetily et choisissez Ollama pour les résumés.",
    },
    "app:qwencode": {
        "steps": [("npm", "install", "--global", "@qwen-code/qwen-code@0.25.3")],
        "size": "~135 Mo",
        "where": NPM + " : commande qwen",
        "after": "Branchez Lezard IA avec le guide « Qwen Code », puis qwen dans un projet.",
    },
    "app:zed": {
        "steps": [_winget("ZedIndustries.Zed")],
        "size": "~150 Mo",
        "where": WINGET,
        "after": "Branchez Lezard IA avec le guide « Zed ».",
    },
    "app:cherrystudio": {
        "steps": [(PYTHON, SETUP, *CHERRY, "/S")],
        "show": [
            "Téléchargement de l'installateur officiel de Cherry Studio 2.1.7 (GitHub, empreinte vérifiée)",
            "Installation silencieuse, puis l'installateur est effacé",
        ],
        "size": "311 Mo",
        "where": "Programmes de Windows (dossier de l'utilisateur), avec son raccourci dans le menu Démarrer",
        "windows": True,
        "after": "Ouvrez Cherry Studio, puis branchez Lezard IA avec le guide « Cherry Studio ».",
    },
    "app:openhuman": {
        "steps": [(PYTHON, SETUP, *OPENHUMAN, "/S")],
        "show": [
            "Téléchargement de l'installateur officiel d'OpenHuman 0.64.15 (GitHub, empreinte vérifiée)",
            "Installation silencieuse, puis l'installateur est effacé",
        ],
        "size": "121 Mo",
        "where": "Programmes de Windows (dossier de l'utilisateur), avec son raccourci dans le menu Démarrer",
        "windows": True,
        "after": "Ouvrez OpenHuman depuis le menu Démarrer, puis connectez les services de votre choix.",
    },
    "app:openwebui": {
        "steps": [
            (
                "docker",
                "run",
                "--detach",
                "--name",
                "open-webui",
                "--restart",
                "always",
                "--publish",
                "127.0.0.1:3000:8080",
                "--volume",
                "open-webui:/app/backend/data",
                "--add-host",
                "host.docker.internal:host-gateway",
                OPENWEBUI_IMAGE,
            )
        ],
        "size": "~2 Go (image Docker)",
        "where": "Un conteneur Docker sur ce PC, ouvert sur http://localhost:3000",
        "after": "Ouvrez http://localhost:3000, puis branchez Lezard IA avec le guide « Open WebUI ».",
    },
    "app:continue": {
        "steps": [("code", "--install-extension", "Continue.continue")],
        "size": "~60 Mo",
        "where": "Dans VS Code (extensions)",
        "after": "Branchez Lezard IA avec le guide « Continue ».",
    },
    "app:cursor": {
        "steps": [_winget("Anysphere.Cursor")],
        "size": "~150 Mo",
        "where": WINGET,
        "after": "Branchez Lezard IA avec le guide « Cursor ».",
    },
    "app:openclaw": {
        "steps": [("npm", "install", "--global", "openclaw@2026.10.5")],
        "size": "258 Mo",
        "where": NPM + " : commande openclaw",
        "after": "Branchez Lezard IA avec le guide « OpenClaw » (fichier ~/.openclaw/openclaw.json).",
    },
    "app:comfyui": {
        "steps": [_winget("Comfy.ComfyUI-Desktop")],
        "size": "~200 Mo, puis quelques Go au premier lancement (modèles d'images)",
        "where": WINGET,
        "after": "Lancez ComfyUI sur un autre port que celui de Lezard IA, puis ajoutez son adresse dans "
        ".env (COMFYUI_URL=http://127.0.0.1:8188).",
    },
    "app:python": {
        "steps": [_pip_user("openai<=3.35.1")],
        "size": "~2 Mo",
        "where": USER_PIP,
        "after": "Exemple de code : guide « Python ».",
    },
    "app:langchain": {
        "steps": [_pip_user("langchain-openai<=1.7.2")],
        "size": "~10 Mo",
        "where": USER_PIP,
        "after": "Exemple de code : guide « LangChain ».",
    },
    # composants, lancement depuis le code (sans l'installation Windows, qui passe par lezard.py)
    "component:voix": {"steps": [_pip("-r", "requirements-local.txt")], "restart": True, "where": PIP},
    "component:multi": {"steps": [_pip("-r", "requirements-multi.txt")], "restart": True, "where": PIP},
    "component:web": {"env": ("TOOLS_WEB", "true"), "where": "Réglage de Lezard IA (.env), actif tout de suite"},
    "component:pc": {
        "env": ("TOOLS_PC", "true"),
        "windows": True,
        "where": "Réglage de Lezard IA (.env), actif tout de suite",
    },
    "component:ollama": {"steps": [_winget("Ollama.Ollama")], "windows": True, "where": WINGET},
}


def recipe(key: str) -> dict:
    found = RECIPES.get(key)
    if not found:
        raise MarketplaceError(404, f"Pas d'installation en un clic pour « {key} ».")
    return found


def program(name: str) -> str | None:
    if name == PYTHON:
        return sys.executable
    if os.path.isabs(name):
        return name if os.path.exists(name) else None
    return shutil.which(name) or (shutil.which(f"{name}.cmd") if sys.platform == "win32" else None)


def refresh_path() -> None:
    """Après une installation par winget : le PATH de Windows (machine + utilisateur) relu pour ce processus, pour
    trouver le programme qui vient d'arriver sans redémarrer Lezard IA."""
    if sys.platform != "win32":
        return
    import winreg

    found = []
    for root, sub in (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(root, sub) as k:
                found += os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]).split(";")
        except OSError:
            pass
    links = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links")
    paths = [p for p in [*os.environ.get("PATH", "").split(";"), *found, links] if p]
    os.environ["PATH"] = ";".join(dict.fromkeys(paths))


def _programs(key: str) -> list[str]:
    r = recipe(key)
    names = [argv[0] for argv in r.get("steps", ())] + list(r.get("requires", ()))
    made = set(r.get("provides", ()))  # programmes installés par une étape précédente de la recette
    return [n for n in dict.fromkeys(names) if n not in made]


_MAJORS: dict[tuple[str, float], int | None] = {}


def _major(exe: str) -> int | None:
    """Version majeure du programme (« v22.11.0 » -> 22), relue s'il a changé (mise à jour) ; None si illisible."""
    try:
        key = (exe, os.path.getmtime(exe))
    except OSError:
        return None
    if key not in _MAJORS:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout  # fmt: skip
            _MAJORS[key] = int(out.strip().lstrip("v").split(".")[0])
        except (OSError, subprocess.SubprocessError, ValueError):
            _MAJORS[key] = None
    return _MAJORS[key]


def too_old(name: str) -> bool:
    """Programme présent mais trop ancien (npm et npx : la version de Node.js) ; version illisible : accepté."""
    dep = DEPENDENCIES.get(PROVIDER.get(name, ""), {})
    minimum = dep.get("min_major")
    exe = program(dep["provides"][0]) if minimum else None
    major = _major(exe) if exe else None
    return major is not None and major < minimum


def _absent(names) -> tuple[list[str], list[str]]:
    """Programmes absents ou trop anciens : (dépendances installables par winget, programmes à installer soi-même)."""
    auto = sys.platform == "win32" and program("winget") is not None
    deps, manual = [], []
    for name in names:
        old = program(name) is not None and too_old(name)
        if program(name) is not None and not old:
            continue
        dep = PROVIDER.get(name)
        if dep and auto:
            if dep not in deps:
                deps.append(dep)
        else:
            manual.append(f"{name} {DEPENDENCIES[dep]['min_major']} ou plus" if old else name)
    return deps, sorted(set(manual))


def dependencies(key: str) -> list[dict]:
    """Ce qui sera installé d'abord, faute d'être sur ce PC : nom, taille, licence."""
    return [
        {"id": d, **{k: v for k, v in DEPENDENCIES[d].items() if k != "provides"}} for d in _absent(_programs(key))[0]
    ]


def missing(key: str) -> list[str]:
    """Programmes requis absents que la page ne sait pas installer (winget absent, autre système)."""
    r = recipe(key)
    if r.get("windows") and sys.platform != "win32":
        return ["Windows"]
    return _absent(_programs(key))[1]


def possible_dependencies(key: str | None = None, names=()) -> list[dict]:
    """Pour le site (sans connaître le PC) : ce qui serait installé d'abord si c'était absent."""
    progs = _programs(key) if key else list(names)
    deps = dict.fromkeys(PROVIDER[p] for p in progs if p in PROVIDER)
    return [{"id": d, **{k: v for k, v in DEPENDENCIES[d].items() if k != "provides"}} for d in deps]


def dependency_steps(names) -> list[tuple[str, ...]]:
    """Étapes winget des dépendances absentes pour ces programmes (serveurs MCP : npx)."""
    return [_winget(DEPENDENCIES[d]["winget"]) for d in _absent(names)[0]]


def plan(key: str) -> list[tuple[str, ...]]:
    """Dépendances absentes d'abord, puis les étapes de la recette ; les programmes sont cherchés au moment de lancer
    chaque étape (resolve), après l'arrivée des dépendances."""
    lacking = missing(key)
    if lacking:
        raise MarketplaceError(409, f"Requis sur ce PC : {', '.join(lacking)}. Installez-le, puis réessayez.")
    return [*dependency_steps(_programs(key)), *recipe(key).get("steps", ())]


def resolve(argv) -> list[str]:
    """Une étape, programme résolu : prête pour subprocess (jamais de shell)."""
    exe = program(argv[0])
    if exe is None:
        raise MarketplaceError(409, f"{argv[0]} est requis : installez-le, puis réessayez.")
    return [exe, *argv[1:]]


def commands(key: str) -> list[list[str]]:
    """Toutes les étapes résolues tout de suite (programmes déjà présents)."""
    return [resolve(argv) for argv in plan(key)]


def _undo(argv) -> tuple[str, ...] | None | bool:
    """L'étape qui défait `argv` ; False : rien à défaire ; None : ne sait pas se défaire sans risque."""
    head, rest = argv[0], list(argv[1:])
    if head == "npm" and rest[:2] == ["install", "--global"]:
        return ("npm", "uninstall", "--global", rest[-1].rsplit("@", 1)[0])
    if head == "uv" and rest[:2] == ["tool", "install"]:
        return ("uv", "tool", "uninstall", rest[-1].split("==")[0].split("[")[0])
    if head == "code" and rest[:1] == ["--install-extension"]:
        return ("code", "--uninstall-extension", rest[1])
    if head == "winget" and rest[:1] == ["install"]:
        return (
            "winget",
            "uninstall",
            "--id",
            rest[rest.index("--id") + 1],
            "--exact",
            "--silent",
            "--disable-interactivity",
        )
    if head == "claude" and rest[:2] == ["plugin", "install"]:
        return ("claude", "plugin", "uninstall", rest[2])
    if head == "claude" and rest[:3] == ["plugin", "marketplace", "add"]:
        return False  # le catalogue d'extensions reste déclaré : il n'installe rien à lui seul
    if head == "claude" and rest[:2] == ["mcp", "add"]:
        name = next(a for a in rest[2:] if not a.startswith("-") and a != "user")
        return (
            ("claude", "mcp", "remove", "--scope", "user", name)
            if "user" in rest
            else ("claude", "mcp", "remove", name)
        )
    if head == PYTHON and rest[:1] == [LIBRECHAT_SETUP]:
        return False  # le dossier (réglages, conversations) est gardé : l'arrêt des conteneurs suffit
    if head == "docker" and rest[:1] == ["compose"] and rest[-2:] == ["up", "--detach"]:
        return ("docker", *rest[:-2], "down")  # volumes (données) gardés
    if head == "docker" and rest[:1] == ["run"]:
        return ("docker", "rm", "--force", rest[rest.index("--name") + 1])  # le volume (données) est gardé
    return None  # pip (paquets partagés avec Lezard IA), installateurs Windows, scripts d'installation


def uninstall_steps(key: str) -> list[tuple[str, ...]] | None:
    """Les étapes qui défont la recette, dans l'ordre inverse ; None si une seule ne sait pas se défaire."""
    out = []
    for argv in reversed(recipe(key).get("steps", ())):
        undo = _undo(argv)
        if undo is None:
            return None
        if undo:
            out.append(undo)
    return out or None


def uninstall_hint(key: str) -> str | None:
    """Pour ce qui ne se désinstalle pas depuis la page : comment le faire soi-même."""
    steps = recipe(key).get("steps", ())
    if any(argv[:2] == (PYTHON, SETUP) for argv in steps):
        if any(a.endswith(".zip") for argv in steps for a in argv):
            return "Supprimez son dossier dans %LOCALAPPDATA%\\Programs."
        return "Désinstallez-le depuis Paramètres > Applications de Windows."
    return None


def label(argv) -> str:
    """Une étape telle qu'affichée (sans chemin de ce PC)."""
    head = "python" if argv[0] == PYTHON or Path(argv[0]).stem.lower().startswith("python") else Path(argv[0]).stem
    return " ".join([head, *(Path(a).name if os.path.isabs(a) else a for a in argv[1:])])


def display(key: str) -> list[str]:
    """Les étapes telles qu'affichées dans la page (sans chemin de ce PC)."""
    if "show" in recipe(key):
        return list(recipe(key)["show"])
    return [" ".join("python" if a == PYTHON else a for a in argv) for argv in recipe(key).get("steps", ())]


def set_env(env_file: Path, name: str, value: str) -> None:
    """Remplace (ou ajoute) NOM=valeur dans .env, le reste gardé tel quel ; et pour ce processus, tout de suite."""
    lines = env_file.read_text(encoding="utf-8-sig").splitlines() if env_file.exists() else []
    found = False
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == name:
            lines[i], found = f"{name}={value}", True
    if not found:
        lines.append(f"{name}={value}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[name] = value
