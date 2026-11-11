"""Outils et applications du catalogue réellement présents sur ce PC, quelle que soit la façon dont ils ont été installés.

Lecture seule : rien n'est installé, lancé en tâche de fond ni modifié ; seuls des noms sortent d'ici, jamais le contenu
des fichiers lus (~/.claude.json contient des secrets). La recette de chaque élément (installers.py) dit où regarder.

Un élément vaut True si chaque étape de sa recette a laissé sa trace, False si l'une manque, None si on ne peut pas le
savoir (Docker arrêté, extension de Firefox, fichier illisible) : la page se fie alors à ce qu'elle a noté elle-même.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from functools import cached_property
from pathlib import Path

from . import installers

TTL = 30  # secondes : la page se recharge souvent, et npm met un moment à répondre
RUN_TIMEOUT = 15
# nom affiché dans « Paramètres > Applications » de Windows, pour ce que winget ou un installateur y dépose
WINDOWS_NAMES = {
    "tool:orca": "orca",
    "app:anythingllm": "anythingllm",
    "app:jan": "jan",
    "app:meetily": "meetily",
    "app:cherrystudio": "cherry studio",
    "app:openhuman": "openhuman",
    "app:chatbox": "chatbox",
    "app:zed": "zed",
    "app:cursor": "cursor",
    "app:comfyui": "comfyui",
}
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
BROWSERS = (r"Google\Chrome", r"Microsoft\Edge", r"BraveSoftware\Brave-Browser")

_lock = threading.Lock()
_cache: tuple[float, dict[str, bool | None]] | None = None


def _run(*argv: str) -> tuple[int, str] | None:
    """Code de retour et sortie d'un programme ; None s'il ne répond pas."""
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=RUN_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode, done.stdout


def _read_json(path: Path) -> dict | list | bool | None:
    """Le contenu du fichier ; False s'il n'existe pas ; None s'il est illisible."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None


def _within(names: set[str] | None, name: str) -> bool | None:
    return None if names is None else name in names


class Facts:
    """Ce que ce PC dit de lui-même : chaque source n'est lue qu'une fois, et seulement si une recette en a besoin."""

    @property
    def _claude_dir(self) -> Path:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")

    def _tool_dir(self, *argv: str) -> Path | bool | None:
        """Dossier annoncé par un programme ; False s'il n'est pas là ; None s'il ne répond pas."""
        exe = installers.program(argv[0])
        if not exe:
            return False
        out = _run(exe, *argv[1:])
        return Path(out[1].strip()) if out and out[0] == 0 and out[1].strip() else None

    @cached_property
    def npm_root(self) -> Path | bool | None:
        return self._tool_dir("npm", "root", "--global")

    @cached_property
    def uv_dir(self) -> Path | bool | None:
        return self._tool_dir("uv", "tool", "dir")

    def _in_dir(self, base: Path | bool | None, name: str) -> bool | None:
        return base if not isinstance(base, Path) else (base / name).is_dir()

    def npm(self, package: str) -> bool | None:
        return self._in_dir(self.npm_root, package)

    def uv(self, package: str) -> bool | None:
        return self._in_dir(self.uv_dir, package.lower().replace("_", "-").replace(".", "-"))

    @cached_property
    def plugins(self) -> set[str] | None:
        found = _read_json(self._claude_dir / "plugins" / "installed_plugins.json")
        if found is False:
            return set()
        return set(found.get("plugins") or {}) if isinstance(found, dict) else None

    @cached_property
    def claude_mcp(self) -> set[str] | None:
        folder = os.environ.get("CLAUDE_CONFIG_DIR")
        found = _read_json(Path(folder) / ".claude.json" if folder else Path.home() / ".claude.json")
        if found is False:
            return set()
        return set(found.get("mcpServers") or {}) if isinstance(found, dict) else None

    def skill(self, name: str) -> bool:
        return (self._claude_dir / "skills" / name).exists()

    @cached_property
    def vscode(self) -> set[str] | None:
        found = _read_json(Path.home() / ".vscode" / "extensions" / "extensions.json")
        if found is False:
            return set()
        if not isinstance(found, list):
            return None
        return {str(e.get("identifier", {}).get("id", "")).lower() for e in found if isinstance(e, dict)}

    @cached_property
    def windows_apps(self) -> set[str] | None:
        """Noms de « Paramètres > Applications », en minuscules ; None hors de Windows."""
        try:
            import winreg
        except ImportError:
            return None
        names = set()
        for root, sub in (
            (winreg.HKEY_CURRENT_USER, UNINSTALL_KEY),
            (winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY),
            (winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY.replace("Software", r"Software\WOW6432Node", 1)),
        ):
            try:
                with winreg.OpenKey(root, sub) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, i)) as app:
                                names.add(str(winreg.QueryValueEx(app, "DisplayName")[0]).lower())
                        except OSError:
                            pass  # entrée sans nom : pas une application affichée
            except OSError:
                pass
        return names

    def windows_app(self, name: str) -> bool | None:
        apps = self.windows_apps
        return None if apps is None else any(a == name or a.startswith(name + " ") for a in apps)

    def local_program_folder(self, name: str) -> bool | None:
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) / "Programs" / name).is_dir() if base else None

    def browser_extension(self, ident: str) -> bool | None:
        """Trouvée dans Chrome, Edge ou Brave ; sinon inconnu : elle est peut-être dans un autre navigateur."""
        base = os.environ.get("LOCALAPPDATA")
        if base and any(any((Path(base) / b / "User Data").glob(f"*/Extensions/{ident}")) for b in BROWSERS):
            return True
        return None

    @cached_property
    def _docker(self) -> tuple[set[str], set[str]] | None:
        """Conteneurs existants, même arrêtés : leurs noms, leurs projets compose ; None si Docker ne répond pas."""
        exe = installers.program("docker")
        if not exe:
            return set(), set()
        out = _run(exe, "ps", "--all", "--format", '{{.Names}}\t{{.Label "com.docker.compose.project"}}')
        if not out or out[0]:
            return None
        rows = [line.split("\t") for line in out[1].splitlines() if line.strip()]
        return {r[0] for r in rows}, {r[1] for r in rows if len(r) > 1 and r[1]}

    def container(self, name: str) -> bool | None:
        return None if self._docker is None else name in self._docker[0]

    def compose_project(self, name: str) -> bool | None:
        return None if self._docker is None else name in self._docker[1]

    def user_package(self, package: str) -> bool | None:
        exe = installers.program(installers.USER_PYTHON[0])
        if not exe:
            return False
        code = "import importlib.metadata as m, sys; m.version(sys.argv[1])"
        out = _run(exe, *installers.USER_PYTHON[1:], "-c", code, package)
        return None if out is None else out[0] == 0


def _step(key: str, argv: tuple[str, ...], facts: Facts) -> bool | None:
    """La trace laissée par une étape de recette (True aussi pour une étape qui n'en laisse aucune à elle seule)."""
    head, rest = argv[0], list(argv[1:])
    if head == "npm" and rest[:2] == ["install", "--global"]:
        return facts.npm(rest[-1].rsplit("@", 1)[0])
    if head == "uv" and rest[:2] == ["tool", "install"]:
        return facts.uv(rest[-1].split("==")[0].split("[")[0])
    if head == "code" and rest[:1] == ["--install-extension"]:
        return _within(facts.vscode, rest[1].lower())
    if head == "winget":
        return facts.windows_app(WINDOWS_NAMES[key])
    if head == "claude" and rest[:2] == ["plugin", "install"]:
        return _within(facts.plugins, rest[2])
    if head == "claude" and rest[:2] == ["mcp", "add"]:
        return _within(facts.claude_mcp, next(a for a in rest[2:] if not a.startswith("-") and a != "user"))
    if head == "claude" and rest[:2] == ["plugin", "marketplace"]:
        return True  # un catalogue d'extensions n'installe rien à lui seul
    if head == "codegraph":
        return True  # le branchement sur les agents : la commande vient de l'étape npm
    if head == "npx" and rest[1].startswith("rea-agents@"):
        return _within(facts.claude_mcp, "rea")
    if head == "npx" and rest[1].startswith("claude-mem@"):  # ses données restent après une désinstallation
        plugins = facts.plugins
        return None if plugins is None else any(p.startswith("claude-mem@") for p in plugins)
    if head == "npx" and rest[1].startswith("skills@"):
        return facts.skill(rest[rest.index("--skill") + 1])
    if head == installers.PYTHON and rest[:1] == [installers.SETUP]:
        if any(a.endswith(".zip") for a in rest):
            return facts.local_program_folder(rest[-1])
        return facts.windows_app(WINDOWS_NAMES[key])
    if head == installers.PYTHON and rest[:2] == [installers.ORCHESTRE, "--skill"]:
        return facts.skill("orchestre")
    if head == installers.PYTHON and rest[:1] == [installers.LIBRECHAT_SETUP]:
        root = Path.home() / rest[-1]  # construit (le dossier reste après une désinstallation : conversations)
        return (root / ".lezard-build").is_file() and (root / "mongodb" / "mongod.exe").is_file()
    if head == installers.PYTHON and rest[:2] == ["-m", "webbrowser"]:
        return facts.browser_extension(rest[-1].rsplit("/", 1)[-1])
    if head == "docker" and rest[:1] == ["compose"]:
        return facts.compose_project(rest[rest.index("--project-directory") + 1].lower())
    if head == "docker" and rest[:1] == ["run"]:
        return facts.container(rest[rest.index("--name") + 1])
    if argv[: len(installers.USER_PYTHON)] == installers.USER_PYTHON:
        return facts.user_package(rest[-1].split("<")[0])
    return None


def state(key: str, facts: Facts) -> bool | None:
    """True : présent ; False : absent ou incomplet ; None : impossible à savoir."""
    steps = [_step(key, argv, facts) for argv in installers.recipe(key).get("steps", ())]
    if False in steps:
        return False
    return None if None in steps or not steps else True


def forget() -> None:
    """Une installation vient de se terminer : relire le PC au prochain affichage."""
    global _cache
    with _lock:
        _cache = None


def scan() -> dict[str, bool | None]:
    """L'état de chaque outil et application du catalogue. Appel bloquant : à lancer hors de la boucle asyncio."""
    global _cache
    with _lock:
        if _cache is None or time.monotonic() - _cache[0] > TTL:
            facts = Facts()
            _cache = (
                time.monotonic(),
                {k: state(k, facts) for k in installers.RECIPES if k.startswith(("tool:", "app:"))},
            )
        return _cache[1]
