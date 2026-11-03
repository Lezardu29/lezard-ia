r"""Mise à jour de Lezard IA installé depuis le zip (sans installateur) : remplace le programme, garde les données.

Lancé par la gateway (bouton « Mettre à jour », gateway\updates.py) une fois le zip signé téléchargé, vérifié et
extrait dans data\updates\nouvelle\ :

    runner\python\python.exe runner\mise_a_jour.py <dossier de Lezard IA> <dossier extrait de la nouvelle version>

runner\ est une copie du Python de la NOUVELLE version, sans ses dépendances : le python\ installé peut être remplacé
pendant que ce script tourne. Étapes : arrêt du service et de la gateway (arret-maj.py de la nouvelle version, chacun
seul), ancien programme mis de côté (data\updates\ancienne\), nouveau programme mis en place, composants installés
mis à jour (lezard.py pack refresh), redémarrage, puis attente de la réponse de Lezard IA. S'il ne répond pas :
l'ancien programme est remis en place et relancé. data\ et .env (conversations, clés, réglages) et packs\<pack>\
(composants installés) ne sont jamais touchés.

Aussi utilisé par « lezard.py install » (première installation depuis le zip) : PROGRAM, program_items, copy_program.
Journal : data\logs\mise-a-jour.log ; résultat : data\updates\resultat.json.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# le programme, remplacé à chaque version ; tout le reste du dossier (data\, .env, packs\<pack>\) appartient au client
PROGRAM = (
    "python",
    "gateway",
    "lezard_pro",  # ancien dossier des fonctions Pro (avant 1.0.7) : retiré à la mise à jour
    "__pycache__",
    "lezard.py",
    "lezard-ia.ico",
    "version.txt",
    "arret-maj.py",
    "mise_a_jour.py",
    "Installer Lezard IA.cmd",
)
CREATE_NO_WINDOW, CREATE_NEW_CONSOLE = 0x08000000, 0x00000010
WAIT_HEALTHY = 180  # s : premier démarrage de la nouvelle version (compilation, Ollama, tunnel)


def program_items(src: Path) -> list[str]:
    """Éléments du programme présents dans `src` : PROGRAM, et les manifestes des composants (packs\\*.json)."""
    items = [name for name in PROGRAM if (src / name).exists()]
    return items + sorted(f"packs/{p.name}" for p in (src / "packs").glob("*.json"))


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _retry(work, *args, seconds: float = 30):
    """Fichier encore ouvert juste après l'arrêt (antivirus, indexation) : nouvel essai, pendant `seconds` au plus."""
    end = time.monotonic() + seconds
    while True:
        try:
            return work(*args)
        except OSError:
            if time.monotonic() > end:
                raise
            time.sleep(1)


def unblock(root: Path) -> None:
    """Retire la marque « téléchargé depuis Internet » (Zone.Identifier) que l'Explorateur pose sur chaque fichier
    extrait d'un zip téléchargé : sans ça, Windows demanderait une confirmation à chaque démarrage de Lezard IA."""
    for p in [root, *root.rglob("*")] if root.is_dir() else [root]:
        if p.is_file():
            try:
                os.remove(f"{p}:Zone.Identifier")
            except OSError:
                pass


def copy_program(src: Path, dst: Path) -> None:
    """Première installation (ou réinstallation) depuis le dossier extrait : programme copié, l'ancien remplacé."""
    for item in program_items(src):
        target = dst / item
        _retry(_remove, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if (src / item).is_dir():
            shutil.copytree(src / item, target)
        else:
            shutil.copy2(src / item, target)
        unblock(target)


def swap(app: Path, new: Path, backup: Path) -> list[str]:
    """Nouveau programme mis en place, l'ancien rangé dans `backup` ; rend les éléments remplacés (pour restore)."""
    _remove(backup)
    backup.mkdir(parents=True)
    done: list[str] = []
    try:
        for item in program_items(new):
            if (app / item).exists():
                (backup / item).parent.mkdir(parents=True, exist_ok=True)
                _retry(Path.replace, app / item, backup / item)
            done.append(item)
            (app / item).parent.mkdir(parents=True, exist_ok=True)
            _retry(Path.replace, new / item, app / item)
    except OSError:
        restore(app, backup, done)
        raise
    return done


def restore(app: Path, backup: Path, items: list[str]) -> None:
    """Ancien programme remis en place (mise à jour ratée)."""
    for item in reversed(items):
        if (backup / item).exists():
            _retry(_remove, app / item)
            _retry(Path.replace, backup / item, app / item)


def stop(app: Path, scripts: Path) -> int:
    """Service puis gateway, chacun seul (arret-maj.py) : ce script, lancé par la gateway, n'est pas arrêté avec elle."""
    spec = importlib.util.spec_from_file_location("arret_maj", scripts / "arret-maj.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(app)


def port(app: Path) -> int:
    try:
        for line in (app / ".env").read_text(encoding="utf-8-sig").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "GATEWAY_PORT":
                return int(value.strip().strip("\"'"))
    except (OSError, ValueError):
        pass
    return 8000


def healthy(p: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{p}/health", timeout=3) as r:  # noqa: S310 (adresse locale)
            return r.status == 200
    except OSError:
        return False


def start(app: Path) -> None:
    """Service dans sa fenêtre « Lezard IA », réduite (comme après une mise à jour par l'installateur)."""
    info = subprocess.STARTUPINFO()
    info.dwFlags, info.wShowWindow = subprocess.STARTF_USESHOWWINDOW, 7  # SW_SHOWMINNOACTIVE
    subprocess.Popen(
        [str(app / "python" / "python.exe"), str(app / "lezard.py"), "service"],
        cwd=app,
        creationflags=CREATE_NEW_CONSOLE,
        startupinfo=info,
        close_fds=True,
    )


def lezard(app: Path, *args: str, timeout: float = 1800) -> int:
    """Commande du lanceur de l'installation (ex. pack refresh), sans fenêtre."""
    try:
        return subprocess.run(
            [str(app / "python" / "python.exe"), str(app / "lezard.py"), *args],
            cwd=app,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def wait_healthy(p: int, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if healthy(p):
            return True
        time.sleep(2)
    return False


def main(app: Path, new: Path, scripts: Path | None = None, wait: float = WAIT_HEALTHY) -> int:
    updates = app / "data" / "updates"
    logs = app / "data" / "logs"
    backup = updates / "ancienne"
    version = (new / "version.txt").read_text(encoding="utf-8").strip() if (new / "version.txt").exists() else "?"

    def say(text: str) -> None:
        print(text)
        try:
            logs.mkdir(parents=True, exist_ok=True)
            with open(logs / "mise-a-jour.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
        except OSError:
            pass

    def result(ok: bool, message: str) -> int:
        say(message)
        try:
            (updates / "resultat.json").write_text(
                json.dumps({"ok": ok, "version": version, "message": message, "at": time.time()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
        return 0 if ok else 1

    say(f"mise à jour vers {version} : {new}")
    time.sleep(2)  # la gateway répond d'abord au bouton « Mettre à jour »
    if stop(app, scripts or new) != 0:
        start(app)
        return result(False, "Lezard IA ne s'est pas arrêté : mise à jour annulée, rien n'a été changé.")
    try:
        items = swap(app, new, backup)
    except OSError as e:
        start(app)
        return result(False, f"Fichiers impossibles à remplacer ({e}) : l'ancienne version est gardée.")
    say(f"nouveau programme en place ({len(items)} éléments)")
    if (app / "packs").is_dir() and any(p.is_dir() and not p.name.startswith(".") for p in (app / "packs").iterdir()):
        say(f"composants installés : mise à jour (code {lezard(app, 'pack', 'refresh')})")
    if not any(app.glob("unins*.exe")):
        lezard(app, "register", timeout=60)  # version affichée dans « Applications installées »
    start(app)
    p = port(app)
    if wait_healthy(p, wait):
        shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(new.parent, ignore_errors=True)
        return result(True, f"Lezard IA {version} installé et redémarré")
    say("la nouvelle version ne répond pas : retour à l'ancienne")
    stop(app, scripts or new)
    try:
        restore(app, backup, items)
    except OSError as e:
        say(f"retour impossible : {e!r}")
    start(app)
    ok = wait_healthy(p, wait)
    shutil.rmtree(backup, ignore_errors=True)
    shutil.rmtree(new.parent, ignore_errors=True)
    return result(
        False, "Échec de la mise à jour : ancienne version remise" + ("" if ok else ", qui ne répond pas non plus")
    )


if __name__ == "__main__":
    try:
        sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2]), Path(__file__).resolve().parent))
    except Exception:  # jamais muet : l'erreur dans le journal, pour comprendre une mise à jour ratée
        import traceback

        logs = Path(sys.argv[1]) / "data" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "mise-a-jour.log", "a", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        raise
