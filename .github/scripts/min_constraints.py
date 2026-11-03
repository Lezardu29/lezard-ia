"""Contraintes pip aux versions MINIMALES déclarées (15.2, reprend la vérification manuelle de 10.9).

Lit les bornes basses (>=) de requirements.txt et requirements-dev.txt (en suivant les -r) et écrit un fichier
de contraintes « paquet==borne_basse », à passer à pip avec -c :

    python .github/scripts/min_constraints.py > constraints-min.txt
    python -m pip install -r requirements-dev.txt -c constraints-min.txt

Échoue si une dépendance n'a pas de borne basse : sans elle, le test « versions minimales » ne teste rien.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$")


def lower_bounds(path: Path, seen: set[Path]) -> dict[str, str]:
    path = path.resolve()
    if path in seen:
        return {}
    seen.add(path)
    bounds: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ")):
            bounds.update(lower_bounds(path.parent / line.split(None, 1)[1], seen))
            continue
        match = REQ.match(line)
        if not match:
            raise SystemExit(f"{path.name} : ligne non reconnue : {raw!r}")
        name, _extras, spec = match.groups()
        low = re.search(r">=\s*([^,;\s]+)", spec)
        if not low:
            raise SystemExit(f"{path.name} : {name} n'a pas de borne basse (>=)")
        bounds[name.lower()] = low.group(1)
    return bounds


if __name__ == "__main__":
    for name, version in sorted(lower_bounds(ROOT / "requirements-dev.txt", set()).items()):
        sys.stdout.write(f"{name}=={version}\n")
