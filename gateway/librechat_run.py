"""LibreChat sans Docker : python librechat_run.py <dossier de LibreChat>

Lancé par Lezard IA (commande /librechat du chat, chat_tools : service « serve »). Démarre la base MongoDB du dossier
(mongodb/mongod.exe, données dans data-db, sur ce PC seulement), attend qu'elle réponde, puis le serveur de LibreChat
(node api/server/index.js, NODE_ENV=production) ; s'il s'arrête, la base s'arrête aussi.
"""

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

MONGO_PORT = 27018  # celui de librechat_setup.py


def listening(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), 1).close()
        return True
    except OSError:
        return False


def main(folder: str) -> int:
    root = Path(folder)
    mongod = root / "mongodb" / "mongod.exe"
    node = shutil.which("node")
    if not mongod.exists() or not node or not (root / "client" / "dist").is_dir():
        print("LibreChat incomplet : réinstallez-le depuis la marketplace de Lezard IA.")
        return 2
    data = root / "data-db"
    data.mkdir(exist_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    db = None
    if not listening(MONGO_PORT):  # déjà lancée (LibreChat redémarré) : gardée
        with (root / "mongod.log").open("ab") as log:
            db = subprocess.Popen(
                [str(mongod), "--dbpath", str(data), "--port", str(MONGO_PORT), "--bind_ip", "127.0.0.1", "--quiet"],
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        for _ in range(60):
            if listening(MONGO_PORT) or db.poll() is not None:
                break
            time.sleep(1)
        if not listening(MONGO_PORT):
            print("La base MongoDB n'a pas démarré (voir mongod.log du dossier LibreChat).")
            db.kill()
            return 3
    env = {k: v for k, v in os.environ.items() if k.upper() != "ELECTRON_RUN_AS_NODE"}
    env.update(NODE_ENV="production", SCARF_ANALYTICS="false", DO_NOT_TRACK="1")
    # la clé de LibreChat est celle de son .env : une LEZARD_API_KEY héritée (celle de Zed et Goose) passerait avant
    env.pop("LEZARD_API_KEY", None)
    try:
        return subprocess.call([node, "api/server/index.js"], cwd=root, env=env)
    finally:
        if db and db.poll() is None:
            db.terminate()
            try:
                db.wait(30)
            except subprocess.TimeoutExpired:
                db.kill()


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2]))
