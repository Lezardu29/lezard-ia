"""Outils de la marketplace dans le chat : « /<commande> » pour chaque outil installé sur ce PC (table
marketplace.CHAT). La clé maître seulement : ces outils agissent sur ce PC (à distance : double authentification).

- agent : une demande en langage courant, l'outil répond dans la conversation. OpenClaw, Aider, OpenCode, Qwen Code
  sont branchés sur Lezard IA sans réglage (adresse 127.0.0.1, modèle choisi dans le chat, clé de l'appelant donnée à
  la tâche seulement, jamais écrite) ; les extensions de Claude Code passent par Claude Code de ce PC (son compte).
  Chaque conversation a son dossier de travail par outil (data/outils/<outil>/<session>) : l'outil s'y souvient de
  la conversation.
- cli : le programme, avec les arguments tapés, comme dans un terminal ; sa sortie s'affiche dans la conversation.
- serve : le service est démarré sur ce PC, son adresse est donnée. open : l'application s'ouvre sur ce PC.

Jamais d'interpréteur de commandes : le programme est lancé directement (les lanceurs .cmd de npm sont lus pour
lancer Node avec leur script), donc aucun caractère de la demande n'est interprété. Une demande est une tâche de fond,
suivie par la page (une réponse locale peut prendre des minutes, plus que ce qu'attend l'accès distant)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from . import detection, installers, marketplace, openclaw

AGENT_TIMEOUT = 600
CLI_TIMEOUT = 300
KEEP = 3600  # tâches terminées gardées une heure (la page relit le résultat)
MAX_RUNNING = 3
MAX_MESSAGE = 32_000
MAX_OUTPUT = 100_000  # caractères de sortie affichés au plus
SESSION = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MODEL = re.compile(r"^[\w.:/@+-]{1,200}$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
SHARED_PROGRAMS = {"npx"}  # programmes de plusieurs outils : présents, ils ne disent pas que l'outil est installé
PROGRAMS = {"openclaw": "openclaw", "aider": "aider", "opencode": "opencode", "qwen": "qwen", "claude": "claude"}
# variables qui feraient payer Claude Code par la clé d'API de Lezard IA (.env) au lieu du compte de ce PC
CLAUDE_HIDDEN = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDECODE")
# lignes d'en-tête d'Aider, sans intérêt dans une réponse
AIDER_NOISE = (
    "Aider v",
    "Model:",
    "Main model:",
    "Weak model:",
    "Git repo:",
    "Repo-map:",
    "Restored previous",
    "Warning",
)
INSTALLED_SETTING = "marketplace_installed"


class ChatToolError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def home(tool: str) -> Path:
    return Path(os.getenv("GATEWAY_DATA_DIR", "data")) / "outils" / tool


# ---------- programmes : lancés sans interpréteur de commandes ----------
_SET = re.compile(r'^\s*SET\s+"?(\w+)=([^"\r\n]*)"?\s*$', re.IGNORECASE | re.MULTILINE)


def _from_shim(path: Path) -> list[str] | None:
    """Lanceur .cmd de npm → ce qu'il lance vraiment : [node, script] (le script écrit en clair ou rangé dans une
    variable, comme dans npx.cmd) ou [programme.exe] (opencode.cmd). None si le lanceur n'a pas cette forme : il
    n'est jamais exécuté par cmd.exe."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    folder = str(path.parent)
    values: dict[str, list[str]] = {}
    for k, v in _SET.findall(text):  # une variable peut être posée plusieurs fois (npx.cmd) : chaque valeur essayée
        values.setdefault(k.upper(), []).append(v)

    def expand(s: str, depth: int = 0) -> list[str]:
        s = s.replace("%~dp0", folder + "\\").replace("%dp0%", folder + "\\")
        m = re.search(r"%(\w+)%", s)
        if not m or depth > 3:
            return [s]
        out = []
        for v in values.get(m.group(1).upper(), []):
            out += expand(s[: m.start()] + v + s[m.end() :], depth + 1)
        return out

    for line in text.splitlines():
        if "%*" not in line:
            continue
        for token in re.findall(r'"([^"]+)"', line):
            for target in map(os.path.normpath, expand(token)):
                if not os.path.isfile(target):
                    continue
                if target.lower().endswith(".exe"):
                    if Path(target).name.lower() == "node.exe":
                        continue  # Node lui-même : le script vient ensuite sur la ligne
                    return [target]
                if target.lower().endswith((".js", ".mjs", ".cjs")):
                    local = path.parent / "node.exe"
                    node = str(local) if local.exists() else shutil.which("node")
                    return [node, target] if node else None
    return None


def program(name: str) -> list[str] | None:
    found = installers.program(name)
    if not found:
        return None
    if sys.platform == "win32":
        p = Path(found)
        if p.suffix.lower() == ".ps1" and p.with_suffix(".cmd").exists():
            p = p.with_suffix(".cmd")
        if p.suffix.lower() in (".cmd", ".bat"):
            return _from_shim(p)
    return [found]


# ---------- quels outils sont là ----------
def _noted(store) -> set[str]:
    return set((store.get_setting(INSTALLED_SETTING) or {}) if store else ())


def available(store=None) -> list[dict]:
    """Outils installés sur ce PC qui ont une commande dans le chat, pour le menu « / »."""
    found = detection.scan()
    noted = _noted(store)
    entries = {e["id"]: e for e in (*marketplace.APPS, *marketplace.AGENT_TOOLS, *marketplace.COMPONENTS)}
    out = []
    for eid, spec in marketplace.CHAT.items():
        key = next((k for k in installers.RECIPES if k.split(":", 1)[1] == eid), None)
        seen = found.get(key) if key else None
        if spec["kind"] == "agent" and spec["runner"] == "claude":
            here = (seen if seen is not None else key in noted) and program("claude") is not None
        elif spec["kind"] in ("agent", "cli", "serve"):
            name = PROGRAMS.get(spec.get("runner"), spec.get("program"))
            here = program(name) is not None
            if name in SHARED_PROGRAMS:  # npx est là dès que Node.js l'est : l'outil lui-même doit être installé
                here = here and (seen if seen is not None else key in noted)
        else:
            here = seen if seen is not None else key in noted
        if here:
            out.append({"id": eid, "name": entries[eid]["name"], **marketplace.chat_usage(eid)})
    return out


# ---------- une demande ----------
def _clean(text: str) -> str:
    text = ANSI.sub("", text).replace("\r\n", "\n")
    text = "\n".join(line.split("\r")[-1] for line in text.split("\n"))  # barres de progression
    return text.strip()


def _cut(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + "\n… (sortie coupée)"


def _block(text: str) -> str:
    fence = "````" if "```" in text else "```"
    return f"{fence}text\n{text}\n{fence}"


def _windows_open(spec: dict) -> str:
    """Ouvre l'application sur ce PC ; renvoie ce qui a été ouvert."""
    if sys.platform != "win32":
        raise ChatToolError(501, "Ouvrir une application depuis le chat : sur Windows seulement.")
    if spec.get("vscode"):
        code = installers.program("code")
        exe = Path(code).parent.parent / "Code.exe" if code else None
        if not exe or not exe.exists():
            raise ChatToolError(404, "VS Code introuvable sur ce PC.")
        subprocess.Popen([str(exe)], creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
        return "VS Code"
    path = os.path.expandvars(spec.get("path") or "")
    if path and os.path.isfile(path):
        os.startfile(path)  # noqa: S606 - chemin fixe de la table, jamais tapé
        return Path(path).stem
    name = (spec.get("start") or "").lower()
    if name:
        menus = [
            Path(os.environ.get(v, "")) / "Microsoft" / "Windows" / "Start Menu" for v in ("APPDATA", "PROGRAMDATA")
        ]
        for menu in menus:
            for lnk in sorted(menu.rglob("*.lnk")) if menu.is_dir() else ():
                if lnk.stem.lower() == name or lnk.stem.lower().startswith(name + " "):
                    os.startfile(str(lnk))  # noqa: S606 - raccourci du menu Démarrer trouvé par son nom
                    return lnk.stem
    raise ChatToolError(404, f"« {spec.get('start') or path} » introuvable dans le menu Démarrer de ce PC.")


class Runs:
    """Tâches en cours ou terminées, propres à chaque compte (identité de l'appelant)."""

    def __init__(self):
        self.runs: dict[str, dict] = {}
        self.services: dict[str, subprocess.Popen] = {}

    def _prune(self) -> None:
        now = time.time()
        for rid in [r for r, v in self.runs.items() if v.get("ended") and now - v["ended"] > KEEP]:
            self.runs.pop(rid, None)

    def get(self, rid: str, owner: str) -> dict:
        run = self.runs.get(rid)
        if not run or run["owner"] != owner:
            raise ChatToolError(404, "Tâche introuvable (terminée depuis plus d'une heure ?).")
        return run

    def stop(self, rid: str, owner: str) -> dict:
        run = self.get(rid, owner)
        if run["status"] == "running" and run.get("task"):
            run["task"].cancel()
        return run

    def start(
        self,
        *,
        tool: str,
        present: list[dict],
        owner: str,
        key: str,
        message: str,
        session: str,
        model: str | None,
        port: int,
    ) -> dict:
        """`present` : available(), lu hors de la boucle (la détection lance des programmes)."""
        self._prune()
        spec = marketplace.CHAT.get(tool)
        if not spec:
            raise ChatToolError(404, f"Pas de commande du chat pour « {tool} ».")
        if not any(a["id"] == tool for a in present):
            raise ChatToolError(409, "Cet outil n'est pas installé sur ce PC : installez-le depuis la marketplace.")
        message = (message or "").strip()
        name = marketplace.chat_command(tool)
        if spec["kind"] == "agent" and not message:
            raise ChatToolError(400, f"Indiquez la demande après la commande, ex. /{name} {spec.get('example', '')}")
        if len(message) > MAX_MESSAGE:
            raise ChatToolError(413, f"Demande trop longue ({MAX_MESSAGE} caractères au plus).")
        if not SESSION.match(session or ""):
            raise ChatToolError(400, "'session' : identifiant de conversation invalide.")
        if model and (not MODEL.match(model) or model == "auto"):
            model = None
        if sum(1 for r in self.runs.values() if r["status"] == "running") >= MAX_RUNNING:
            raise ChatToolError(429, "Trois outils travaillent déjà : attendez qu'un se termine.")
        rid = secrets.token_hex(8)
        run = {"id": rid, "owner": owner, "tool": tool, "status": "running", "started": time.time(), "ended": None}
        run.update(text="", error=None, model=None)
        self.runs[rid] = run
        job = dict(spec=spec, key=key, message=message, session=session, model=model, port=port)
        run["task"] = asyncio.get_running_loop().create_task(self._run(run, **job))
        return run

    async def _run(self, run: dict, *, spec: dict, **job) -> None:
        try:
            kind = spec["kind"]
            if kind == "open":
                opened = await asyncio.to_thread(self._open, spec)
                run.update(status="done", text=opened)
            elif kind == "serve":
                run.update(status="done", text=await asyncio.to_thread(self._serve, run["tool"], spec))
            elif kind == "cli":
                await self._cli(run, spec, job["message"])
            else:
                await self._agent(run, spec, **job)
        except asyncio.CancelledError:
            run.update(status="stopped", error="Demande arrêtée.")
        except ChatToolError as e:
            run.update(status="error", error=e.message)
        except OSError as e:
            run.update(status="error", error=f"Impossible de lancer l'outil : {e}")
        finally:
            run["ended"] = time.time()

    # -- applications et services --
    def _open(self, spec: dict) -> str:
        if spec.get("docker"):  # conteneur arrêté (redémarrage du PC) : relancé avant d'ouvrir la page
            docker = installers.program("docker")
            if docker:
                subprocess.run([docker, "start", spec["docker"]], capture_output=True, timeout=60, check=False)
        if spec.get("url"):
            webbrowser.open(spec["url"])
            return f"Ouvert dans le navigateur de ce PC : {spec['url']} (adresse valable sur ce PC seulement)."
        return f"{_windows_open(spec)} est ouvert sur ce PC."

    def _serve(self, tool: str, spec: dict) -> str:
        proc = self.services.get(tool)
        if not proc or proc.poll() is not None:
            argv = program(spec["program"])
            if not argv:
                raise ChatToolError(409, "Programme introuvable sur ce PC.")
            work = home(tool)
            work.mkdir(parents=True, exist_ok=True)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.services[tool] = subprocess.Popen(
                argv,
                cwd=work,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            return f"Démarré sur ce PC : {spec['url']} (adresse valable sur ce PC seulement)."
        return f"Déjà démarré sur ce PC : {spec['url']}."

    # -- programmes en ligne de commande --
    async def _cli(self, run: dict, spec: dict, message: str) -> None:
        argv = program(spec["program"])
        if not argv:
            raise ChatToolError(409, "Programme introuvable sur ce PC.")
        try:
            words = [
                w[1:-1] if len(w) > 1 and w[0] == w[-1] and w[0] in "\"'" else w
                for w in shlex.split(message, posix=False)
            ]
        except ValueError:
            raise ChatToolError(400, "Guillemet non fermé dans les arguments.")
        work = home(run["tool"])
        work.mkdir(parents=True, exist_ok=True)
        code, out = await self._exec([*argv, *spec.get("args", ()), *words], work, tool_env(), CLI_TIMEOUT, run)
        text = _cut(_clean(out)) or "(aucune sortie)"
        run.update(status="done", text=_block(text) + (f"\n\nCode de sortie : {code}." if code else ""))

    # -- agents --
    async def _agent(self, run: dict, spec: dict, *, key, message, session, model, port) -> None:
        runner = spec["runner"]
        argv = program(PROGRAMS[runner])
        if not argv:
            raise ChatToolError(409, "Programme introuvable sur ce PC.")
        work = home(run["tool"]) / session
        work.mkdir(parents=True, exist_ok=True)
        again = (work / ".lezard-session").exists()  # échange précédent dans cette conversation : il s'en souvient
        base = f"http://127.0.0.1:{port}/v1"
        env = {**tool_env(), "LEZARD_API_KEY": key}
        m = model or "auto"
        if runner == "openclaw":
            config = openclaw.write_config(port, model)
            env.update(
                OPENCLAW_CONFIG_PATH=str(config.resolve()),
                OPENCLAW_STATE_DIR=str((openclaw.home() / "etat").resolve()),
            )
            ask = work / "demande.txt"
            ask.write_text(message, encoding="utf-8")
            args = ["agent", "--local", "--json", "--session-id", f"lezard-{session}", "--timeout", str(AGENT_TIMEOUT)]
            args += ["--message-file", str(ask.resolve())] + (["--model", f"lezard/{model}"] if model else [])
        elif runner == "aider":
            env.update(OPENAI_API_BASE=base, OPENAI_API_KEY=key)
            ask = work / "demande.txt"
            ask.write_text(message, encoding="utf-8")
            args = ["--model", f"openai/{m}", "--message-file", str(ask.resolve()), "--yes-always", "--no-git"]
            args += ["--no-auto-commits", "--no-pretty", "--no-stream", "--no-check-update", "--analytics-disable"]
            args += ["--no-show-model-warnings", "--restore-chat-history", "--no-gitignore", "--no-fancy-input"]
            args += ["--no-show-release-notes"]  # sinon « oui » d'office ouvrirait les nouveautés dans le navigateur
        elif runner == "opencode":
            config = home("opencode") / "opencode.json"
            config.write_text(json.dumps(_opencode_config(base, model)), encoding="utf-8")
            env["OPENCODE_CONFIG"] = str(config.resolve())
            args = ["run", "--model", f"lezard/{m}", "--dir", str(work.resolve())]
            args += (["--continue"] if again else []) + ["--", message]
        elif runner == "qwen":
            env.update(OPENAI_API_KEY=key, OPENAI_BASE_URL=base, OPENAI_MODEL=m)
            args = ["--auth-type", "openai", "--openai-base-url", base, "--model", m, "--output-format", "text"]
            args += (["--continue"] if again else []) + [f"--prompt={message}"]  # « -- » n'est pas compris
        else:  # claude : Claude Code de ce PC, avec son compte (jamais la clé d'API de Lezard IA)
            for name in CLAUDE_HIDDEN:
                env.pop(name, None)
            env.pop("LEZARD_API_KEY", None)
            args = [
                "-p",
                spec.get("prefix", "") + message,
                "--output-format",
                "text",
                "--permission-mode",
                "acceptEdits",
            ]
            args += ["--continue"] if again else []
        code, out = await self._exec([*argv, *args], work, env, AGENT_TIMEOUT, run)
        if runner == "openclaw":
            text, error, used = openclaw.answer_of(out)
            run["model"] = used
        else:
            text = _clean(out)
            if runner == "aider":  # sa réponse, telle qu'il l'écrit dans l'historique de la conversation
                text = (
                    aider_answer(work / ".aider.chat.history.md")
                    or "\n".join(line for line in text.split("\n") if not line.startswith(AIDER_NOISE)).strip()
                )
            error = None if code == 0 or text else f"L'outil s'est arrêté (code {code})."
            run["model"] = None if runner == "claude" else m
        if text:
            (work / ".lezard-session").touch()
            run.update(status="done", text=_cut(text))
        else:
            raise ChatToolError(502, error or run.pop("stderr", "") or "L'outil n'a rien répondu.")

    async def _exec(self, argv, cwd: Path, env, limit: float, run: dict) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), limit)
        except TimeoutError:
            _kill(proc.pid)
            raise ChatToolError(504, f"L'outil n'a pas terminé en {int(limit // 60)} minutes.")
        except asyncio.CancelledError:
            _kill(proc.pid)
            raise
        stdout, stderr = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        tail = " ".join(_clean(stderr).splitlines()[-3:])
        if run is not None and tail:
            run["stderr"] = tail
        if marketplace.CHAT.get(run["tool"], {}).get("kind") == "cli":
            return proc.returncode, stdout + ("\n" + stderr if stderr.strip() else "")
        return proc.returncode, stdout


def aider_answer(history: Path) -> str:
    """Dernière réponse d'Aider : après la dernière demande (« #### … ») de son historique, sans ses lignes de
    service (« > … » : jetons, avertissements)."""
    try:
        lines = history.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    asked = max((i for i, line in enumerate(lines) if line.startswith("#### ")), default=None)
    if asked is None:
        return ""
    out = [line for line in lines[asked + 1 :] if not line.startswith(("#### ", "> ", "# aider chat started"))]
    return "\n".join(out).strip()


def tool_env() -> dict:
    """Variables d'un outil lancé : sortie en UTF-8 (sous Windows, Python écrirait sinon dans l'ancien jeu de
    caractères et les accents arriveraient abîmés), sans couleurs."""
    return {**os.environ, "NO_COLOR": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def _opencode_config(base: str, model: str | None) -> dict:
    models = {"auto": {"name": "Lezard IA (auto)"}, "auto/economy": {"name": "Lezard IA (éco)"}}
    if model:
        models[model] = {"name": model}
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "lezard": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Lezard IA",
                "options": {"baseURL": base, "apiKey": "{env:LEZARD_API_KEY}"},
                "models": models,
            }
        },
        "model": f"lezard/{model or 'auto'}",
        "autoupdate": False,
        "share": "disabled",
    }


def _kill(pid: int) -> None:
    """Arrête l'outil et ce qu'il a lancé."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def public(run: dict) -> dict:
    ended = run.get("ended")
    return {
        "id": run["id"],
        "tool": run["tool"],
        "status": run["status"],
        "text": run.get("text") or "",
        "error": run.get("error"),
        "model": run.get("model"),
        "seconds": round((ended or time.time()) - run["started"]),
    }
