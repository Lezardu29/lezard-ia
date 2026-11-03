"""LibreChat en un clic : python librechat_setup.py <étiquette> <dossier>

Étape d'une recette de installers.py (lancée comme un script : aucune dépendance au paquet gateway). Ensuite la
recette lance docker compose dans le dossier.
1. Le dépôt officiel, cloné à l'étiquette figée (gardé s'il est déjà là : réinstaller ne le recopie pas).
2. .env créé depuis .env.example, avec les secrets que l'exemple laisse vides tirés au hasard pour ce PC ; un .env
   existant est gardé tel quel (ses secrets chiffrent les données déjà enregistrées).
3. docker-compose.override.yml : images à version figée (le fichier du projet suit « latest ») et panneau
   d'administration non lancé (image sans version publiée ; il reste disponible avec le profil « admin »).
"""

import re
import secrets
import subprocess
import sys
from pathlib import Path

REPO = "https://github.com/LibreChat-AI/LibreChat.git"
RAG_TAG = "v0.9.0"  # librechat-rag-api-dev-lite, publiée le 2026-07-31
SECRETS = {  # nom -> octets tirés au hasard (affichés en hexadécimal)
    "CREDS_KEY": 32,
    "CREDS_IV": 16,
    "JWT_SECRET": 32,
    "JWT_REFRESH_SECRET": 32,
    "MEILI_MASTER_KEY": 32,
    "ADMIN_PANEL_SESSION_SECRET": 32,
}
OVERRIDE = """# Écrit par Lezard IA : images à version figée, panneau d'administration seulement avec --profile admin.
services:
  api:
    image: registry.librechat.ai/librechat-ai/librechat:{tag}
  rag_api:
    image: registry.librechat.ai/librechat-ai/librechat-rag-api-dev-lite:{rag}
  admin-panel:
    profiles: ["admin"]
"""


def env_text(example: str) -> str:
    """Le .env de ce PC : l'exemple, secrets vides remplis au hasard, utilisateur des conteneurs fixé (1000)."""
    out = example
    for name, size in SECRETS.items():
        out, n = re.subn(rf"^{name}=\s*$", f"{name}={secrets.token_hex(size)}", out, flags=re.M)
        if not n and not re.search(rf"^{name}=", out, flags=re.M):
            out += f"\n{name}={secrets.token_hex(size)}"
    for name in ("UID", "GID"):  # « user: ${UID}:${GID} » dans docker-compose.yml : vide, Docker refuse
        if not re.search(rf"^{name}=", out, flags=re.M):
            out += f"\n{name}=1000"
    return out if out.endswith("\n") else out + "\n"


def main(tag: str, folder: str) -> int:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag) or not re.fullmatch(r"[\w-]+", folder):
        print(f"arguments refusés : {tag} {folder}")
        return 2
    root = Path.cwd() / folder
    if not (root / "docker-compose.yml").is_file():
        print(f"Téléchargement de LibreChat {tag}…", flush=True)
        code = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", tag, REPO, str(root)], check=False
        ).returncode
        if code:
            return code
    else:
        print("LibreChat déjà téléchargé : dossier gardé.", flush=True)
    env = root / ".env"
    if env.exists():
        print(".env existant gardé (ses secrets protègent les données déjà enregistrées).")
    else:
        env.write_text(env_text((root / ".env.example").read_text(encoding="utf-8")), encoding="utf-8")
        print(".env créé, avec des secrets tirés au hasard pour ce PC.")
    (root / "docker-compose.override.yml").write_text(OVERRIDE.format(tag=tag, rag=RAG_TAG), encoding="utf-8")
    print("Images à version figée : docker-compose.override.yml.")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
