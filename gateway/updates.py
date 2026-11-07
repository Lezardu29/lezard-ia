"""Version installée et nouvelle version disponible (18.3).

Version : __version__ (15.4) et, dans une copie git, le commit installé. Vérification (GATEWAY_UPDATE_CHECK=on,
désactivée par défaut) : toutes les 5 minutes, `git fetch` du dépôt d'origine puis comparaison avec la branche suivie
— rien n'est jamais installé : la mise à jour reste une action du propriétaire (lancer.ps1 -Update sur le PC, qui
sauvegarde, met à jour, teste et revient en arrière en cas d'échec).

Installation Windows (installateur, 32.3) : la version signée est lue sur GitHub (latest.json du dépôt public
lezard-ia-telecharger, release.py), à chaque lancement puis toutes les 5 minutes ; elle est proposée, jamais imposée : sur
demande du propriétaire, l'installateur est téléchargé, sa taille et son empreinte SHA-256 vérifiées, la base
sauvegardée, puis il est lancé en mode silencieux (il arrête le service, remplace les fichiers et le relance).
Retour arrière : la version précédente, signée elle aussi, s'installe de la même façon.

Installation depuis le zip (même dossier, sans installateur : pas de unins000.exe) : la même version signée annonce
aussi son zip. Il est téléchargé et vérifié de la même façon, extrait dans data/updates/nouvelle/, puis
mise_a_jour.py (celui de la NOUVELLE version, lancé avec une copie de son Python) arrête Lezard IA, remplace le
programme sans toucher aux données, le relance, et remet l'ancien programme si le nouveau ne répond pas.

Image Docker, ou dossier sans git : pas de vérification (mettre à jour l'image).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx

from . import __version__
from .release import ReleaseError, newer, verify

ROOT = Path(__file__).resolve().parent.parent
INTERVAL = 5 * 60  # toutes les 5 minutes : une nouvelle version est proposée sans attendre le lendemain
TIMEOUT = 60


def check_enabled() -> bool:
    """Vérification régulière (toutes les 5 min) : activée par défaut pour l'installation Windows (versions signées, 32.3)."""
    default = "on" if installer_mode() else "off"
    return os.getenv("GATEWAY_UPDATE_CHECK", default).strip().lower() in ("1", "on", "true", "yes")


def _git(*args: str, timeout: float = 10) -> str | None:
    git = shutil.which("git")
    if not git or not (ROOT / ".git").exists():
        return None
    try:
        r = subprocess.run(
            [git, "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )  # jamais de demande d'identifiants
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def packaging(root: Path | None = None) -> str:
    """« exe » : installé par l'installateur (son désinstalleur est là) ; « zip » : installé depuis le zip."""
    return "exe" if any((root or ROOT).glob("unins*.exe")) else "zip"


def installer_mode() -> bool:
    """Installé par l'installateur Windows (32.1) : lanceur et Python embarqué à côté du code."""
    return (ROOT / "lezard.py").is_file() and (ROOT / "python" / "python.exe").is_file()


# version signée publiée avec l'installateur : {"release": "LZU1.…", "previous": "LZU1.…"} ; la signature (clé à part)
# est vérifiée ici, donc un fichier modifié sur GitHub ne peut rien faire installer
RELEASES = "https://raw.githubusercontent.com/Lezardu29/lezard-ia-telecharger/main/latest.json"
STARTUP_DELAY = 30  # vérification à chaque lancement, une fois la gateway démarrée


def release_url() -> str:
    return os.getenv("LEZARD_UPDATE_URL", "").strip() or RELEASES


# Intermédiaire qui lance l'installateur puis se termine aussitôt : l'installateur n'a plus la gateway pour ancêtre.
# Sans lui, l'arrêt du service au début de l'installation (lezard.py stop : la gateway et TOUT son arbre, taskkill /T)
# arrêtait aussi l'installateur : pas de mise à jour, et pas de redémarrage.
_DETACH = (
    "import subprocess, sys\n"
    "flags = 0x8 | 0x200 | 0x1000000  # DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, CREATE_BREAKAWAY_FROM_JOB\n"
    "try:\n"
    "    subprocess.Popen(sys.argv[1:], creationflags=flags, close_fds=True)\n"
    "except OSError:  # tâche planifiée qui interdit de sortir de son groupe de processus\n"
    "    subprocess.Popen(sys.argv[1:], creationflags=flags & ~0x1000000, close_fds=True)\n"
)


def launch_detached(args: list[str]) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([sys.executable, "-c", _DETACH, *args], creationflags=flags, close_fds=True).wait(timeout=30)


def launch_installer(path: Path, log_file: Path, extra: tuple[str, ...] = ()) -> None:
    """Installateur en mode silencieux, hors de l'arbre de la gateway : il arrête le service (et donc ce processus),
    remplace les fichiers et relance Lezard IA."""
    launch_detached([str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/LOG={log_file}", *extra])


def _shell_folder(csidl: int) -> Path | None:
    """Dossier de Windows (Bureau, Démarrage), même redirigé (Bureau dans OneDrive)."""
    if sys.platform != "win32":
        return None
    import ctypes

    buf = ctypes.create_unicode_buffer(260)
    return Path(buf.value) if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 else None


def migration_args(app: Path, folder=None) -> tuple[str, ...]:
    """Passage du zip à l'installateur signé : même dossier et mêmes choix que l'installation zip. Sans /TASKS,
    une 1re installation silencieuse cocherait tout, dont l'accès depuis la maison (pare-feu : demande d'accord
    de Windows au milieu de la mise à jour)."""
    folder, tasks = folder or _shell_folder, []
    if (folder(7) or Path("-")).joinpath("Lezard IA.lnk").exists():  # CSIDL_STARTUP : démarrage avec Windows
        tasks.append("autostart")
    if (folder(0x10) or Path("-")).joinpath("Lezard IA.lnk").exists():  # CSIDL_DESKTOPDIRECTORY
        tasks.append("desktopicon")
    if os.getenv("TOOLS_WEB", "true").strip().lower() not in ("0", "false", "no", "off"):  # comme tools.web_enabled
        tasks.append("web")  # l'installateur réécrit TOOLS_WEB d'après cette case
    return (f"/DIR={app}", f"/TASKS={','.join(tasks)}")


def launch_zip_update(python: Path, script: Path, app: Path, new: Path) -> None:
    """mise_a_jour.py hors de l'arbre de la gateway : il l'arrête, remplace le programme et relance Lezard IA."""
    launch_detached([str(python), str(script), str(app), str(new)])


class UpdateError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def installed() -> dict:
    commit = _git("rev-parse", "--short", "HEAD")
    source = "git" if commit else "installer" if installer_mode() else "archive"
    out = {"version": __version__, "commit": commit, "source": source}
    if source == "installer":
        out["package"] = packaging()
    if commit:
        out["date"] = _git("log", "-1", "--format=%cI")
        out["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD")
        out["local_changes"] = bool(_git("status", "--porcelain", "--untracked-files=no"))
    return out


class UpdateChecker:
    def __init__(
        self,
        store,
        clock=time.time,
        transport=None,
        launcher=None,
        backups=None,
        folder=None,
        zip_launcher=None,
        owner: str | None = None,
    ):
        self.store, self.clock = store, clock
        self.owner = owner  # compte de la clé maître : reçoit la notification « nouvelle version »
        self.transport, self.backups = transport, backups
        self.launcher = launcher or launch_installer
        self.zip_launcher = zip_launcher or launch_zip_update
        self.app = ROOT  # dossier de l'installation (mise à jour depuis le zip)
        self.folder = Path(folder) if folder else ROOT / "data" / "updates"
        self._info: dict | None = None

    @property
    def info(self) -> dict:
        if self._info is None:  # à la 1re demande : git n'est pas lancé à chaque démarrage
            self._info = installed()
        return self._info

    def status(self) -> dict:
        last = self.store.get_setting("update_check") or {}
        return {
            "installed": self.info,
            "enabled": check_enabled(),
            "can_check": self.info["source"] in ("git", "installer"),
            "can_install": self.info["source"] == "installer",
            "last": {k: v for k, v in last.items() if k not in ("token", "previous_token")} or None,
            "install": self.store.get_setting("update_install") or None,
            "command": ".\\lancer.ps1 -Update",
        }

    def _http(self) -> httpx.Client:
        return httpx.Client(transport=self.transport, timeout=30, follow_redirects=True)

    def check_release(self) -> dict:
        """Version signée la plus récente (et la précédente) chez le serveur central, signatures vérifiées."""
        result: dict = {"at": self.clock()}
        try:
            with self._http() as client:
                r = client.get(release_url())
            data = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            data = None
        if not isinstance(data, dict) or not data.get("release"):
            result["error"] = "Serveur des mises à jour injoignable, ou aucune version publiée : réessayez plus tard."
            return result
        try:
            latest = verify(data["release"])
            previous = verify(data["previous"]) if data.get("previous") else None
        except ReleaseError as e:
            result["error"] = str(e)
            return result
        current = self.info["version"]
        result.update(
            {
                "available": newer(latest["version"], current),
                "version": latest["version"],
                "notes": latest["notes"],
                "size": latest["zip"]["size"] if self._by_zip(latest) and latest["zip"] else latest["size"],
                "token": data["release"],
            }
        )
        if previous and newer(current, previous["version"]):
            result.update({"previous": previous["version"], "previous_token": data["previous"]})
        return result

    def install(self, which: str = "latest") -> dict:
        """Télécharge l'installateur signé (dernière version ou précédente), vérifie taille et empreinte, sauvegarde
        la base, puis lance l'installation. Rien n'est lancé si une vérification échoue."""
        if self.info["source"] != "installer":
            raise UpdateError(409, "Installation automatique réservée à la version installée avec l'installateur.")
        last = self.store.get_setting("update_check") or {}
        token = last.get("token" if which == "latest" else "previous_token")
        if not token or (which == "latest" and not last.get("available")):
            raise UpdateError(409, "Aucune version à installer : vérifiez d'abord les mises à jour.")
        try:
            release = verify(token)  # revérifiée : le réglage enregistré a pu être modifié
        except ReleaseError as e:
            raise UpdateError(409, str(e))
        self.folder.mkdir(parents=True, exist_ok=True)
        by_zip = self._by_zip(release)
        if by_zip:
            if not release["zip"]:
                raise UpdateError(
                    409, "Cette version n'existe pas en zip : téléchargez-la sur https://lezard-ia.fr/telecharger."
                )
            target = self.folder / f"LezardIA-{release['version']}.zip"
            self._download(release["zip"], target)
            new = self._extract(target)
        else:
            target = self.folder / f"LezardIA-Setup-{release['version']}.exe"
            self._download(release, target)
        backup = None
        if self.backups is not None and self.backups.available:
            backup = self.backups.create("avant-mise-a-jour")["name"]
        for old in [*self.folder.glob("LezardIA-Setup-*.exe"), *self.folder.glob("LezardIA-*.zip")]:
            if old != target:  # on ne garde que ce téléchargement
                old.unlink(missing_ok=True)
        if by_zip:
            runner = self._runner(new)
            self.zip_launcher(runner / "python" / "python.exe", runner / "mise_a_jour.py", self.app, new)
        elif self._zip():  # installateur signé : l'installation zip passe à l'installateur (données gardées)
            self.launcher(target, self.folder / "installation.log", migration_args(self.app))
        else:
            self.launcher(target, self.folder / "installation.log")
        result = {"installing": release["version"], "backup": backup, "at": self.clock()}
        self.store.set_setting("update_install", result)
        return result

    def _zip(self) -> bool:
        return self.info.get("package") == "zip"

    def _by_zip(self, release: dict) -> bool:
        """Mise à jour par le zip : installation zip, tant que l'installateur n'est pas signé (ensuite elle passe à
        l'installateur, sans avertissement de Windows)."""
        return self._zip() and not release.get("signed")

    def _download(self, file: dict, target: Path) -> None:
        """Fichier de la version signée : refusé (et supprimé) si sa taille ou son empreinte SHA-256 diffère."""
        partial = target.with_suffix(".part")
        digest, size = hashlib.sha256(), 0
        try:
            with self._http() as client, client.stream("GET", file["url"]) as r:
                if r.status_code != 200:
                    raise UpdateError(502, f"Téléchargement refusé ({r.status_code}) : réessayez plus tard.")
                with open(partial, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        size += len(chunk)
                        if size > file["size"]:
                            break
                        digest.update(chunk)
                        f.write(chunk)
        except httpx.HTTPError:
            partial.unlink(missing_ok=True)
            raise UpdateError(502, "Téléchargement interrompu : réessayez plus tard.")
        if size != file["size"] or digest.hexdigest() != file["sha256"]:
            partial.unlink(missing_ok=True)
            raise UpdateError(
                502, "Fichier refusé : il ne correspond pas à la version signée (fichier modifié ou incomplet)."
            )
        partial.replace(target)

    def _extract(self, archive: Path) -> Path:
        """Zip vérifié extrait dans data/updates/nouvelle/ ; rend le dossier « Lezard IA » qu'il contient."""
        dest = self.folder / "nouvelle"
        shutil.rmtree(dest, ignore_errors=True)
        root = dest.resolve()
        try:
            with zipfile.ZipFile(archive) as z:
                for name in z.namelist():
                    if root not in (root / name).resolve().parents:
                        raise UpdateError(502, f"Zip refusé : chemin interdit ({name}).")
                z.extractall(dest)
        except zipfile.BadZipFile:
            raise UpdateError(502, "Zip illisible : réessayez plus tard.")
        new = dest / "Lezard IA"
        if not (new / "python" / "python.exe").is_file() or not (new / "mise_a_jour.py").is_file():
            shutil.rmtree(dest, ignore_errors=True)
            raise UpdateError(502, "Zip incomplet : la mise à jour n'est pas lancée.")
        return new

    def _runner(self, new: Path) -> Path:
        """Copie du Python de la nouvelle version (sans ses dépendances) et des scripts de mise à jour : le python\\
        installé est remplacé pendant que mise_a_jour.py tourne."""
        runner = self.folder / "runner"
        shutil.rmtree(runner, ignore_errors=True)
        (runner / "python").mkdir(parents=True)
        for f in (new / "python").iterdir():
            if f.is_file():
                shutil.copy2(f, runner / "python" / f.name)
        for script in ("mise_a_jour.py", "arret-maj.py"):
            shutil.copy2(new / script, runner / script)
        return runner

    def check(self) -> dict:
        """git fetch puis commits de la branche suivie absents d'ici (aucune modification des fichiers)."""
        now = self.clock()
        result: dict = {"at": now}
        if self.info["source"] == "installer":
            result = self.check_release()
        elif self.info["source"] != "git":
            result["error"] = "Pas une copie git (image Docker ou archive) : mettez à jour l'image ou l'archive."
        elif _git("fetch", "--quiet", timeout=TIMEOUT) is None:
            result["error"] = (
                "Dépôt d'origine injoignable (réseau, identifiants git) : vérification impossible pour l'instant."
            )
        else:
            upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
            if not upstream:
                result["error"] = "La branche installée ne suit aucune branche distante."
            else:
                behind = int(_git("rev-list", "--count", "HEAD..@{u}") or 0)
                result.update({"upstream": upstream, "behind": behind, "available": behind > 0})
                if behind:
                    log = _git("log", "--format=%h%x09%cI%x09%s", "-n", "10", "HEAD..@{u}") or ""
                    result["commits"] = [
                        dict(zip(("commit", "date", "subject"), line.split("\t", 2)))
                        for line in log.splitlines()
                        if line.count("\t") >= 2
                    ]
        self.store.set_setting("update_check", result)
        self._notify(result)
        return result

    def _notify(self, result: dict) -> None:
        """Nouvelle version vue pour la première fois : une notification (cloche du tableau de bord) pour la clé
        maître, une seule par version ; elle propose « Mettre à jour », comme le bandeau."""
        version = result.get("version")
        if not (self.owner and result.get("available") and version):
            return
        if (self.store.get_setting("update_notified") or {}).get("version") == version:
            return
        notes = (result.get("notes") or "").strip()
        message = f"Vous avez la {self.info['version']}." + (f" Nouveautés : {notes}" if notes else "")
        self.store.add_notification(
            self.owner,
            "info",
            f"Lezard IA {version} est disponible",
            message[:600],
            {"kind": "update", "version": version},
        )
        self.store.set_setting("update_notified", {"version": version})

    async def run(self) -> None:
        """Vérification à chaque lancement, puis toutes les 5 minutes (si GATEWAY_UPDATE_CHECK=on, le défaut de
        l'installation Windows) ; une nouvelle version est seulement proposée (bandeau), jamais installée seule."""
        await asyncio.sleep(STARTUP_DELAY)
        while True:
            if check_enabled():
                await asyncio.to_thread(self.check)
            await asyncio.sleep(INTERVAL)
