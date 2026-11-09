"""LibreChat en un clic, sans Docker : python librechat_setup.py <étiquette> <dossier>

Étape d'une recette de installers.py (lancée comme un script : aucune dépendance au paquet gateway). Ensuite
Lezard IA le démarre lui-même (librechat_run.py : sa base MongoDB puis son serveur Node.js).
1. Le dépôt officiel, cloné à l'étiquette figée (gardé s'il est déjà là : réinstaller ne le recopie pas).
2. .env créé depuis .env.example, avec les secrets que l'exemple laisse vides tirés au hasard pour ce PC ; un .env
   existant est gardé (ses secrets chiffrent les données déjà enregistrées). Réglages pour tourner sur ce PC : base
   MongoDB locale, recherche Meilisearch coupée (LibreChat s'en passe), écoute sur ce PC seulement.
3. MongoDB Community (serveur de base de données) : l'archive officielle de MongoDB, version, taille et empreinte
   SHA-256 figées ; seul mongod.exe est gardé, dans <dossier>/mongodb.
4. npm ci puis npm run frontend : dépendances (versions du package-lock.json du projet) et interface construites
   sur ce PC (Node.js 24).
"""

import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = "https://github.com/LibreChat-AI/LibreChat.git"
MONGO_URL = "https://fastdl.mongodb.org/windows/mongodb-windows-x86_64-8.0.34.zip"  # publiée le 2026-10-08
MONGO_SHA256 = "6b021a0864494e4decf278b36477a29eb36072432773e4e62c2f42c302630db0"
MONGO_SIZE = 807616685
MONGO_PORT = 27018  # pas celui d'un MongoDB déjà installé (27017)
SECRETS = {  # nom -> octets tirés au hasard (affichés en hexadécimal)
    "CREDS_KEY": 32,
    "CREDS_IV": 16,
    "JWT_SECRET": 32,
    "JWT_REFRESH_SECRET": 32,
    "MEILI_MASTER_KEY": 32,
    "ADMIN_PANEL_SESSION_SECRET": 32,
}
NATIVE = {  # réglages d'une installation sur ce PC (sans conteneurs)
    "HOST": "127.0.0.1",
    "PORT": "3080",
    "MONGO_URI": f"mongodb://127.0.0.1:{MONGO_PORT}/LibreChat",
    "DOMAIN_CLIENT": "http://localhost:3080",
    "DOMAIN_SERVER": "http://localhost:3080",
    "SEARCH": "false",
    "NO_INDEX": "true",
    "ENDPOINTS": "custom,agents",  # Lezard IA (librechat.yaml) et les agents ; pas les fournisseurs à clé à part
}


def env_text(example: str) -> str:
    """Le .env de ce PC : l'exemple, secrets vides remplis au hasard, réglages pour tourner sans Docker."""
    out = example
    for name, size in SECRETS.items():
        out, n = re.subn(rf"^{name}=\s*$", f"{name}={secrets.token_hex(size)}", out, flags=re.M)
        if not n and not re.search(rf"^{name}=", out, flags=re.M):
            out += f"\n{name}={secrets.token_hex(size)}"
    return native(out)


def native(text: str) -> str:
    """Réglages NATIVE posés (remplacés, ou ajoutés s'ils manquent) ; le reste du .env est gardé."""
    for name, value in NATIVE.items():
        text, n = re.subn(rf"^#?\s*{name}=.*$", f"{name}={value}", text, count=1, flags=re.M)
        if not n:
            text = text.rstrip("\n") + f"\n{name}={value}"
    return text if text.endswith("\n") else text + "\n"


def mongo(root: Path) -> int:
    target = root / "mongodb" / "mongod.exe"
    if target.exists():
        print("MongoDB déjà là.", flush=True)
        return 0
    if sys.platform != "win32":
        print("MongoDB : Windows seulement ici.")
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "mongodb.zip"
        digest, got, shown = hashlib.sha256(), 0, 0
        print(f"Téléchargement de MongoDB 8.0.34 ({MONGO_SIZE / 1e6:.0f} Mo, site officiel de MongoDB)…", flush=True)
        with urllib.request.urlopen(MONGO_URL, timeout=60) as r, dest.open("wb") as f:  # noqa: S310 (adresse figée)
            while chunk := r.read(1 << 20):
                got += len(chunk)
                if got > MONGO_SIZE:
                    break
                digest.update(chunk)
                f.write(chunk)
                if got * 10 // MONGO_SIZE > shown:
                    shown = got * 10 // MONGO_SIZE
                    print(f"  {shown * 10} %", flush=True)
        if got != MONGO_SIZE or digest.hexdigest() != MONGO_SHA256:
            print("Taille ou empreinte de MongoDB inattendue : fichier supprimé, rien n'est installé.")
            return 3
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest) as z:
            for member in z.namelist():  # mongod.exe et les licences, rien d'autre (ni hors du dossier)
                name = member.rsplit("/", 1)[-1]
                if name in ("mongod.exe", "LICENSE-Community.txt", "THIRD-PARTY-NOTICES"):
                    with z.open(member) as src, (target.parent / name).open("wb") as out:
                        shutil.copyfileobj(src, out)
    print("MongoDB installé (mongod.exe seul).", flush=True)
    return 0


def build(root: Path) -> int:
    """Dépendances et interface, une fois par version (réinstaller la même version ne refait rien)."""
    stamp = root / ".lezard-build"
    tag = (root / ".lezard-tag").read_text(encoding="utf-8").strip() if (root / ".lezard-tag").exists() else ""
    if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == tag and (root / "client" / "dist").is_dir():
        print("LibreChat déjà construit.", flush=True)
        return 0
    npm = shutil.which("npm")
    if not npm:
        print("npm introuvable : installez Node.js 24.")
        return 2
    env = {k: v for k, v in os.environ.items() if k.upper() != "ELECTRON_RUN_AS_NODE"}
    env.update(SCARF_ANALYTICS="false", DO_NOT_TRACK="1", NODE_OPTIONS="--max-old-space-size=6144")
    for args, what in ((["ci", "--no-audit", "--no-fund"], "dépendances"), (["run", "frontend"], "interface")):
        print(f"LibreChat : {what} (npm {' '.join(args)}, quelques minutes)…", flush=True)
        code = subprocess.run([npm, *args], cwd=root, env=env, check=False).returncode
        if code:
            print(f"npm {' '.join(args)} a échoué (code {code}).")
            return code
    stamp.write_text(tag, encoding="utf-8")
    return 0


def main(tag: str, folder: str) -> int:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag) or not re.fullmatch(r"[\w-]+", folder):
        print(f"arguments refusés : {tag} {folder}")
        return 2
    root = Path.cwd() / folder
    if not (root / "package.json").is_file():
        print(f"Téléchargement de LibreChat {tag}…", flush=True)
        clone = root.with_name(root.name + ".telechargement")  # le dossier peut garder des conversations : à côté
        shutil.rmtree(clone, ignore_errors=True)
        code = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", tag, REPO, str(clone)], check=False
        ).returncode
        if code:
            shutil.rmtree(clone, ignore_errors=True)
            return code
        root.mkdir(exist_ok=True)
        for item in clone.iterdir():
            if not (root / item.name).exists():
                item.rename(root / item.name)
        shutil.rmtree(clone, ignore_errors=True)
        (root / ".lezard-tag").write_text(tag, encoding="utf-8")
    else:
        print("LibreChat déjà téléchargé : dossier gardé.", flush=True)
    if not (root / ".lezard-tag").exists():
        (root / ".lezard-tag").write_text(tag, encoding="utf-8")
    env = root / ".env"
    if env.exists():
        env.write_text(native(env.read_text(encoding="utf-8")), encoding="utf-8")
        print(".env existant gardé (ses secrets protègent les données déjà enregistrées), réglé pour ce PC.")
    else:
        env.write_text(env_text((root / ".env.example").read_text(encoding="utf-8")), encoding="utf-8")
        print(".env créé, avec des secrets tirés au hasard pour ce PC.")
    return mongo(root) or build(root)


KEEP = {"data-db", ".env", "librechat.yaml", "uploads", "logs"}  # conversations, secrets, réglages, fichiers


def remove(folder: str) -> int:
    """Désinstallation : programme, dépendances et MongoDB supprimés ; conversations (data-db), secrets (.env, qui
    les déchiffrent), réglages et fichiers envoyés gardés : une réinstallation les retrouve."""
    if not re.fullmatch(r"[\w-]+", folder or ""):
        print(f"nom de dossier refusé : {folder!r}")
        return 2
    root = Path.cwd() / folder
    if not root.is_dir():
        print("Déjà absent.")
        return 0
    failed = []
    for item in root.iterdir():
        if item.name in KEEP:
            continue
        try:
            shutil.rmtree(item, onexc=_writable) if item.is_dir() else item.unlink()
        except OSError:
            failed.append(item.name)
    if failed:
        print(f"Non supprimés (encore ouverts ?) : {', '.join(failed)}")
        return 4
    print(f"LibreChat désinstallé ; vos conversations et réglages restent dans {root}.")
    return 0


def _writable(func, path, _exc) -> None:
    """Fichiers de git en lecture seule (.git/objects) : rendus modifiables, puis supprimés."""
    os.chmod(path, 0o700)
    func(path)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--remove"]:
        sys.exit(remove(*sys.argv[2:3]))
    sys.exit(main(*sys.argv[1:]))
