r"""Zip de Lezard IA (installation sans installateur), construit par build.ps1 à partir du même dossier que l'installateur.

    python installer\zip.py <build\stage> <version> <dossier de sortie>   ->   LezardIA-<version>.zip

Contenu, dans un dossier « Lezard IA » : le programme de l'installateur (python\, gateway\, lezard.py,
packs\*.json), « Installer Lezard IA.cmd » (double-clic après extraction), arret-maj.py et mise_a_jour.py (mise à jour
depuis le zip), version.txt. Dates fixes et ordre trié : même contenu, même zip.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOP = "Lezard IA"
DATE = (2026, 1, 1, 0, 0, 0)
EXTRA = {
    "Installer Lezard IA.cmd": HERE / "app" / "Installer Lezard IA.cmd",
    "mise_a_jour.py": HERE / "app" / "mise_a_jour.py",
    "arret-maj.py": HERE / "arret-maj.py",
}


def build(stage: Path, version: str, out: Path) -> Path:
    files = {p.relative_to(stage).as_posix(): p for p in stage.rglob("*") if p.is_file()}
    files.update(EXTRA)
    target = out / f"LezardIA-{version}.zip"
    part = target.with_suffix(".part")
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(f"{TOP}/{name}", DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, files[name].read_bytes())
        z.writestr(zipfile.ZipInfo(f"{TOP}/version.txt", DATE), version + "\n", zipfile.ZIP_DEFLATED)
    part.replace(target)
    return target


if __name__ == "__main__":
    print(build(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])))
