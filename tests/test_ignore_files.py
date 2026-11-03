"""Fichiers générés et secrets exclus du dépôt et de l'image Docker (11.4).

git n'étant pas forcément installé, les règles sont vérifiées avec un interpréteur minimal des motifs
(sous-ensemble utilisé : nom, *.ext, dossier/, **/, négation !)."""

from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).parent.parent

SECRETS = [
    ".env",
    "deploy/.env",
    ".env.local",
    "providers.json",
    "data/gateway.db",
    "data/gateway.db-wal",
    "data/gateway.db-shm",
    "data/encryption.key",
    "data/logs/stripe.log",
    "data/tls/ca.key",
    "data/tls/server.key",
    "data/tls/ca.crt",
    # hors de data/ (GATEWAY_DB, GATEWAY_TLS_DIR, GATEWAY_ENCRYPTION_KEY_FILE déplacés) : filet de sécurité
    "gateway.db",
    "backup/gateway.db-wal",
    "certs/server.key",
    "certs/fullchain.pem",
    "lancer.log",
]
GENERATED = [".pytest_cache/v/cache/nodeids", "gateway/__pycache__/main.cpython-313.pyc"]
KEPT = [
    "gateway/main.py",
    "deploy/.env.example",
    "requirements.txt",
    "Dockerfile",
    "lancer.ps1",
    "gateway/web/static/models.js",
    "tests/test_gateway.py",
    "ROADMAP_CORRECTIFS.xml",
]


def rules(name: str) -> list[str]:
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.lstrip().startswith("#")]


def git_ignored(path: str, patterns: list[str]) -> bool:
    parts, ignored = path.split("/"), False
    for raw in patterns:
        neg, p = raw.startswith("!"), raw.lstrip("!")
        dir_only, p = p.endswith("/"), p.rstrip("/")
        if "/" in p:  # motif ancré à la racine
            hit = any(_segments(p.split("/"), parts[:i]) for i in range(1, len(parts) + 1))
        else:  # nom à n'importe quel niveau
            names = parts[:-1] if dir_only else parts
            hit = any(fnmatchcase(n, p) for n in names)
        if hit:
            ignored = not neg
    return ignored


def _segments(pat: list[str], parts: list[str]) -> bool:
    """Comparaison dossier par dossier, comme Docker (Go filepath.Match) : `*` ne traverse pas `/`, `**` oui."""
    if not pat:
        return not parts
    if pat[0] == "**":
        return any(_segments(pat[1:], parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatchcase(parts[0], pat[0]) and _segments(pat[1:], parts[1:])


def docker_ignored(path: str, patterns: list[str]) -> bool:
    parts, ignored = path.split("/"), False
    for raw in patterns:
        neg, p = raw.startswith("!"), raw.lstrip("!")
        # ancré à la racine du contexte ; un dossier exclu exclut tout son contenu
        hit = any(_segments(p.split("/"), parts[:i]) for i in range(1, len(parts) + 1))
        if hit:
            ignored = not neg
    return ignored


def test_gitignore_excludes_secrets_and_generated_files():
    pats = rules(".gitignore")
    for path in SECRETS + GENERATED:
        assert git_ignored(path, pats), path
    for path in KEPT:
        assert not git_ignored(path, pats), path


def test_dockerignore_keeps_secrets_out_of_the_build_context():
    pats = rules(".dockerignore")
    for path in SECRETS + GENERATED:
        assert docker_ignored(path, pats), path
    for path in ["tests/test_gateway.py", "requirements-dev.txt"]:
        assert docker_ignored(path, pats), path
    for path in ["gateway/main.py", "requirements.txt", "gateway/web/static/models.js", "deploy/.env.example"]:
        assert not docker_ignored(path, pats), path


def test_secret_files_on_disk_are_all_covered():
    """Ce qui existe réellement sur cette machine (base, clé, journaux…) est bien exclu."""
    pats = rules(".gitignore")
    found = [p.relative_to(ROOT).as_posix() for p in (ROOT / "data").rglob("*") if p.is_file()]
    found += [n for n in (".env", "providers.json") if (ROOT / n).exists()]
    for path in found:
        assert git_ignored(path, pats), path
