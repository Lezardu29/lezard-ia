"""Installateur officiel téléchargé puis lancé sans question : python setup_download.py <url> <sha256> <taille> [args…]

Étape d'une recette de installers.py (lancée comme un script : aucune dépendance au paquet gateway). Seules les
adresses de GitHub sont acceptées ; un fichier dont la taille ou l'empreinte SHA-256 diffère de celle figée dans la
recette est supprimé, jamais lancé. L'installateur s'installe lui-même au bon endroit (Programmes, menu Démarrer),
puis est effacé : rien à déplacer. Une archive .zip (application sans installateur) est décompressée dans
%LOCALAPPDATA%/Programs/<nom>, le nom étant le premier argument ; « --remove <nom> » supprime ce dossier.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ALLOWED = "https://github.com/"


def main(url: str, sha256: str, size: str, *args: str) -> int:
    if not url.startswith(ALLOWED):
        print(f"adresse refusée : {url}")
        return 2
    expected = int(size)
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / url.rsplit("/", 1)[-1]
        digest, got, shown = hashlib.sha256(), 0, 0
        print(f"Téléchargement de {dest.name} ({expected / 1e6:.0f} Mo)…", flush=True)
        with urllib.request.urlopen(url, timeout=60) as r, dest.open("wb") as f:  # noqa: S310 (adresse vérifiée)
            while chunk := r.read(1 << 20):
                got += len(chunk)
                if got > expected:
                    break
                digest.update(chunk)
                f.write(chunk)
                if got * 10 // expected > shown:
                    shown = got * 10 // expected
                    print(f"  {shown * 10} %", flush=True)
        if got != expected or digest.hexdigest() != sha256:
            print("Taille ou empreinte inattendue : fichier supprimé, rien n'est installé.")
            return 3
        if dest.suffix == ".zip":
            return unzip(dest, *args)
        print("Empreinte vérifiée. Installation…", flush=True)
        code = subprocess.run([str(dest), *args], check=False).returncode
        print("Installé." if code == 0 else f"L'installateur a échoué (code {code}).")
        return code


def folder(name: str) -> Path | None:
    if not name or not name.isalnum():
        print(f"nom de dossier refusé : {name!r}")
        return None
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Programs" / name


def remove(name: str) -> int:
    """Désinstallation d'une application décompressée : son dossier, et rien d'autre."""
    target = folder(name)
    if target is None:
        return 2
    if target.exists():
        shutil.rmtree(target)
        print(f"Dossier supprimé : {target}")
    else:
        print("Déjà absent.")
    return 0


def unzip(archive: Path, name: str = "") -> int:
    target = folder(name)
    if target is None:
        return 2
    print(f"Empreinte vérifiée. Décompression dans {target}…", flush=True)
    with zipfile.ZipFile(archive) as z:
        for member in z.namelist():  # rien hors du dossier cible (chemins absolus ou « .. »)
            if not (target / member).resolve().is_relative_to(target.resolve()):
                print(f"chemin refusé dans l'archive : {member}")
                return 3
        z.extractall(target)
    print("Installé.")
    return 0


UNINSTALL_KEYS = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"


def _registered(name: str) -> list[dict]:
    """Programmes installés dont le nom affiché par Windows est `name` (Paramètres > Applications)."""
    import winreg

    found = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            base = winreg.OpenKey(root, UNINSTALL_KEYS)
        except OSError:
            continue
        with base:
            for i in range(winreg.QueryInfoKey(base)[0]):
                try:
                    with winreg.OpenKey(base, winreg.EnumKey(base, i)) as k:
                        values = {
                            n: winreg.QueryValueEx(k, n)[0]
                            for n in ("DisplayName", "UninstallString", "QuietUninstallString")
                            if _has(k, n)
                        }
                except OSError:
                    continue
                shown = str(values.get("DisplayName", "")).strip().lower()
                # le nom, éventuellement suivi de sa version (« AnythingLLM 1.18.0 »)
                if shown == name.lower() or re.fullmatch(re.escape(name.lower()) + r" v?[\d.]+", shown):
                    found.append(values)
    return found


def _has(key, name: str) -> bool:
    import winreg

    try:
        winreg.QueryValueEx(key, name)
        return True
    except OSError:
        return False


def uninstall(name: str) -> int:
    """Désinstallation sans question par le désinstallateur de l'application (celui de Paramètres > Applications) :
    sa commande silencieuse, sinon la commande normale avec /S (installateurs NSIS : Tauri, Electron)."""
    if sys.platform != "win32":
        return 0
    entries = _registered(name)
    if not entries:
        print("Déjà retiré.")
        return 0
    for entry in entries:
        command = entry.get("QuietUninstallString") or f"{entry.get('UninstallString', '')} /S"
        print(f"Désinstallation de {name}…", flush=True)
        subprocess.run(command, check=False)  # chaîne lue dans le registre de Windows, comme Paramètres le fait
    for _ in range(120):  # le désinstallateur NSIS se recopie et rend la main tout de suite : on attend qu'il ait fini
        if not _registered(name):
            print("Désinstallé.")
            return 0
        time.sleep(1)
    print("Le désinstallateur n'a pas terminé en 2 minutes : finissez depuis Paramètres > Applications.")
    return 4


if __name__ == "__main__":
    if sys.argv[1:2] == ["--remove"]:
        sys.exit(remove(*sys.argv[2:3]))
    if sys.argv[1:2] == ["--uninstall"]:
        sys.exit(uninstall(" ".join(sys.argv[2:])))
    sys.exit(main(*sys.argv[1:]))
