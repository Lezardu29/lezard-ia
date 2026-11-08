"""Outils de la marketplace dans le chat : « /<commande> » pour chaque outil installé sur ce PC (table
marketplace.CHAT). La clé maître seulement : ces outils agissent sur ce PC (à distance : double authentification).

- agent : une demande en langage courant, l'outil répond dans la conversation. OpenClaw, Aider, OpenCode, Qwen Code
  sont branchés sur Lezard IA sans réglage (adresse 127.0.0.1, modèle choisi dans le chat, clé de l'appelant donnée à
  la tâche seulement, jamais écrite). Les extensions de Claude Code (Superpowers…) passent par Claude Code de ce PC,
  branché lui aussi sur Lezard IA : jamais par le compte claude.ai de ce PC.
  Chaque conversation a son dossier de travail par outil (data/outils/<outil>/<session>) : l'outil s'y souvient de
  la conversation.
- cli : le programme, avec les arguments tapés, comme dans un terminal ; sa sortie s'affiche dans la conversation.
- serve : le service est démarré sur ce PC, son adresse est donnée. open : l'application s'ouvre sur ce PC.

Jamais d'interpréteur de commandes : le programme est lancé directement (les lanceurs .cmd de npm sont lus pour
lancer Node avec leur script), donc aucun caractère de la demande n'est interprété. Une demande est une tâche de fond,
suivie par la page (une réponse locale peut prendre des minutes, plus que ce qu'attend l'accès distant)."""

from __future__ import annotations

import asyncio
import base64
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

import httpx

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
# variables de Claude Code venues de l'environnement de Lezard IA (clé d'API d'Anthropic du .env…) : retirées, Claude
# Code ne parle qu'à Lezard IA (claude_env)
# outils de Claude Code proposés au modèle : lire, écrire, chercher, et Skill (les extensions comme Superpowers s'en
# servent). Sans les autres (agents, tâches planifiées, serveurs MCP…), sa demande passe d'environ 40 000 à 7 000
# tokens : elle tient dans la fenêtre d'un modèle local, qui sinon la lirait par morceaux et perdrait le fil.
CLAUDE_TOOLS = "Read,Write,Edit,Glob,Grep,Skill"
CLAUDE_FULL = "Read,Write,Edit,Glob,Grep,Skill,Agent"  # avec Claude : aussi les sous-agents (grande fenêtre)
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


def _shell_open(path: str) -> None:
    """Ouvert par l'Explorateur de Windows, avec SON environnement : une variable héritée par Lezard IA (lancé
    depuis un terminal de VS Code : ELECTRON_RUN_AS_NODE=1) ferait quitter aussitôt les applications Electron
    (Orca, Jan, Cherry Studio…)."""
    subprocess.Popen(["explorer.exe", path], close_fds=True)  # noqa: S603, S607 - chemin trouvé par Lezard IA


def _windows_open(spec: dict) -> str:
    """Ouvre l'application sur ce PC ; renvoie ce qui a été ouvert."""
    if sys.platform != "win32":
        raise ChatToolError(501, "Ouvrir une application depuis le chat : sur Windows seulement.")
    if spec.get("vscode"):
        code = installers.program("code")
        exe = Path(code).parent.parent / "Code.exe" if code else None
        if not exe or not exe.exists():
            raise ChatToolError(404, "VS Code introuvable sur ce PC.")
        _shell_open(str(exe))
        return "VS Code"
    path = os.path.expandvars(spec.get("path") or "")
    if path and os.path.isfile(path):
        _shell_open(path)  # chemin fixe de la table, jamais tapé
        return Path(path).stem
    name = (spec.get("start") or "").lower()
    if name:
        menus = [
            Path(os.environ.get(v, "")) / "Microsoft" / "Windows" / "Start Menu" for v in ("APPDATA", "PROGRAMDATA")
        ]
        for menu in menus:
            for lnk in sorted(menu.rglob("*.lnk")) if menu.is_dir() else ():
                if lnk.stem.lower() == name or lnk.stem.lower().startswith(name + " "):
                    _shell_open(str(lnk))  # raccourci du menu Démarrer trouvé par son nom
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
        claude_model: str | None = None,
    ) -> dict:
        """`present` : available(), lu hors de la boucle (la détection lance des programmes). `claude_model` : un
        modèle Claude de Lezard IA (clé d'API d'Anthropic enregistrée), pour les extensions de Claude Code quand
        aucun modèle n'est choisi dans le chat."""
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
        job["claude_model"] = claude_model
        run["task"] = asyncio.get_running_loop().create_task(self._run(run, **job))
        return run

    async def _run(self, run: dict, *, spec: dict, **job) -> None:
        try:
            kind = spec["kind"]
            if kind == "open":
                opened = await asyncio.to_thread(self._open, spec)
                run.update(status="done", text=opened)
            elif kind == "serve":
                run.update(status="done", text=await asyncio.to_thread(self._serve, run["tool"], spec, job))
            elif kind == "cli":
                await self._cli(run, spec, job["message"])
            else:
                if spec.get("watch"):  # Watch : vidéo préparée par Lezard IA, son rapport donné à l'IA
                    job = {**job, "message": await self._watched(run, job)}
                if note := await self._prepared(spec, job):
                    job = {**job, "message": f"{job['message']}\n\n{note}"}
                await self._agent(run, spec, **job)
                await self._checked(run, spec, job)
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

    def _serve(self, tool: str, spec: dict, job: dict | None = None) -> str:
        """Service local (interface web). « claude » : il lance Claude Code (CloudCLI), branché comme les extensions
        sur Lezard IA avec la clé de l'appelant, jamais sur le compte Claude de ce PC."""
        if spec.get("detached"):
            return self._detached(tool, spec)
        proc = self.services.get(tool)
        if not proc or proc.poll() is not None:
            argv = program(spec["program"])
            if not argv:
                raise ChatToolError(409, "Programme introuvable sur ce PC.")
            argv = [*argv, *spec.get("args", ())]
            work = home(tool)
            work.mkdir(parents=True, exist_ok=True)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            env = tool_env()
            if spec.get("claude") and job:
                for name in CLAUDE_HIDDEN:
                    env.pop(name, None)
                model = job.get("model") or job.get("claude_model") or "auto"
                env.update(claude_env(job["port"], job["key"], model))
            if spec.get("openwebui") and job:
                env.update(openwebui_env(work, job["port"], job["key"]))
            self.services[tool] = subprocess.Popen(
                argv,
                cwd=work,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            ready = " Il est prêt dans une minute environ." if spec.get("openwebui") else ""
            return f"Démarré sur ce PC : {spec['url']} (adresse valable sur ce PC seulement).{ready}"
        return f"Déjà démarré sur ce PC : {spec['url']}."

    def _detached(self, tool: str, spec: dict) -> str:
        """Service qui se détache lui-même (Claude Code Router : « ccr start ») et écrit l'adresse de son interface,
        jeton d'accès compris : ouverte dans le navigateur de ce PC, jamais recopiée dans le chat (le jeton protège
        l'interface comme un mot de passe)."""
        argv = program(spec["program"])
        if not argv:
            raise ChatToolError(409, "Programme introuvable sur ce PC.")
        work = home(tool)
        work.mkdir(parents=True, exist_ok=True)
        try:
            done = subprocess.run(
                [*argv, *spec.get("args", ())],
                cwd=work,
                env=tool_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            raise ChatToolError(504, "Le service n'a pas démarré en 2 minutes.")
        found = re.search(r"https?://(?:127\.0\.0\.1|localhost)[^\s\"'<>]*", _clean(done.stdout + done.stderr))
        if not found:
            raise ChatToolError(502, f"Le service n'a pas démarré : {_cut(_clean(done.stderr or done.stdout))[-800:]}")
        webbrowser.open(found.group(0))
        return f"Démarré et ouvert dans le navigateur de ce PC ({spec['url']}, avec son jeton d'accès)."

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

    async def _watched(self, run: dict, job: dict) -> str:
        """Watch : son script (images de la vidéo et sous-titres) lancé par Lezard IA, moteur local (rien n'est
        envoyé à Google), car il demande des commandes que l'IA n'a pas ici. L'IA reçoit le rapport et lit les
        images avec l'outil Read."""
        message = job["message"]
        source = await asyncio.to_thread(video_source, message)
        script = await asyncio.to_thread(watch_script)
        if not source or not script:
            return message
        work = home(run["tool"]) / job["session"]
        await asyncio.to_thread(work.mkdir, parents=True, exist_ok=True)
        argv = [sys.executable, str(script), source, "--engine", "local", "--no-whisper", "--no-dedup"]
        argv += ["--out-dir", str(work)]  # sans dédoublonnage : un texte court qui change garde son image
        code, out = await self._exec(argv, work, tool_env(), CLI_TIMEOUT, {"tool": "watch"})
        if code != 0:
            raise ChatToolError(502, f"Watch n'a pas pu lire la vidéo : {_cut(_clean(out))[-1500:]}")
        report = out[out.find("# watch") :] if "# watch" in out else out
        report += await describe_frames(report, job["port"], job["key"])
        return (
            f"{message}\n\nLezard IA a déjà préparé la vidéo avec Watch (inutile de lancer ses scripts). Lis avec "
            f"l'outil Read chaque image listée ci-dessous, puis réponds à la demande à partir des images et du "
            f"texte.\n\n{report[:12000]}"
        )

    async def _prepared(self, spec: dict, job: dict) -> str | None:
        """Point de départ valide créé par l'outil lui-même avant l'IA (« prepare » : HyperFrames, hyperframes init
        dans le dossier de la demande s'il n'a pas encore de composition) : une petite IA modifie un projet juste
        bien mieux qu'elle n'en écrit un de zéro."""
        prep = spec.get("prepare")
        folders = await asyncio.to_thread(named_folders, job["message"]) if prep else []
        argv = program(prep["program"]) if prep else None
        if not folders or not argv:
            return None
        folder = Path(folders[0])
        if await asyncio.to_thread((folder / prep["unless"]).exists):
            return None
        args = [a.replace("{name}", folder.name) for a in prep["args"]]
        cwd = folder if prep.get("cwd") == "folder" else folder.parent
        code, _ = await self._exec([*argv, *args], cwd, tool_env(), CLI_TIMEOUT, {"tool": prep["tool"]})
        return prep["note"].format(folder=folder) if code == 0 else None

    async def _checked(self, run: dict, spec: dict, job: dict) -> None:
        """Contrôle de l'extension sur ce qu'elle vient d'écrire (« check » : HyperFrames, hyperframes lint) : en cas
        d'erreurs, elles sont renvoyées à l'IA dans la même conversation pour qu'elle corrige, deux fois au plus. Le
        résultat du dernier contrôle est ajouté à la réponse."""
        check = spec.get("check")
        folders = await asyncio.to_thread(named_folders, job["message"]) if check else []
        argv = program(check["program"]) if check else None
        if not folders or not argv:
            run["status"] = "done" if check else run["status"]
            return
        folder = folders[0]
        for attempt in range(CHECK_ROUNDS + 1):
            code, out = await self._exec(
                [*argv, *check["args"], folder], Path(folder), tool_env(), CLI_TIMEOUT, {"tool": check["tool"]}
            )
            report = _cut(_clean(out))
            if code == 0 or attempt == CHECK_ROUNDS:
                break
            fix = f"Le contrôle « {check['label']} » du dossier {folder} signale ces erreurs. Corrige les fichiers "
            fix += f"pour qu'il passe, sans poser de question :\n\n{report[:6000]}"
            await self._agent(run, spec, **{**job, "message": fix})
        verdict = "réussi" if code == 0 else f"encore des erreurs après {CHECK_ROUNDS} corrections"
        run["text"] = f"{run.get('text') or ''}\n\nContrôle « {check['label']} » : {verdict}.\n{_block(report)}".strip()
        run["status"] = "done"

    # -- agents --
    async def _agent(self, run: dict, spec: dict, *, key, message, session, model, port, claude_model=None) -> None:
        runner = spec["runner"]
        argv = program(PROGRAMS[runner])
        if not argv:
            raise ChatToolError(409, "Programme introuvable sur ce PC.")
        work = home(run["tool"]) / session
        work.mkdir(parents=True, exist_ok=True)
        again = (work / ".lezard-session").exists()  # échange précédent dans cette conversation : il s'en souvient
        cwd = await asyncio.to_thread(project_folder, work, message) if spec.get("in_folder") else work
        base = f"http://127.0.0.1:{port}/v1"
        env = {**tool_env(), "LEZARD_API_KEY": key}
        env.update(bundled_env(spec))
        # modèle : celui choisi dans le chat, sinon Claude si une clé d'Anthropic est enregistrée dans Lezard IA,
        # sinon le choix automatique de Lezard IA
        model = model or claude_model
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
            config.write_text(json.dumps(_opencode_config(base, model, named_folders(message))), encoding="utf-8")
            env["OPENCODE_CONFIG"] = str(config.resolve())
            args = ["run", "--model", f"lezard/{m}", "--dir", str(work.resolve())]
            args += (["--continue"] if again else []) + ["--", message]
        elif runner == "qwen":
            env.update(OPENAI_API_KEY=key, OPENAI_BASE_URL=base, OPENAI_MODEL=m)
            args = ["--auth-type", "openai", "--openai-base-url", base, "--model", m, "--output-format", "text"]
            # fichiers modifiés sans demander (pas de terminal pour accepter), jamais les commandes ; les dossiers
            # nommés dans la demande font partie de son espace de travail
            args += ["--approval-mode", "auto-edit"]
            for folder in await asyncio.to_thread(named_folders, message):
                args += ["--include-directories", folder]
            args += (["--continue"] if again else []) + [f"--prompt={message}"]  # « -- » n'est pas compris
        else:  # claude : Claude Code de ce PC et son extension, mais l'IA est celle de Lezard IA (jamais son compte)
            for name in CLAUDE_HIDDEN:
                env.pop(name, None)
            env.pop("LEZARD_API_KEY", None)
            env.update(claude_env(port, key, m))
            args = ["-p", spec.get("prefix", "") + message, "--output-format", "text", "--model", m]
            claude = is_claude(m)  # Claude (clé d'Anthropic dans Lezard IA) : l'extension telle qu'elle a été écrite
            args += ["--permission-mode", "acceptEdits", "--tools", CLAUDE_FULL if claude else CLAUDE_TOOLS]
            servers = mcp_args(run["tool"], spec, local=not claude)
            allowed = list(READ_ANYWHERE)  # lecture partout ; écriture : son dossier et ceux nommés dans la demande
            if "--allowedTools" in servers:
                at = servers.index("--allowedTools")
                allowed += servers[at + 1].split(",")
                del servers[at : at + 2]
            args += [*servers, "--allowedTools", ",".join(allowed)]
            if not claude and spec.get("deny_local"):  # IA locale : outils de l'extension qui l'égareraient, masqués
                args += ["--disallowedTools", ",".join(spec["deny_local"])]
            for folder in await asyncio.to_thread(named_folders, message):
                args += ["--add-dir", folder]
            guide = [LANGUAGE]  # la langue de la demande (Claude Code répond sinon en anglais)
            if not claude and spec.get("methods"):  # Superpowers : ses méthodes en français, choisies sans appel
                guide.append(methods_prompt(message, again))
            elif not claude and not spec.get("prefix") and not message.startswith("/"):
                guide.append(await plugin_guide(run["tool"], message, port, key, m))
                if guide[-1]:  # méthode déjà donnée : sans l'outil Skill, elle ne part pas vers une compétence
                    args[args.index("--tools") + 1] = CLAUDE_TOOLS.replace(",Skill", "")  # intégrée inadaptée
            # IA locale : la méthode qui convient, donnée d'office (un petit modèle n'irait pas la chercher)
            args += ["--append-system-prompt", "\n\n".join(g for g in guide if g)]
            args += ["--continue"] if again else []
        code, out = await self._exec([*argv, *args], cwd, env, AGENT_TIMEOUT, run)
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
            run["model"] = m
        if text:
            (work / ".lezard-session").touch()
            # avec un contrôle à faire (_checked), la demande reste en cours jusqu'à son résultat
            run.update(status="running" if spec.get("check") else "done", text=_cut(text))
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


def is_claude(model: str) -> bool:
    """Modèle Claude d'Anthropic, par Lezard IA (« anthropic/claude-sonnet-5-5 »)."""
    return "claude" in model.lower().rsplit("/", 1)[-1]


def claude_model_of(providers: dict) -> str | None:
    """Un modèle Claude si une clé d'API d'Anthropic est enregistrée dans Lezard IA (page Modèles) ; sinon None."""
    for name, p in sorted(providers.items()):
        cfg = getattr(p, "cfg", None)
        if cfg and cfg.kind == "anthropic" and cfg.enabled and not cfg.owner and cfg.resolved_key():
            return f"{name}/{CLAUDE_DEFAULT}"
    return None


# méthodes de Superpowers pour une IA locale : les compétences françaises condensées de Lezard IA (skill_packs), la
# ou les deux qui conviennent à la demande, données dans les instructions (--append-system-prompt)
CLAUDE_DEFAULT = "claude-sonnet-5-5"
METHODS = (
    ("debogage", r"bug|bogue|erreur|plante|crash|marche pas|fonctionne pas|exception|traceback|d[ée]bog|cass[ée]"),
    ("tdd", r"\btests?\b|\btdd\b|\btester\b"),
    ("plan", r"\bplan\b|planifi|[ée]tapes|d[ée]coup"),
    ("verifier", r"v[ée]rifi|\bfini|termin[ée]|[çc]a marche|livr"),
    ("cadrer", r"constru|\bcr[ée]e|\bfais|\bfaire\b|d[ée]velopp|appli|\bsite\b|projet|\bid[ée]e|fonctionnalit|ajout"),
)


def plugins_of(tool: str) -> list[str]:
    """Extensions de Claude Code installées par la recette de l'outil (« claude plugin install x@y »)."""
    recipe = installers.RECIPES.get(f"tool:{tool}") or {}
    return [s[3] for s in recipe.get("steps", ()) if tuple(s[:3]) == ("claude", "plugin", "install") and len(s) > 3]


def plugin_skills(tool: str) -> list[dict]:
    """Méthodes (skills) des extensions de l'outil, lues là où Claude Code les a installées : nom, description,
    fichier."""
    try:
        index = json.loads((Path.home() / ".claude" / "plugins" / "installed_plugins.json").read_text("utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for plugin in plugins_of(tool):
        for entry in (index.get("plugins") or {}).get(plugin) or ():
            root = Path(entry.get("installPath") or "")
            # compétences, et agents spécialisés (wshobson : relecteur, auditeur de sécurité, débogueur), qu'une IA
            # locale ne peut pas lancer en sous-agent : leur consigne lui est donnée comme méthode
            files = (
                [*sorted(root.glob("skills/*/SKILL.md")), *sorted(root.glob("agents/*.md"))] if root.is_dir() else ()
            )
            for f in files:
                head = f.read_text("utf-8", errors="replace")[:2000]
                name = re.search(r"^name:\s*(.+)$", head, re.M)
                desc = re.search(r"^description:\s*(.+)$", head, re.M)
                if name:
                    out.append(
                        {
                            "name": name.group(1).strip().strip('"'),
                            "description": (desc.group(1) if desc else "").strip().strip('"')[:300],
                            "path": f,
                        }
                    )
    return out


GUIDE_MAX = 8000  # caractères d'une méthode donnée à une IA locale


async def plugin_guide(tool: str, message: str, port: int, key: str, model: str) -> str:
    """IA locale : la méthode de l'extension qui convient à la demande, choisie par le modèle lui-même en une courte
    requête (les descriptions sont en anglais, la demande souvent en français), puis donnée dans ses instructions.
    Rien si l'extension n'a pas de méthode, ou si aucune ne convient."""
    # « using-… » : le mode d'emploi général de l'extension, jamais la méthode d'une demande précise
    found = [s for s in plugin_skills(tool) if not s["name"].lower().startswith("using-")]
    if not found:
        return ""
    if len(found) == 1:
        chosen = found[0]
    else:
        listing = "\n".join(f"- {s['name']} : {s['description']}" for s in found)
        ask = {
            "model": model,
            "max_tokens": 800,  # un modèle qui réfléchit d'abord (gemma4, qwen3…) y passe ses premiers tokens
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "Réponds seulement par le nom d'une méthode de la liste, ou « aucune »."},
                {
                    "role": "user",
                    "content": f"Méthodes :\n{listing}\n\nDemande : {message[:2000]}\n\nLaquelle convient ?",
                },
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                r = await client.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions", json=ask, headers={"Authorization": f"Bearer {key}"}
                )
            reply = r.json()["choices"][0]["message"] if r.status_code == 200 else {}
            said = (reply.get("content") or "").lower()
            if not said.strip():  # réponse vide après la réflexion : la méthode retenue en dernier dans la réflexion
                thought = (reply.get("reasoning") or reply.get("reasoning_content") or "").lower()
                last = max(found, key=lambda s: thought.rfind(s["name"].lower()))
                said = last["name"].lower() if thought.rfind(last["name"].lower()) >= 0 else ""
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            said = ""
        chosen = next((s for s in sorted(found, key=lambda s: -len(s["name"])) if s["name"].lower() in said), None)
        if not chosen:
            return ""
    text = chosen["path"].read_text("utf-8", errors="replace")
    text = re.sub(r"^---\n.*?\n---\n", "", text.replace("\r\n", "\n"), count=1, flags=re.S).strip()[:GUIDE_MAX]
    return (
        f"Méthode « {chosen['name']} » de l'extension à suivre dès cette réponse (donnée par Lezard IA ; tu n'as pas "
        f"besoin de l'outil Skill pour elle). Réponds en français.\n\n{text}"
    )


CHECK_ROUNDS = 2  # corrections demandées au plus après un contrôle raté (_checked)
READ_ANYWHERE = ("Read", "Glob", "Grep")  # sans autorisation dans « -p », même hors de son dossier de travail
LANGUAGE = (
    "Réponds, et rédige les documents que tu écris, dans la langue de la demande de l'utilisateur (en français si "
    "elle est en français), même quand les consignes d'une commande ou d'une compétence sont en anglais."
)
_PATH = re.compile(r"[A-Za-z]:\\[^\s\"'«»<>|*?]+")


_URL = re.compile(r"https?://[^\s\"'<>«»]+")


def video_source(message: str) -> str | None:
    """La vidéo d'une demande à Watch : son adresse, sinon un fichier de ce PC écrit en toutes lettres."""
    if url := _URL.search(message):
        return url.group(0).rstrip(".,;:!)")
    for raw in _PATH.findall(message):
        path = Path(raw.rstrip(".,;:!)"))
        if path.is_file():
            return str(path)
    return None


_FRAME = re.compile(r"`([^`]+\.(?:jpg|jpeg|png))`\s*\(t=([0-9:]+)")
MAX_FRAMES_DESCRIBED = 16
DESCRIBE = "Image extraite d'une vidéo. Décris-la en deux phrases et recopie exactement tout texte visible."


async def describe_frames(report: str, port: int, key: str) -> str:
    """Images d'un rapport de Watch décrites par Lezard IA (modèle « auto » : il choisit un modèle qui voit les
    images). L'IA de l'agent peut ne pas les voir elle-même (gemma4:e4b avec Ollama 0.40 : images ignorées), et une
    IA qui voit les images ne gère pas toujours les outils (gemma3)."""
    found = _FRAME.findall(report)
    step = max(1, -(-len(found) // MAX_FRAMES_DESCRIBED))  # réparties sur toute la vidéo
    frames = found[::step]
    lines = []
    async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
        for path, at in frames:
            try:
                data = await asyncio.to_thread(Path(path).read_bytes)
            except OSError:
                continue
            url = "data:image/jpeg;base64," + base64.b64encode(data).decode()
            content = [{"type": "image_url", "image_url": {"url": url}}, {"type": "text", "text": DESCRIBE}]
            try:
                r = await client.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    json={"model": "auto", "messages": [{"role": "user", "content": content}], "max_tokens": 300},
                    headers={"Authorization": f"Bearer {key}"},
                )
                text = r.json()["choices"][0]["message"]["content"].strip()
            except (httpx.HTTPError, ValueError, KeyError, IndexError):
                continue
            lines.append(f"- t={at} : {' '.join(text.split())}")
    if not lines:
        return ""
    return "\n\n## Ce que montrent les images (décrites par Lezard IA)\n\n" + "\n".join(lines)


def openwebui_env(work: Path, port: int, key: str) -> dict:
    """Open WebUI lancé par Lezard IA : ses données dans le dossier de Lezard IA, branché sur ses modèles (clé de
    l'appelant), sans Ollama en direct (Lezard IA le couvre), sans télémétrie ; sa clé de session gardée d'un
    lancement à l'autre (sinon chaque redémarrage déconnecte ses utilisateurs)."""
    data = work / "data"
    data.mkdir(parents=True, exist_ok=True)
    secret = work / "secret.txt"
    if not secret.exists():
        secret.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return {
        "DATA_DIR": str(data.resolve()),
        "OPENAI_API_BASE_URL": f"http://127.0.0.1:{port}/v1",
        "OPENAI_API_KEY": key,
        "ENABLE_OLLAMA_API": "false",
        "WEBUI_SECRET_KEY": secret.read_text(encoding="utf-8").strip(),
        "SCARF_NO_ANALYTICS": "true",
        "ANONYMIZED_TELEMETRY": "false",
        "DO_NOT_TRACK": "true",
    }


def project_folder(work: Path, message: str) -> Path:
    """Spec Kit : Claude Code travaille dans le projet nommé dans la demande (ses commandes y vivent), retenu pour
    la suite de la conversation ; sans projet nommé, dans son propre dossier."""
    named, kept = named_folders(message), work / ".lezard-folder"
    if named:
        kept.write_text(named[0], encoding="utf-8")
    folder = Path(kept.read_text(encoding="utf-8")) if kept.exists() else None
    return folder if folder is not None and folder.is_dir() else work


def watch_script() -> Path | None:
    """Le script de l'extension Watch installée (sa version la plus récente)."""
    root = Path.home() / ".claude" / "plugins" / "cache" / "claude-video" / "watch"
    found = sorted(root.glob("*/skills/watch/scripts/watch.py"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def named_folders(message: str) -> list[str]:
    """Dossiers (ou dossiers des fichiers) écrits en toutes lettres dans la demande et présents sur ce PC : l'outil
    peut y écrire (« --add-dir »), comme dans son propre dossier de travail ; ailleurs, il lit seulement."""
    found = []
    for raw in _PATH.findall(message):
        path = Path(raw.rstrip(".,;:!)"))
        folder = path if path.is_dir() else path.parent if path.parent.is_dir() and path.parent != path else None
        if folder is not None and len(folder.parts) > 1 and str(folder) not in found:
            found.append(str(folder))
    return found[:5]


RELAY = (("--only", "only_tools"), ("--drop", "drop_args"), ("--any-word", "any_word"))  # réglages de mcp_filter.py


def mcp_args(tool: str, spec: dict, local: bool = False) -> list[str]:
    """Serveurs MCP proposés à Claude Code : aucun, sauf celui de l'extension (« mcp » : son nom dans ~/.claude.json),
    écrit à part pour cette tâche. Introuvable sous ce nom : ceux dont le nom le contient ; sinon, tous ceux de
    Claude Code (l'extension doit marcher, même si sa demande est plus lourde). Ses réglages (« mcp_env ») lui sont
    donnés ; avec une IA locale (`local`), le relais mcp_filter.py le lui adapte (outils montrés, arguments retirés,
    recherches refaites mot par mot)."""
    want = spec.get("mcp")
    if not want:
        return ["--strict-mcp-config"]
    try:
        servers = json.loads((Path.home() / ".claude.json").read_text("utf-8")).get("mcpServers") or {}
    except (OSError, ValueError):
        servers = {}
    keep = {n: c for n, c in servers.items() if n.lower() == want.lower()} or {
        n: c for n, c in servers.items() if want.lower() in n.lower()
    }
    if not keep:  # serveur fourni par l'extension (son .mcp.json) : chargé par Claude Code, ses outils autorisés
        return ["--allowedTools", ",".join(spec["allow"])] if spec.get("allow") else []
    for name, config in keep.items():
        if not isinstance(config, dict) or "command" not in config:
            continue  # serveur distant (adresse) : rien à lancer ici
        config = keep[name] = {**config, "env": {**(config.get("env") or {}), **spec.get("mcp_env", {})}}
        relay = [part for flag, name in RELAY if spec.get(name) for part in (flag, ",".join(spec[name]))]
        if local and relay:  # IA locale : relais qui adapte le serveur à un petit modèle (mcp_filter.py)
            argv = [config["command"], *(config.get("args") or [])]
            config["command"] = sys.executable
            config["args"] = [installers._script("mcp_filter"), *relay, "--", *argv]
    path = home(tool) / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": keep}), encoding="utf-8")
    allowed = ",".join(f"mcp__{name}" for name in keep)  # ses outils autorisés d'office (sinon refusés sans « -p »)
    return ["--mcp-config", str(path.resolve()), "--strict-mcp-config", "--allowedTools", allowed]


def methods_prompt(message: str, again: bool) -> str:
    """Instructions ajoutées pour une IA locale : au plus deux méthodes, choisies d'après la demande ; sans indice,
    cadrer au premier échange, vérifier ensuite."""
    from . import skills

    text = message.lower()
    found = [sid for sid, pattern in METHODS if re.search(pattern, text)][:2]
    found = found or (["verifier"] if again else ["cadrer"])
    packs = skills.load()
    body = "\n\n".join(packs[sid].content for sid in found if sid in packs)
    return (
        "Méthode Superpowers à suivre dès cette réponse (donnée par Lezard IA ; tu n'as pas besoin de l'outil Skill "
        "pour elle). Réponds en français.\n\n" + body
    )


def claude_env(port: int, key: str, model: str) -> dict:
    """Claude Code branché sur Lezard IA (route /v1/messages, format d'Anthropic) : la clé de l'appelant prime sur le
    compte claude.ai de ce PC, chaque modèle demandé par Claude Code (principal, rapide, sous-agents) devient celui
    choisi dans le chat ; ni télémétrie ni trafic annexe vers Anthropic."""
    names = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL")
    names += ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")
    return {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_AUTH_TOKEN": key,
        **dict.fromkeys(names, model),
        "DISABLE_TELEMETRY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


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


def bundled_env(spec: dict) -> dict:
    """Programmes fournis avec l'outil par sa recette (REA : Ghidra et Java 21), s'ils sont bien là."""
    paths = {name: os.path.expandvars(value) for name, value in spec.get("env", {}).items()}
    return {name: path for name, path in paths.items() if os.path.isdir(path)}


def tool_env() -> dict:
    """Variables d'un outil lancé : sortie en UTF-8 (sous Windows, Python écrirait sinon dans l'ancien jeu de
    caractères et les accents arriveraient abîmés), sans couleurs ni télémétrie (DO_NOT_TRACK). PATH de Windows relu (un outil installé depuis le
    démarrage de Lezard IA y a ajouté son dossier : claude-mem), avec les commandes de uv et winget."""
    installers.refresh_path()
    installers.with_tool_dirs()
    env = {k: v for k, v in os.environ.items() if k.upper() != "ELECTRON_RUN_AS_NODE"}  # voir _shell_open
    return {**env, "DO_NOT_TRACK": "1", "NO_COLOR": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def _opencode_config(base: str, model: str | None, folders: list[str] = ()) -> dict:
    """Réglages d'OpenCode : Lezard IA comme fournisseur ; les dossiers nommés dans la demande autorisés (sinon il
    refuse d'y écrire, sans pouvoir demander : pas de terminal)."""
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
        "permission": {"external_directory": {str(Path(f) / "*"): "allow" for f in folders}},
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
