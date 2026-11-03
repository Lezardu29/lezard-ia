"""Fige les dépendances de l'installateur (32.1) : la base (requirements.lock) et les packs facultatifs (packs/*.json).

    python installer/lock.py         (ou : .\\installer\\build.ps1 -UpdateLock)

Une seule résolution, commune à la base et à tous les packs, pour la cible de l'installateur (Windows 64 bits,
CPython 3.13, roues seulement), quelle que soit la version de Python qui lance ce script : un pack ajouté plus tard
ne remplace jamais une version de la base (ex. CrewAI impose une version de pydantic, la base la prend aussi).

- requirements.lock : la base, embarquée dans l'installateur (build.ps1 : pip --require-hashes).
- packs/<pack>.json : les roues qui manquent à la base pour ce pack, avec leur adresse officielle (PyPI), leur
  taille et leur empreinte SHA-256. Rien n'est embarqué : lezard.py les télécharge seulement si le client a coché
  le pack, vérifie chaque empreinte, puis les décompresse dans packs\\<pack>. Ce qui n'est pas choisi ne prend pas
  de place.
Après une mise à jour, lancez la suite de tests avec ces versions : .\\installer\\build.ps1 -Test
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TARGET = ["--only-binary=:all:", "--platform", "win_amd64", "--python-version", "3.13", "--implementation", "cp"]
# compteurs partagés entre plusieurs gateways (déploiement serveur) : jamais importé sur un PC
EXCLUDE = {"redis"}
PACKS = {
    "voix": ("requirements-local.txt", "Dictée et lecture vocales sur ce PC (faster-whisper, Kokoro)"),
    "multi": ("requirements-multi.txt", "Multi-agents CrewAI pour les gros modèles"),
}
NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirements(path: Path) -> list[str]:
    """Lignes de dépendances d'un fichier requirements (sans commentaires, inclusions ni paquets exclus)."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        m = NAME.match(line)
        if line and not line.startswith("-") and m and norm(m.group(1)) not in EXCLUDE:
            out.append(line)
    return out


def resolve(reqs: list[str], tmp: Path, constraints: Path | None = None) -> list[dict]:
    report, req = tmp / "report.json", tmp / "req.txt"
    req.write_text("\n".join(reqs) + "\n", encoding="utf-8")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--quiet",
         "--disable-pip-version-check", "--report", str(report), *TARGET, "--target", str(tmp / "t"),
         "-r", str(req), *(["-c", str(constraints)] if constraints else [])],
        check=True,
    )  # fmt: skip
    return json.loads(report.read_text(encoding="utf-8"))["install"]


def size(url: str) -> int:
    with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as r:
        return int(r.headers["Content-Length"])


def ollama_release(min_age_days: int = 14) -> dict:
    """Installateur officiel d'Ollama : la version publiée la plus récente qui a au moins `min_age_days` jours (les
    toutes dernières sont parfois retirées), avec l'empreinte SHA-256 publiée par GitHub. Ollama se met ensuite à
    jour lui-même."""
    import datetime as dt

    api = "https://api.github.com/repos/ollama/ollama/releases?per_page=50"
    with urllib.request.urlopen(urllib.request.Request(api, headers={"Accept": "application/vnd.github+json"})) as r:
        releases = json.load(r)
    limit = dt.datetime.now(dt.UTC) - dt.timedelta(days=min_age_days)
    for rel in releases:
        published = dt.datetime.fromisoformat(rel["published_at"].replace("Z", "+00:00"))
        asset = next((a for a in rel["assets"] if a["name"] == "OllamaSetup.exe"), None)
        if rel["draft"] or rel["prerelease"] or published > limit or not asset or not asset.get("digest"):
            continue
        return {
            "version": rel["tag_name"].lstrip("v"),
            "url": asset["browser_download_url"],
            "sha256": asset["digest"].removeprefix("sha256:"),
            "size": asset["size"],
        }
    raise SystemExit("aucune version d'Ollama assez ancienne avec une empreinte publiée")


def main() -> int:
    base_reqs = requirements(ROOT / "requirements.txt")
    pack_reqs = {p: requirements(ROOT / f) for p, (f, _) in PACKS.items()}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        joint = resolve(base_reqs + [r for reqs in pack_reqs.values() for r in reqs], tmp)
        constraints = tmp / "constraints.txt"
        constraints.write_text(
            "\n".join(f"{i['metadata']['name']}=={i['metadata']['version']}" for i in joint) + "\n", encoding="utf-8"
        )
        base = resolve(base_reqs, tmp, constraints)
        packs = {p: resolve(base_reqs + reqs, tmp, constraints) for p, reqs in pack_reqs.items()}

    lines = [
        "# Dépendances embarquées par l'installateur Windows (32.1), générées par installer/lock.py depuis",
        "# requirements.txt — ne pas modifier à la main. Cible : win_amd64, CPython 3.13, roues seulement.",
    ]
    for item in sorted(base, key=lambda i: i["metadata"]["name"].lower()):
        meta, digest = item["metadata"], item["download_info"]["archive_info"]["hashes"]["sha256"]
        lines.append(f"{meta['name']}=={meta['version']} \\\n    --hash=sha256:{digest}")
    (HERE / "requirements.lock").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"requirements.lock : {len(base)} paquets")

    in_base = {norm(i["metadata"]["name"]) for i in base}
    (HERE / "packs").mkdir(exist_ok=True)
    for p, items in packs.items():
        wheels = [
            {
                "name": i["metadata"]["name"],
                "version": i["metadata"]["version"],
                "url": i["download_info"]["url"],
                "sha256": i["download_info"]["archive_info"]["hashes"]["sha256"],
                "size": size(i["download_info"]["url"]),
            }
            for i in sorted(items, key=lambda i: i["metadata"]["name"].lower())
            if norm(i["metadata"]["name"]) not in in_base
        ]
        manifest = {"pack": p, "label": PACKS[p][1], "wheels": wheels}
        (HERE / "packs" / f"{p}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", "utf-8")
        print(f"packs/{p}.json : {len(wheels)} roues, {sum(w['size'] for w in wheels) / 1e6:.0f} Mo à télécharger")
    ollama = {"pack": "ollama", "label": "Ollama, pour faire tourner les modèles d'IA sur ce PC", **ollama_release()}
    (HERE / "packs" / "ollama.json").write_text(json.dumps(ollama, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(f"packs/ollama.json : Ollama {ollama['version']}, {ollama['size'] / 1e6:.0f} Mo à télécharger")
    return 0


if __name__ == "__main__":
    sys.exit(main())
