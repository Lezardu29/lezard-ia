r"""Lanceur de Lezard IA installé par l'installateur Windows (32.1).

Copié à la racine du dossier d'installation, à côté de python\ (Python embarqué) et de gateway\ ; exécuté par
python\python.exe (installation, désinstallation, et le service dans sa fenêtre « Lezard IA », qui affiche la clé maître
une fois tout lancé ; la fermer arrête tout) ou python\pythonw.exe (sans fenêtre : ouverture, copie de la clé).

    lezard.py setup [--port N]    crée .env au premier passage (clés générées, port libre ; version gratuite sans licence)
    lezard.py service [--delay S] superviseur : lance la gateway, la relance si elle s'arrête (comme lancer.ps1 -Service)
    lezard.py open                démarre le service s'il ne tourne pas, puis ouvre le navigateur ; la 1re fois,
                                  copie la clé d'administration dans le presse-papiers et l'explique
    lezard.py key                 copie la clé d'administration dans le presse-papiers (jamais affichée)
    lezard.py stop                arrête le service et la gateway (désinstallation, mise à jour)
    lezard.py status              état du service
    lezard.py ollama              0 si Ollama est installé ou répond, 1 sinon (lu par l'installateur)
    lezard.py pack install|remove|refresh [voix] [multi]
                                  composants facultatifs, téléchargés seulement s'ils sont choisis (PyPI, versions et
                                  empreintes figées) ; refresh : mise à jour de ceux déjà installés (et d'Ollama, s'il
                                  a été installé par Lezard IA)
    lezard.py ollama-install      installe Ollama (installateur officiel figé et vérifié) ; il est ensuite lancé par le
                                  service de Lezard IA, pas par une application à part. Jamais pendant l'installation :
                                  les IA locales se choisissent après, depuis la page Modèles (test de performance)
    lezard.py tools web=on pc=off outils activés (TOOLS_WEB, TOOLS_PC dans .env), choisis à l'installation
    lezard.py install [--dir D]   installation depuis le dossier extrait du zip (« Installer Lezard IA.cmd ») :
                                  copie dans le dossier de l'installateur, configuration, raccourcis, démarrage
    lezard.py register            entrée « Applications installées » de Windows (installation depuis le zip)
    lezard.py uninstall           désinstallation d'une installation faite depuis le zip (données gardées par défaut)

Aucune tâche planifiée : le démarrage automatique est un raccourci du dossier Démarrage, créé et retiré par
l'installateur. Aucun modèle n'est téléchargé : le client les choisit lui-même (page Modèles, ou Ollama).
Journaux : data\logs\service.log et data\logs\gateway.log.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON_DIR = ROOT / "python"
# composants facultatifs : packs\<pack>.json (manifestes figés à la construction, installer\lock.py), et une fois
# installé, packs\<pack>\ (ajouté au chemin de Python par lezard-packs.pth, seulement s'il existe)
PACKS = ROOT / "packs"
PYPI = "https://files.pythonhosted.org/packages/"
OLLAMA_RELEASES = "https://github.com/ollama/ollama/releases/download/"
OLLAMA_MANAGED = PACKS / "ollama.installed"  # Ollama installé par Lezard IA : mis à jour par lui, lancé par lui
ENV_FILE = ROOT / ".env"
DATA = ROOT / "data"
LOGS = DATA / "logs"
STOP_FILE = DATA / "launcher.stop"
STATE_FILE = DATA / "launcher.json"
DEFAULT_PORT = 8000
OLLAMA_URL = "http://127.0.0.1:11434/api/tags"
OLLAMA_DOWNLOAD = "https://ollama.com/download"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
RESTART_EXIT = 3  # gateway.serve : redémarrage demandé (POST /v1/admin/restart)
# un mutex par dossier d'installation : deux installations (ou deux ports) ne se gênent pas
MUTEX_NAME = "Local\\LezardIA-" + hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:16]
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# ---------- outils ----------
def log(msg: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    path = LOGS / "service.log"
    if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
        path.replace(path.with_suffix(".log.1"))
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")


def say(msg: str) -> None:
    """Ligne de la fenêtre « Lezard IA » du service (et du journal) ; sans console (pythonw), le journal seul."""
    log(msg)
    if sys.stdout and sys.stdout.isatty():
        print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def message(text: str, error: bool = False) -> None:
    """Boîte de dialogue Windows (pythonw n'a pas de console)."""
    flags = 0x10 if error else 0x40  # MB_ICONERROR | MB_ICONINFORMATION
    ctypes.windll.user32.MessageBoxW(None, text, "Lezard IA", flags | 0x10000)  # MB_SETFOREGROUND


def new_secret(prefix: str) -> str:
    # comme New-Secret de lancer.ps1 : 32 octets aléatoires, ~43 caractères sans + / =
    return prefix + base64.b64encode(secrets.token_bytes(32)).decode().translate({43: None, 47: None, 61: None})


def read_env() -> dict[str, str]:
    """Mêmes règles que lancer.ps1 : NOM=valeur, guillemets retirés, valeurs vides ignorées."""
    out: dict[str, str] = {}
    if not ENV_FILE.exists():
        return out
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or name.startswith("#") or not name.replace("_", "a").isalnum() or name[0].isdigit():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            out[name] = value
    return out


def port() -> int:
    try:
        return int(read_env().get("GATEWAY_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def url_ok(url: str, timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (adresse locale fixe)
            return r.status == 200
    except OSError:
        return False


def healthy(p: int | None = None) -> bool:
    return url_ok(f"http://127.0.0.1:{p or port()}/health")


def port_free(p: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", p))
        except OSError:
            return False
    return True


def service_running() -> bool:
    handle = kernel32.OpenMutexW(0x00100000, False, MUTEX_NAME)  # SYNCHRONIZE
    if handle:
        kernel32.CloseHandle(handle)
    return bool(handle)


def ollama_exe() -> str | None:
    found = shutil.which("ollama")
    default = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return found or (str(default) if default.exists() else None)


# ---------- setup : configuration au premier passage ----------
def cmd_setup(args: argparse.Namespace) -> int:
    DATA.mkdir(exist_ok=True)
    if ENV_FILE.exists():  # réinstallation, mise à jour : configuration et clés du client conservées
        print("configuration existante conservée")
        return 0
    chosen = args.port or next((p for p in range(DEFAULT_PORT, DEFAULT_PORT + 100) if port_free(p)), DEFAULT_PORT)
    tls = next((p for p in range(8443, 8543) if p != chosen and port_free(p)), 8443)
    lan = (  # réseau de la maison (compris dans la version gratuite) : HTTPS, autorité locale créée par la gateway
        f"""
# --- appareils de la maison (téléphone, tablette, autre PC en Wi-Fi) : https://<adresse de ce PC>:{tls} ---
GATEWAY_BIND=0.0.0.0
GATEWAY_TLS_PORT={tls}
"""
        if args.lan
        else ""
    )
    text = f"""# Configuration de Lezard IA (créée par l'installateur). Ne pas partager ce fichier : il contient la clé
# d'administration et la clé qui chiffre vos données. Gardez-le avec le dossier data\\ (sauvegardes comprises).

# --- accès par le site : la licence (gratuite) se colle dans « Accès par le site » du tableau de bord ---

# --- accès : clé d'administration, demandée par la page de connexion (menu Démarrer > Lezard IA > Copier ma clé) ---
GATEWAY_MASTER_KEY={new_secret("gw-")}

# --- chiffrement des clés enregistrées dans la base (32 octets en base64) ---
GATEWAY_ENCRYPTION_KEY={base64.b64encode(secrets.token_bytes(32)).decode()}

# --- port de l'interface : http://localhost:{chosen} ---
GATEWAY_PORT={chosen}
{lan}
# --- fenêtre de contexte d'Ollama (défaut d'Ollama : 4096 tokens). Ex. : OLLAMA_CONTEXT_LENGTH=32768 ---
OLLAMA_CONTEXT_LENGTH=
"""
    # créé d'un bloc, jamais écrasé (O_EXCL) ; aucun secret n'est affiché
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"configuration créée (port {chosen})")
    return 0


# ---------- service : superviseur ----------
class Supervisor:
    def __init__(self) -> None:
        self.port = port()
        self.children: list[subprocess.Popen] = []

    def environment(self) -> dict[str, str]:
        # la configuration de l'installation vient de .env seulement : une variable GATEWAY_* du compte Windows
        # (base, port…) ne doit pas faire travailler cette installation sur d'autres fichiers
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GATEWAY_")}
        env.update(read_env())
        env.update({"GATEWAY_PORT": str(self.port), "GATEWAY_SUPERVISED": "1", "PYTHONUTF8": "1"})
        return env

    def save_state(self, gateway_pid: int | None = None) -> None:
        STATE_FILE.write_text(json.dumps({"pid": os.getpid(), "port": self.port, "gateway": gateway_pid}))

    def stop_requested(self) -> bool:
        return STOP_FILE.exists()

    def wait(self, seconds: float) -> bool:
        """Attend ; True si un arrêt a été demandé entre-temps."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stop_requested():
                return True
            time.sleep(1)
        return self.stop_requested()

    def kill(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            # l'arbre entier : un tunnel lancé par la gateway ne doit pas rester seul en ligne
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW
            )

    def announce(self) -> None:
        """Tout est lancé : l'adresse, puis la clé maître en DERNIÈRE ligne de la fenêtre (jamais dans le journal)."""
        say(f"Lezard IA est prêt : http://localhost:{self.port}")
        if sys.stdout and sys.stdout.isatty():
            print("Fermer cette fenêtre arrête Lezard IA.", flush=True)
            key = read_env().get("GATEWAY_MASTER_KEY")
            if key:
                print(f"Clé maître : {key}", flush=True)

    def stop_with_console(self) -> None:
        """Fenêtre fermée, Ctrl+C ou fin de session : la gateway (et son tunnel) et Ollama s'arrêtent avec elle."""
        if not (sys.stdout and sys.stdout.isatty()):
            return

        def handler(event: int) -> bool:
            try:
                STOP_FILE.write_text("1")
            except OSError:
                pass
            for proc in list(self.children):
                self.kill(proc)
            return True

        self._handler = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)(handler)  # gardé : sinon libéré
        kernel32.SetConsoleCtrlHandler(self._handler, True)

    def start_ollama(self, env: dict[str, str]) -> None:
        # comme lancer.ps1 : 5 s, Ollama répond lentement à l'ouverture de session
        if url_ok(OLLAMA_URL, 5):
            say("Ollama répond")
            return
        exe = ollama_exe()
        if not exe:
            say("Ollama non installé (facultatif : page Modèles ou marketplace)")
            return
        out = (LOGS / "ollama.log").open("ab")
        self.children.append(
            subprocess.Popen([exe, "serve"], stdout=out, stderr=out, env=env, creationflags=CREATE_NO_WINDOW)
        )
        say("Ollama démarré")

    def run(self, delay: int) -> int:
        STOP_FILE.unlink(missing_ok=True)
        self.stop_with_console()
        if sys.stdout and sys.stdout.isatty():
            kernel32.SetConsoleTitleW("Lezard IA")
        if delay:
            say(f"démarrage dans {delay} s (le temps que Windows et le réseau soient prêts)")
        if delay and self.wait(delay):  # ouverture de session : laisse le réseau et Ollama démarrer
            return 0
        say(f"démarrage de Lezard IA (port {self.port})")
        env = self.environment()
        announced = False
        while healthy(self.port) or not port_free(self.port):  # gateway lancée à la main, autre programme
            if not announced:
                say(f"port {self.port} déjà utilisé : attente qu'il se libère")
                announced = True
            if self.wait(10):
                return 0
        self.start_ollama(env)
        python = PYTHON_DIR / "python.exe"
        backoff = 5
        try:
            while not self.stop_requested():
                for name in ("gateway.log", "gateway.out.log"):  # journal de l'exécution précédente gardé (.1)
                    f = LOGS / name
                    if f.exists():
                        f.replace(f.with_name(name + ".1"))
                with (LOGS / "gateway.out.log").open("ab") as out, (LOGS / "gateway.log").open("ab") as err:
                    gw = subprocess.Popen(
                        [str(python), "-m", "gateway.serve", "--port", str(self.port)],
                        cwd=ROOT,
                        env=env,
                        stdout=out,
                        stderr=err,
                        creationflags=CREATE_NO_WINDOW,
                    )
                self.children.append(gw)
                self.save_state(gw.pid)
                say("démarrage de la gateway (journal : data\\logs\\gateway.log)")
                since, ready = time.monotonic(), False
                while gw.poll() is None:
                    if not ready and healthy(self.port):
                        ready = True
                        self.announce()  # la clé maître en dernière ligne
                    if self.stop_requested():
                        self.kill(gw)
                        gw.wait()
                        break
                    time.sleep(1)
                self.children.remove(gw)
                if self.stop_requested():
                    break
                if gw.returncode == RESTART_EXIT:
                    say("redémarrage demandé : relance de la gateway")
                    continue
                # arrêt inattendu : 5 s, 10 s… 5 min ; remis à 5 s après 5 min de fonctionnement
                backoff = 5 if time.monotonic() - since >= 300 else min(backoff * 2, 300)
                say(
                    f"la gateway s'est arrêtée (code {gw.returncode}) : relance dans {backoff} s (data\\logs\\gateway.log.1)"
                )
                if self.wait(backoff):
                    break
        finally:
            for proc in self.children:
                self.kill(proc)
            STATE_FILE.unlink(missing_ok=True)
            STOP_FILE.unlink(missing_ok=True)
            say("Lezard IA arrêté")
        return 0


def cmd_service(args: argparse.Namespace) -> int:
    DATA.mkdir(exist_ok=True)
    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS : déjà lancé
        kernel32.CloseHandle(handle)
        return 0
    try:
        return Supervisor().run(args.delay)
    finally:
        kernel32.CloseHandle(handle)


def start_service() -> None:
    """Service dans sa fenêtre « Lezard IA » : étapes du démarrage, puis la clé maître ; la fermer arrête tout."""
    python = PYTHON_DIR / "python.exe"
    subprocess.Popen(
        [str(python), str(ROOT / "lezard.py"), "service"], cwd=ROOT, creationflags=CREATE_NEW_CONSOLE, close_fds=True
    )


# ---------- open, key, stop, status ----------
def copy_key() -> bool:
    key = read_env().get("GATEWAY_MASTER_KEY")
    if not key:
        return False
    subprocess.run(["clip"], input=key.encode("ascii"), creationflags=CREATE_NO_WINDOW, check=False)
    return True


def cmd_open(args: argparse.Namespace) -> int:
    p = port()
    if not healthy(p):
        if not service_running():
            start_service()
        for _ in range(90):
            if healthy(p):
                break
            time.sleep(1)
        else:
            message(f"Lezard IA ne démarre pas.\n\nJournaux : {LOGS}\\service.log et gateway.log", error=True)
            return 1
    # connecté d'office sur ce PC : la clé passe dans le fragment (#key=…), que le navigateur n'envoie jamais au
    # serveur ; la page de connexion la vérifie puis l'efface de la barre d'adresse
    key = read_env().get("GATEWAY_MASTER_KEY")
    webbrowser.open(f"http://localhost:{p}/" + (f"#key={urllib.parse.quote(key, safe='')}" if key else ""))
    return 0


def cmd_key(args: argparse.Namespace) -> int:
    if copy_key():
        message(
            "Votre clé d'administration est copiée dans le presse-papiers : collez-la (Ctrl+V) dans "
            "« Clé d'accès » sur la page de connexion.\n\nNe la communiquez à personne."
        )
        return 0
    message(f"Clé introuvable : le fichier {ENV_FILE} est absent ou incomplet.", error=True)
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    if service_running():
        DATA.mkdir(exist_ok=True)
        STOP_FILE.write_text("1")
        for _ in range(30):
            if not service_running():
                break
            time.sleep(0.5)
    if service_running() and STATE_FILE.exists():
        # superviseur bloqué : arrêt forcé de ses processus, seulement s'il s'agit bien de notre Python
        state = json.loads(STATE_FILE.read_text())
        for pid in (state.get("gateway"), state.get("pid")):
            if not pid:
                continue
            found = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                creationflags=CREATE_NO_WINDOW,
            ).stdout.lower()
            if "python" in found:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW
                )
    STOP_FILE.unlink(missing_ok=True)
    print("service arrêté" if not service_running() else "service toujours en cours")
    return 0 if not service_running() else 1


def cmd_status(args: argparse.Namespace) -> int:
    p = port()
    print(f"service : {'en cours' if service_running() else 'arrêté'}")
    print(f"gateway : {f'répond sur http://localhost:{p}' if healthy(p) else 'ne répond pas'}")
    print(f"Ollama  : {'répond' if url_ok(OLLAMA_URL) else ('installé' if ollama_exe() else 'absent')}")
    for p in sorted(PACKS.glob("*.json")):
        if p.stem != "ollama":
            state = "à jour" if pack_current(p.stem) else "à mettre à jour" if (PACKS / p.stem).exists() else "absent"
            print(f"pack {p.stem} : {state}")
    return 0


def cmd_ollama(args: argparse.Namespace) -> int:
    return 0 if ollama_exe() or url_ok(OLLAMA_URL) else 1


# ---------- outils choisis à l'installation : recherche web, contrôle du PC ----------
TOOL_VARS = {"web": "TOOLS_WEB", "pc": "TOOLS_PC"}


def set_env_value(name: str, value: str) -> None:
    """Remplace (ou ajoute) une ligne NOM=valeur de .env ; le reste du fichier, clés comprises, est gardé tel quel."""
    lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines() if ENV_FILE.exists() else []
    found = False
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == name:
            lines[i], found = f"{name}={value}", True
    if not found:
        lines.append(f"{name}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_tools(args: argparse.Namespace) -> int:
    for item in args.choices:
        tool, _, state = item.partition("=")
        if tool not in TOOL_VARS or state not in ("on", "off"):
            print(f"choix inconnu : {item} (ex. web=on pc=off)")
            return 2
        set_env_value(TOOL_VARS[tool], "true" if state == "on" else "false")
    print("outils : " + ", ".join(args.choices))
    return 0


# ---------- composants facultatifs : voix locale, multi-agents, Ollama ----------
def manifest(name: str) -> dict:
    return json.loads((PACKS / f"{name}.json").read_text(encoding="utf-8"))


def download(url: str, sha256: str, size: int, dest: Path, allowed: str) -> None:
    """Téléchargement d'une adresse du manifeste (source officielle seulement), taille et SHA-256 vérifiées : un
    fichier différent de celui figé à la construction est supprimé, jamais utilisé."""
    if not url.startswith(allowed):
        raise RuntimeError(f"adresse refusée : {url}")
    digest, got = hashlib.sha256(), 0
    part = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, part.open("wb") as f:  # noqa: S310 (adresse vérifiée)
        while chunk := r.read(1 << 20):
            got += len(chunk)
            if got > size:
                break
            digest.update(chunk)
            f.write(chunk)
    if got != size or digest.hexdigest() != sha256:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{url.rsplit('/', 1)[-1]} : taille ou empreinte inattendue, fichier supprimé")
    part.replace(dest)


def extract_wheel(wheel: Path, target: Path) -> None:
    """Une roue est une archive zip : son contenu va dans target, comme pip le ferait (<dist>.data/purelib et platlib
    compris ; scripts et en-têtes ne servent pas ici). Aucun chemin ne sort de target."""
    root = target.resolve()
    with zipfile.ZipFile(wheel) as z:
        for info in z.infolist():
            parts = info.filename.split("/")
            if parts[0].endswith(".data"):
                if len(parts) < 3 or parts[1] not in ("purelib", "platlib"):
                    continue
                parts = parts[2:]
            name = "/".join(parts)
            if not name or name.endswith("/"):
                continue
            dest = (root / name).resolve()
            if root not in dest.parents:
                raise RuntimeError(f"{wheel.name} : chemin refusé ({info.filename})")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)


def pack_current(name: str) -> bool:
    """Pack installé, et dans la version figée par cette installation de Lezard IA."""
    installed = PACKS / name / "pack.json"
    return installed.exists() and json.loads(installed.read_text(encoding="utf-8")) == manifest(name)


def install_pack(name: str) -> None:
    """Télécharge les roues du pack, puis remplace packs\\<name> d'un bloc : un échec laisse l'ancien en place."""
    m = manifest(name)
    target, tmp, cache = PACKS / name, PACKS / f".{name}.tmp", PACKS / ".cache"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    cache.mkdir(exist_ok=True)
    total = sum(w["size"] for w in m["wheels"])
    log(f"pack {name} : {len(m['wheels'])} roues, {total / 1e6:.0f} Mo")
    try:
        for w in m["wheels"]:
            whl = cache / w["url"].rsplit("/", 1)[-1]
            download(w["url"], w["sha256"], w["size"], whl, PYPI)
            extract_wheel(whl, tmp)
            whl.unlink()
        (tmp / "pack.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        shutil.rmtree(target, ignore_errors=True)
        tmp.replace(target)
        log(f"pack {name} installé")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(cache, ignore_errors=True)


def ollama_version(exe: str) -> str | None:
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    words = [w for w in out.split() if w[:1].isdigit()]
    return words[-1] if words else None


def older(a: str, b: str) -> bool:
    def key(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.split(".") if x.isdigit())

    return key(a) < key(b)


def install_ollama() -> str:
    """Installe Ollama (installateur officiel, figé et vérifié), ou le met à jour s'il a été installé par Lezard IA.
    Un Ollama installé par le client n'est jamais touché. Ensuite, une seule application : Ollama ne démarre plus
    tout seul (raccourci du dossier Démarrage retiré), c'est le service de Lezard IA qui le lance."""
    m = manifest("ollama")
    exe = ollama_exe()
    if exe and not OLLAMA_MANAGED.exists():
        return "Ollama est déjà installé : il n'est pas modifié"
    current = ollama_version(exe) if exe else None
    if current and not older(current, m["version"]):
        return f"Ollama {current} à jour"
    PACKS.mkdir(exist_ok=True)
    setup = PACKS / "OllamaSetup.exe"
    log(f"Ollama {m['version']} : téléchargement ({m['size'] / 1e6:.0f} Mo)")
    download(m["url"], m["sha256"], m["size"], setup, OLLAMA_RELEASES)
    try:
        subprocess.run(
            [str(setup), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], check=True, creationflags=CREATE_NO_WINDOW
        )
    finally:
        setup.unlink(missing_ok=True)
    OLLAMA_MANAGED.write_text(m["version"], encoding="utf-8")
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    (startup / "Ollama.lnk").unlink(missing_ok=True)
    subprocess.run(["taskkill", "/IM", "ollama app.exe", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    exe = ollama_exe()
    if exe and not url_ok(OLLAMA_URL, 3):  # tout de suite (test de performance) ; ensuite, lancé par le service
        env = {**os.environ, **{k: v for k, v in read_env().items() if k.startswith("OLLAMA_")}}
        LOGS.mkdir(parents=True, exist_ok=True)
        out = (LOGS / "ollama.log").open("ab")
        subprocess.Popen([exe, "serve"], stdout=out, stderr=out, env=env, creationflags=CREATE_NO_WINDOW | DETACHED)
    log(f"Ollama {m['version']} installé, lancé par le service de Lezard IA")
    return f"Ollama {m['version']} installé"


def with_service_stopped(work):
    """Fichiers d'un pack en cours d'utilisation (DLL chargées) : le service est arrêté le temps du travail."""
    was_running = service_running()
    if was_running:
        cmd_stop(argparse.Namespace())
    try:
        return work()
    finally:
        if was_running:
            start_service()


def cmd_pack(args: argparse.Namespace) -> int:
    known = sorted(p.stem for p in PACKS.glob("*.json") if p.stem != "ollama")
    names = args.names or known
    unknown = [n for n in names if n not in known]
    if unknown:
        print(f"pack inconnu : {', '.join(unknown)} (connus : {', '.join(known)})")
        return 2

    def work() -> list[str]:
        done = []
        for n in names:
            if args.action == "remove":
                shutil.rmtree(PACKS / n, ignore_errors=True)
                done.append(f"{n} retiré")
            elif args.action == "install" or (PACKS / n / "pack.json").exists():  # refresh : packs déjà installés
                if pack_current(n):
                    done.append(f"{n} à jour")
                else:
                    install_pack(n)
                    done.append(f"{n} installé")
        if args.action == "refresh" and OLLAMA_MANAGED.exists():
            done.append(install_ollama())
        return done

    try:
        print("\n".join(with_service_stopped(work)) or "rien à faire")
        return 0
    except Exception as e:
        log(f"pack {args.action} : {e!r}")
        if args.notify:
            message(f"Un composant n'a pas pu être installé :\n\n{e}\n\nRéessayez plus tard (journal : {LOGS}).", True)
        return 1


def cmd_ollama_install(args: argparse.Namespace) -> int:
    try:
        print(install_ollama())  # sans arrêter le service : c'est souvent la passerelle qui le demande
        return 0
    except Exception as e:
        log(f"Ollama : {e!r}")
        if args.notify:
            message(
                f"Ollama n'a pas pu être installé :\n\n{e}\n\nVous pouvez l'installer depuis {OLLAMA_DOWNLOAD}.", True
            )
        return 1


# ---------- installation depuis le zip (sans installateur) ----------
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Lezard IA"  # celui de l'installateur
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\LezardIA-zip"  # retiré par l'installateur


def installed_by_setup(app: Path = ROOT) -> bool:
    """Installé par l'installateur (Inno Setup) : sa désinstallation et ses raccourcis prévalent."""
    return any(app.glob("unins*.exe"))


def version() -> str:
    try:
        return (ROOT / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def shortcuts(app: Path, autostart: bool, desktop: bool = True) -> None:
    """Raccourcis de l'installateur : menu Démarrer, Bureau, démarrage avec Windows (30 s après l'ouverture de session,
    fenêtre réduite). Créés par PowerShell (WScript.Shell) ; dossiers donnés par Windows (Bureau dans OneDrive…)."""

    def q(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    py, pyw, ico, script = (str(app / "python" / "python.exe"), str(app / "python" / "pythonw.exe"),
                            str(app / "lezard-ia.ico"), f'"{app / "lezard.py"}"')  # fmt: skip
    items = [
        ("Programs", r"Lezard IA\Lezard IA.lnk", pyw, f"{script} open", "Ouvrir Lezard IA dans le navigateur", 1),
        (
            "Programs",
            r"Lezard IA\Copier ma clé d'administration.lnk",
            pyw,
            f"{script} key",
            "Copie la clé demandée par la page de connexion",
            1,
        ),  # fmt: skip
        ("Programs", r"Lezard IA\Désinstaller Lezard IA.lnk", pyw, f"{script} uninstall", "Désinstaller Lezard IA", 1),
    ]
    if desktop:
        items.append(("Desktop", "Lezard IA.lnk", pyw, f"{script} open", "Ouvrir Lezard IA dans le navigateur", 1))
    if autostart:
        items.append(("Startup", "Lezard IA.lnk", py, f"{script} service --delay 30", "Lezard IA", 7))
    lines = ["$sh = New-Object -ComObject WScript.Shell"]
    for folder, name, target, arguments, comment, window in items:
        lines += [
            f"$p = Join-Path ([Environment]::GetFolderPath('{folder}')) {q(name)}",
            "New-Item -ItemType Directory -Force (Split-Path $p) | Out-Null",
            "$s = $sh.CreateShortcut($p)",
            f"$s.TargetPath = {q(target)}; $s.Arguments = {q(arguments)}; $s.WorkingDirectory = {q(str(app))}",
            f"$s.IconLocation = {q(ico)}; $s.Description = {q(comment)}; $s.WindowStyle = {window}; $s.Save()",
        ]
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", "\n".join(lines)],
        check=True,
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )


def remove_shortcuts(app: Path = ROOT) -> None:
    """Raccourcis de CETTE installation seulement (cible dans `app`) : une autre installation de Lezard IA sur le PC
    (copie de test, autre dossier) garde les siens, démarrage avec Windows compris."""
    root = str(app.resolve()).replace("'", "''")
    lines = [
        "$sh = New-Object -ComObject WScript.Shell",
        f"$root = '{root}\\'",
        "$menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Lezard IA'",
        "$links = @(Get-ChildItem $menu -Filter *.lnk -EA 0) + @(Get-Item (Join-Path ([Environment]::GetFolderPath("
        "'Desktop')) 'Lezard IA.lnk'), (Join-Path ([Environment]::GetFolderPath('Startup')) 'Lezard IA.lnk') -EA 0)",
        "foreach ($l in $links) { if ($sh.CreateShortcut($l.FullName).TargetPath.StartsWith($root, "
        "[StringComparison]::OrdinalIgnoreCase)) { Remove-Item -Force $l.FullName -EA 0 } }",
        "if ((Test-Path $menu) -and -not (Get-ChildItem $menu -EA 0)) { Remove-Item -Force $menu -EA 0 }",
    ]
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "\n".join(lines)],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)  # fmt: skip


def registered_here(app: Path = ROOT) -> bool:
    """L'entrée « Applications installées » du zip est-elle celle de cette installation (InstallLocation) ?"""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            where = winreg.QueryValueEx(k, "InstallLocation")[0]
    except OSError:
        return False
    return os.path.normcase(os.path.abspath(where)) == os.path.normcase(str(app.resolve()))


def cmd_register(args: argparse.Namespace) -> int:
    """Lezard IA dans « Applications installées » de Windows (désinstallation), version à jour. Sans objet quand
    l'installateur l'a installé : il a sa propre entrée."""
    import winreg

    if installed_by_setup():
        return 0
    pyw = ROOT / "python" / "pythonw.exe"
    values = {
        "DisplayName": "Lezard IA",
        "DisplayVersion": version(),
        "Publisher": "Lezard IA",
        "URLInfoAbout": "https://lezard-ia.fr",
        "DisplayIcon": str(ROOT / "lezard-ia.ico"),
        "InstallLocation": str(ROOT),
        "UninstallString": f'"{pyw}" "{ROOT / "lezard.py"}" uninstall',
    }
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        for name, value in values.items():
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        for name in ("NoModify", "NoRepair"):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, 1)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Installation depuis le dossier extrait du zip (« Installer Lezard IA.cmd ») : comme l'installateur, dans le
    même dossier (une version installée y est mise à jour, ses données gardées), sans droits administrateur."""
    import mise_a_jour

    target = Path(args.dir).resolve() if args.dir else INSTALL_DIR
    print(f"Installation de Lezard IA {version()} dans {target}")
    if ROOT.resolve() != target:
        if (target / "data" / "launcher.json").exists():
            print("arrêt de la version installée…")
            mise_a_jour.stop(target, ROOT)
        print("copie des fichiers (environ 100 Mo)…")
        target.mkdir(parents=True, exist_ok=True)
        mise_a_jour.copy_program(ROOT, target)
    first = not (target / ".env").exists()
    run = [str(target / "python" / "python.exe"), str(target / "lezard.py")]
    steps = [["setup", "--port", str(args.port)]]
    if first:
        steps.append(["tools", "web=on"])  # comme la case cochée par défaut de l'installateur
    steps.append(["pack", "refresh"])
    if not args.no_shortcuts:
        steps.append(["register"])
    for step in steps:
        print("préparation : " + " ".join(step))
        subprocess.run(run + step, cwd=target, check=step[0] in ("setup", "register"))
    if not args.no_shortcuts:
        print("raccourcis : menu Démarrer, Bureau" + (", démarrage avec Windows" if not args.no_autostart else ""))
        shortcuts(target, autostart=not args.no_autostart)
    if args.no_open:
        mise_a_jour.start(target)
        return 0
    subprocess.Popen([str(target / "python" / "pythonw.exe"), str(target / "lezard.py"), "open"], cwd=target)
    message(
        f"Lezard IA est installé.\n\nIl s'ouvre dans votre navigateur ; ensuite, menu Démarrer > « Lezard IA ».\n\n"
        f"Vous pouvez supprimer le zip téléchargé et le dossier extrait : Lezard IA est dans {target}."
    )
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Désinstallation d'une installation faite depuis le zip ; données gardées, sauf demande du client."""
    import winreg

    if installed_by_setup():
        setup = sorted(ROOT.glob("unins*.exe"))[-1]
        subprocess.Popen([str(setup)], cwd=ROOT)
        return 0
    if not args.yes:
        ask = ctypes.windll.user32.MessageBoxW(None, "Désinstaller Lezard IA ?", "Lezard IA", 0x4 | 0x20 | 0x10000)
        if ask != 6:  # IDYES
            return 0
    delete = args.delete_data or (
        not args.yes
        and ctypes.windll.user32.MessageBoxW(
            None,
            f"Supprimer aussi vos données ?\n\nConversations, documents, réglages et clés de Lezard IA ({ROOT}).\n\n"
            "Choisissez Non pour les garder : une prochaine installation les retrouvera.",
            "Lezard IA",
            0x4 | 0x20 | 0x100 | 0x10000,  # MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2
        )
        == 6
    )
    cmd_stop(argparse.Namespace())
    remove_shortcuts()
    if registered_here():  # l'entrée d'une autre installation (autre dossier) reste
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        except OSError:
            pass
    # ce Python est en cours d'utilisation : les fichiers sont supprimés juste après sa fin, par cmd.exe
    import mise_a_jour

    targets = [ROOT] if delete else [ROOT / item for item in mise_a_jour.program_items(ROOT)]
    removal = " & ".join(f'(rmdir /s /q "{p}" 2>nul || del /f /q "{p}" 2>nul)' for p in targets)
    subprocess.Popen(
        f'cmd /c "ping -n 4 127.0.0.1 >nul & {removal}"',
        cwd=ROOT.parent,  # pas dans le dossier à supprimer : Windows refuserait de le supprimer
        creationflags=CREATE_NO_WINDOW | DETACHED,
        close_fds=True,
    )
    if not args.yes:
        message("Lezard IA est désinstallé." + ("" if delete else f"\n\nVos données sont gardées dans {ROOT}."))
    return 0


def main(argv: list[str] | None = None) -> int:
    os.chdir(ROOT)
    parser = argparse.ArgumentParser(prog="lezard.py", description="Lanceur de Lezard IA")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--port", type=int, default=0, help="0 : premier port libre à partir de 8000")
    s.add_argument("--lan", action="store_true", help="joignable par les appareils de la maison, en HTTPS")
    s.set_defaults(func=cmd_setup)
    s = sub.add_parser("service")
    s.add_argument("--delay", type=int, default=0, help="attente avant de démarrer (ouverture de session)")
    s.set_defaults(func=cmd_service)
    for name, func in (
        ("open", cmd_open),
        ("key", cmd_key),
        ("stop", cmd_stop),
        ("status", cmd_status),
        ("ollama", cmd_ollama),
    ):
        sub.add_parser(name).set_defaults(func=func)
    s = sub.add_parser("pack", help="composants facultatifs : voix (dictée, lecture), multi (CrewAI)")
    s.add_argument("action", choices=("install", "remove", "refresh"), help="refresh : mise à jour des packs installés")
    s.add_argument("names", nargs="*", help="sans nom : tous les packs connus")
    s.add_argument("--notify", action="store_true", help="message en cas d'échec (installation interactive)")
    s.set_defaults(func=cmd_pack)
    s = sub.add_parser("tools", help="outils activés : web=on|off pc=on|off (écrits dans .env)")
    s.add_argument("choices", nargs="+")
    s.set_defaults(func=cmd_tools)
    s = sub.add_parser("ollama-install", help="installe Ollama, ou le met à jour s'il a été installé par Lezard IA")
    s.add_argument("--notify", action="store_true")
    s.set_defaults(func=cmd_ollama_install)
    s = sub.add_parser("install", help="installation depuis le dossier extrait du zip")
    s.add_argument("--dir", help=f"dossier d'installation (défaut : {INSTALL_DIR})")
    s.add_argument("--port", type=int, default=0, help="port au premier passage (0 : premier libre dès 8000)")
    s.add_argument("--no-autostart", action="store_true", help="sans démarrage avec Windows")
    s.add_argument("--no-shortcuts", action="store_true", help="sans raccourcis ni entrée Windows (tests)")
    s.add_argument("--no-open", action="store_true", help="démarre le service sans ouvrir le navigateur (tests)")
    s.set_defaults(func=cmd_install)
    sub.add_parser("register", help="entrée « Applications installées » (zip)").set_defaults(func=cmd_register)
    s = sub.add_parser("uninstall", help="désinstallation (installation depuis le zip)")
    s.add_argument("--yes", action="store_true", help="sans question (tests)")
    s.add_argument("--delete-data", action="store_true", help="supprime aussi data\\ et .env")
    s.set_defaults(func=cmd_uninstall)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:  # pythonw : sans ceci, une erreur disparaîtrait sans trace
        log(f"erreur ({args.command}) : {e!r}")
        raise


if __name__ == "__main__":
    sys.exit(main())
