"""Répare un outil npm installé sans ses scripts (npm install --global --ignore-scripts) : ses modules natifs.

Certains outils de la marketplace (Claude Code Router, CloudCLI) dépendent de better-sqlite3 12, qui n'a pas de
version toute prête pour Node.js 24 sous Windows : npm essaie alors de le compiler, ce qui demande Visual Studio, et
toute l'installation échoue. Ici :
1. better-sqlite3 est remplacé par une version qui a sa version toute prête (BETTER_SQLITE3), installée à part puis
   copiée à sa place dans l'outil ;
2. les autres modules natifs de l'outil sont reconstruits (npm rebuild : ils ont leurs versions toutes prêtes) ;
3. le script d'installation de l'outil lui-même est lancé, s'il en a un.

    python -m gateway.npm_native <paquet> [commande]     ex. @cloudcli-ai/cloudcli cloudcli
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BETTER_SQLITE3 = "13.0.3"  # version toute prête pour Node.js 24 sous Windows (la 12.11.1 n'en a pas)
SCRIPTS = ("preinstall", "install", "postinstall")


def npm() -> str:
    found = shutil.which("npm.cmd") or shutil.which("npm")
    if not found:
        raise SystemExit("npm introuvable : installez Node.js.")
    return found


def run(argv: list[str], cwd: Path | None = None) -> None:
    print(">", " ".join(Path(a).name if Path(a).is_absolute() else a for a in argv), flush=True)
    subprocess.run(argv, cwd=cwd, check=True)


def native_packages(root: Path) -> list[tuple[Path, dict]]:
    """Modules de l'outil qui ont un script d'installation (dossier, package.json)."""
    out = []
    for pj in root.rglob("package.json"):
        up = pj.parent.parent
        if up.name != "node_modules" and not (up.name.startswith("@") and up.parent.name == "node_modules"):
            continue  # pas un module (fichier de test, exemple…)
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if any((data.get("scripts") or {}).get(s) for s in SCRIPTS) and data.get("name"):
            out.append((pj.parent, data))
    return out


def global_root(command: str | None) -> Path:
    """Dossier node_modules des outils npm globaux : à côté de la commande de l'outil (npm root --global masque
    parfois une partie du chemin qu'il prend pour un secret), sinon celui que donne npm."""
    found = command and (shutil.which(f"{command}.cmd") or shutil.which(command))
    if found:
        return Path(found).parent / "node_modules"
    return Path(subprocess.run([npm(), "root", "--global"], capture_output=True, text=True, check=True).stdout.strip())


def repair(package: str, command: str | None = None) -> None:
    tool = global_root(command) / package
    if not (tool / "package.json").is_file():
        raise SystemExit(f"{package} n'est pas installé (npm install --global).")
    natives = native_packages(tool)
    sqlite = [d for d, data in natives if data["name"] == "better-sqlite3"]
    if sqlite:
        with tempfile.TemporaryDirectory(prefix="lezard-npm-") as tmp:
            run([npm(), "install", "--prefix", tmp, "--no-fund", "--no-audit", f"better-sqlite3@{BETTER_SQLITE3}"])
            fresh = Path(tmp) / "node_modules" / "better-sqlite3"
            for target in sqlite:
                shutil.rmtree(target)
                shutil.copytree(fresh, target)
                print(f"better-sqlite3 {BETTER_SQLITE3} (version toute prête) : {target.relative_to(tool)}", flush=True)
    others = sorted({data["name"] for d, data in natives if data["name"] != "better-sqlite3" and d != tool})
    if others:
        run([npm(), "rebuild", "--foreground-scripts", *others], cwd=tool)
    own = json.loads((tool / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
    for script in SCRIPTS:
        if own.get(script):
            run([npm(), "run", script], cwd=tool)
    print(f"{package} : modules natifs prêts.", flush=True)


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        raise SystemExit(__doc__)
    repair(*sys.argv[1:])
