"""Installateur officiel téléchargé puis lancé sans question : python setup_download.py <url> <sha256> <taille> [args…]

Étape d'une recette de installers.py (lancée comme un script : aucune dépendance au paquet gateway). Seules les
adresses de GitHub sont acceptées ; un fichier dont la taille ou l'empreinte SHA-256 diffère de celle figée dans la
recette est supprimé, jamais lancé. L'installateur s'installe lui-même au bon endroit (Programmes, menu Démarrer),
puis est effacé : rien à déplacer. Une archive .zip (application sans installateur) est décompressée dans
%LOCALAPPDATA%/Programs/<nom>, le nom étant le premier argument.
"""

import hashlib
import os
import subprocess
import sys
import tempfile
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


def unzip(archive: Path, name: str = "") -> int:
    if not name or not name.isalnum():
        print(f"nom de dossier refusé : {name!r}")
        return 2
    target = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Programs" / name
    print(f"Empreinte vérifiée. Décompression dans {target}…", flush=True)
    with zipfile.ZipFile(archive) as z:
        for member in z.namelist():  # rien hors du dossier cible (chemins absolus ou « .. »)
            if not (target / member).resolve().is_relative_to(target.resolve()):
                print(f"chemin refusé dans l'archive : {member}")
                return 3
        z.extractall(target)
    print("Installé.")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
