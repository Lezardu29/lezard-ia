"""Applications de la marketplace branchées d'office sur Lezard IA, juste après leur installation.

- Réglages dans un fichier : Continue (~/.continue/config.yaml), Zed (settings.json de Zed), Goose
  (custom_providers + config.yaml), LibreChat (librechat.yaml et .env du dossier LibreChat), Cline (~/.cline/data :
  globalState.json + secrets.json), Kilo Code (~/.config/kilo/kilo.jsonc, format d'OpenCode).
- Applications de bureau qui gardent leurs réglages dans leur propre base : Jan (data/settings.json, clé dans le
  Gestionnaire d'identification de Windows, service « jan-providers »), Cherry Studio (cherrystudio.sqlite),
  AnythingLLM (storage/.env), Chatbox CE (config.json) et Claude Code Router (config.sqlite). Elles écrasent leur base
  en se fermant : le branchement se fait application fermée (sinon : « fermez-la, puis Brancher sur Lezard IA »).
  Juste après l'installation, la base n'existe pas encore : l'application est ouverte une fois, le temps de la créer,
  puis refermée.
- Cursor reste à brancher avec son guide : il appelle la gateway depuis les serveurs de Cursor (adresse Internet et
  compte Cursor requis) et range ses réglages dans une base interne non documentée.

- Une clé virtuelle par application (« Application : Continue »), sans droits d'administration, révocable seule ;
  rebrancher l'application la remplace (l'ancienne est révoquée). Zed et Goose ne lisent une clé que dans une
  variable d'environnement : elles partagent la variable LEZARD_API_KEY de l'utilisateur Windows, et sa clé.
- Un fichier existant n'est jamais perdu : copie « .avant-lezard » avant la première modification, seuls les
  réglages de Lezard IA sont ajoutés ou remplacés.
- L'adresse est celle de ce PC (127.0.0.1).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
import logging
from pathlib import Path

from .keys import display_prefix, generate_secret, hash_secret, validate_key

ENV_NAME = "LEZARD_API_KEY"  # Zed (fournisseur « Lezard ») et Goose (api_key_env) la lisent
SHARED_KEY = "Applications : Zed et Goose (variable LEZARD_API_KEY)"
CONTEXT = 32768
log = logging.getLogger("gateway")


class ConnectError(Exception):
    pass


# ---------- clés ----------
def _active(store, owner: str, name: str) -> list[dict]:
    return [k for k in store.list_keys(owner) if k.get("name") == name and k.get("status") == "active"]


def new_key(store, owner: str, plan: str, name: str) -> str:
    """Une clé neuve pour l'application ; ses anciennes clés (même nom) sont révoquées."""
    for old in _active(store, owner, name):
        store.set_key_status(owner, old["id"], "revoked", "remplacée : application rebranchée", actor="auto")
    data = validate_key({"name": name}, plan)
    secret = generate_secret()
    key = store.add_key(owner, {**data, "key_prefix": display_prefix(secret)}, hash_secret(secret))
    store.log_key_event(owner, key["id"], "created", "branchement automatique d'une application", "auto")
    return secret


def shared_key(store, owner: str, plan: str) -> str:
    """La clé de la variable LEZARD_API_KEY : gardée si elle est encore active (l'autre application s'en sert),
    sinon une neuve, écrite dans la variable."""
    current = user_env(ENV_NAME)
    found = store.key_by_hash(hash_secret(current)) if current else None
    if found and found[1].get("status") == "active" and found[1].get("name") == SHARED_KEY:
        return current
    secret = new_key(store, owner, plan, SHARED_KEY)
    set_user_env(ENV_NAME, secret)
    return secret


# ---------- variable d'environnement de l'utilisateur Windows ----------
def user_env(name: str) -> str | None:
    if sys.platform != "win32":
        return os.environ.get(name)
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def set_user_env(name: str, value: str) -> None:
    """Variable de l'utilisateur (comme « setx ») : vue par les applications lancées ensuite."""
    os.environ[name] = value
    if sys.platform != "win32":
        return
    import ctypes
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
    result = ctypes.c_ulong()  # prévient l'Explorateur : le menu Démarrer lance les applications avec la variable
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))


# ---------- fichiers ----------
def _backup(path: Path) -> None:
    keep = path.with_name(path.name + ".avant-lezard")
    if path.exists() and not keep.exists():
        shutil.copy2(path, keep)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup(path)
    path.write_text(text, encoding="utf-8")


def appdata() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / ".config")


def strip_jsonc(text: str) -> str:
    """JSON avec commentaires (// et /* */) et virgules finales, tel que Zed l'écrit : en JSON strict."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


# ---------- applications ----------
def continue_(store, owner: str, plan: str, port: int) -> str:
    path = Path.home() / ".continue" / "config.yaml"
    key = new_key(store, owner, plan, "Application : Continue")
    block = (
        "  - name: Lezard IA (auto)\n"
        "    provider: openai\n"
        "    model: auto\n"
        f"    apiBase: http://127.0.0.1:{port}/v1\n"
        f"    apiKey: {key}\n"
        "    roles: [chat, edit, apply]\n"
    )
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    # un branchement précédent de Lezard IA : remplacé (jusqu'à l'élément suivant de la liste ou la clé suivante)
    old = re.sub(r"  - name: Lezard IA \(auto\)\n(?:    .*\n)*", "", old)
    if not old.strip():
        text = "name: Lezard IA\nversion: 1.0.0\nschema: v1\nmodels:\n" + block
    elif re.search(r"^models:\s*\[\]\s*$", old, re.M):
        text = re.sub(r"^models:\s*\[\]\s*$", "models:\n" + block.rstrip("\n"), old, count=1, flags=re.M)
    elif re.search(r"^models:\s*$", old, re.M):
        text = re.sub(r"^models:\s*\n", "models:\n" + block, old, count=1, flags=re.M)
    else:
        text = old.rstrip("\n") + "\nmodels:\n" + block
    _write(path, text)
    return "Continue utilise Lezard IA (modèle « Lezard IA (auto) ») : rouvrez VS Code si Continue était ouvert."


def zed(store, owner: str, plan: str, port: int) -> str:
    path = appdata() / "Zed" / "settings.json"
    settings = {}
    if path.exists():
        try:
            settings = json.loads(strip_jsonc(path.read_text(encoding="utf-8"))) or {}
        except ValueError as e:
            raise ConnectError(f"settings.json de Zed illisible ({e}) : branchez-le avec le guide « Zed ».")
    providers = settings.setdefault("language_models", {}).setdefault("openai_compatible", {})
    providers["Lezard"] = {  # Zed lit sa clé dans la variable LEZARD_API_KEY (nom du fournisseur + _API_KEY)
        "api_url": f"http://127.0.0.1:{port}/v1",
        "available_models": [
            {
                "name": "auto",
                "display_name": "Lezard IA (auto)",
                "max_tokens": CONTEXT,
                "capabilities": {
                    "tools": True,
                    "images": False,
                    "parallel_tool_calls": False,
                    "prompt_cache_key": False,
                },
            }
        ],
    }
    shared_key(store, owner, plan)
    _write(path, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    return (
        "Zed utilise Lezard IA (fournisseur « Lezard », modèle « Lezard IA (auto) ») : relancez Zed s'il était ouvert."
    )


def goose(store, owner: str, plan: str, port: int) -> str:
    root = appdata() / "Block" / "goose" / "config"
    provider = {
        "name": "lezard",
        "engine": "openai",
        "display_name": "Lezard IA",
        "description": "Les IA de Lezard IA, sur ce PC",
        "api_key_env": ENV_NAME,
        "base_url": f"http://127.0.0.1:{port}/v1/chat/completions",
        "models": [{"name": "auto", "context_limit": CONTEXT}],
        "supports_streaming": True,
    }
    _write(root / "custom_providers" / "lezard.json", json.dumps(provider, ensure_ascii=False, indent=2) + "\n")
    config = root / "config.yaml"
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    for name, value in (("GOOSE_PROVIDER", "lezard"), ("GOOSE_MODEL", "auto")):
        line = f"{name}: {value}"
        text, n = re.subn(rf"^{name}:.*$", line, text, flags=re.M)
        if not n:
            text = (text.rstrip("\n") + "\n" if text.strip() else "") + line + "\n"
    shared_key(store, owner, plan)
    _write(config, text)
    return "Goose utilise Lezard IA (fournisseur « Lezard IA », modèle « auto ») : relancez Goose s'il était ouvert."


# version du format de librechat.yaml attendue par le LibreChat installé (installers.LIBRECHAT_TAG : v0.8.8)
LIBRECHAT_YAML = """# Écrit par Lezard IA : Lezard IA comme fournisseur de LibreChat (clé : LEZARD_API_KEY dans .env).
version: 1.3.17
cache: true
endpoints:
  custom:
    - name: "Lezard IA"
      apiKey: "${{LEZARD_API_KEY}}"
      baseURL: "http://127.0.0.1:{port}/v1"
      models:
        default: ["auto", "auto/speed", "auto/economy"]
        fetch: false
      titleConvo: true
      titleModel: "current_model"
      modelDisplayLabel: "Lezard IA"
"""


def librechat(store, owner: str, plan: str, port: int) -> str:
    """LibreChat (installé sans Docker dans ~/LibreChat, lancé par Lezard IA) : Lezard IA en « custom endpoint »
    (librechat.yaml), sa clé dans le .env du dossier."""
    root = Path.home() / "LibreChat"
    if not (root / "package.json").is_file():
        raise ConnectError("dossier LibreChat introuvable : installez-le depuis la marketplace.")
    config = root / "librechat.yaml"
    if config.exists() and "Écrit par Lezard IA" not in config.read_text(encoding="utf-8"):
        raise ConnectError("librechat.yaml déjà personnalisé : ajoutez Lezard IA avec le guide « LibreChat ».")
    key = new_key(store, owner, plan, "Application : LibreChat")
    _write(config, LIBRECHAT_YAML.format(port=port))
    env = root / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    text, n = re.subn(rf"^{ENV_NAME}=.*$", f"{ENV_NAME}={key}", text, flags=re.M)
    if not n:
        text = (text.rstrip("\n") + "\n" if text.strip() else "") + f"{ENV_NAME}={key}\n"
    _write(env, text)
    return (
        "LibreChat utilise Lezard IA (fournisseur « Lezard IA ») : démarrez-le avec /librechat dans le chat "
        "(déjà démarré : arrêtez-le puis relancez-le), puis ouvrez http://localhost:3080."
    )


def claudemem(store, owner: str, plan: str, port: int) -> str:
    """claude-mem (extension de Claude Code) : ses résumés de mémoire faits par Lezard IA (fournisseur « compatible
    OpenAI »), jamais par le compte Claude de ce PC (fournisseur et repli « claude » de son installateur), sans
    télémétrie (activée chez lui par défaut)."""
    path = Path.home() / ".claude-mem" / "settings.json"
    try:
        settings = json.loads(path.read_text("utf-8")) if path.exists() else {}
    except ValueError:
        raise ConnectError("~/.claude-mem/settings.json illisible") from None
    settings.update(
        CLAUDE_MEM_PROVIDER="openai-compatible",
        CLAUDE_MEM_OPENAI_COMPAT_BASE_URL=f"http://127.0.0.1:{port}/v1",
        CLAUDE_MEM_OPENAI_COMPAT_API_KEY=new_key(store, owner, plan, "Application : claude-mem"),
        CLAUDE_MEM_OPENAI_COMPAT_MODEL="auto",
        # vide = « claude » chez lui ; le même fournisseur que le principal = aucun repli
        CLAUDE_MEM_QUOTA_FALLBACK_PROVIDER="openai-compatible",
        CLAUDE_MEM_TELEMETRY="false",
    )
    _write(path, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    return "claude-mem fait ses résumés de mémoire avec Lezard IA (modèle « auto »), sans télémétrie."


# ---------- extensions de VS Code ----------
def cline(store, owner: str, plan: str, port: int) -> str:
    """Cline lit ses réglages dans ~/.cline/data (partagés par toutes les fenêtres de VS Code et son CLI) : fournisseur
    « OpenAI Compatible » (apiProvider « openai ») pour les modes Plan et Act, clé dans secrets.json."""
    root = Path(os.environ.get("CLINE_DATA_DIR") or Path.home() / ".cline" / "data")
    state, secrets = _read_json(root / "globalState.json"), _read_json(root / "secrets.json")
    state.update(
        openAiBaseUrl=f"http://127.0.0.1:{port}/v1",
        planModeApiProvider="openai",
        actModeApiProvider="openai",
        planModeOpenAiModelId="auto",
        actModeOpenAiModelId="auto",
        welcomeViewCompleted=True,
    )
    secrets["openAiApiKey"] = new_key(store, owner, plan, "Application : Cline")
    _write(root / "secrets.json", json.dumps(secrets, ensure_ascii=False, indent=2) + "\n")
    _write(root / "globalState.json", json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return (
        "Cline utilise Lezard IA (fournisseur « OpenAI Compatible », modèle « auto ») : rechargez VS Code "
        "(Ctrl+Maj+P, « Reload Window »)."
    )


def kilocode(store, owner: str, plan: str, port: int) -> str:
    """Kilo Code (bâti sur OpenCode) : fournisseur « lezard » dans ~/.config/kilo/kilo.jsonc, choisi par défaut."""
    root = Path.home() / ".config" / "kilo"
    path = next((f for f in (root / "kilo.jsonc", root / "kilo.json") if f.exists()), root / "kilo.jsonc")
    try:
        config = json.loads(strip_jsonc(path.read_text(encoding="utf-8"))) if path.exists() else {}
    except ValueError as e:
        raise ConnectError(
            f"{path.name} de Kilo Code illisible ({e}) : branchez-le avec le guide « Kilo Code »."
        ) from None
    config.setdefault("$schema", "https://app.kilo.ai/config.json")
    config.setdefault("provider", {})["lezard"] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Lezard IA",
        "options": {
            "baseURL": f"http://127.0.0.1:{port}/v1",
            "apiKey": new_key(store, owner, plan, "Application : Kilo Code"),
        },
        "models": {"auto": {"name": "Lezard IA (auto)", "limit": {"context": CONTEXT, "output": 8192}}},
    }
    config["model"] = "lezard/auto"
    _write(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    return (
        "Kilo Code utilise Lezard IA (fournisseur « Lezard IA », modèle « auto ») : rechargez VS Code "
        "(Ctrl+Maj+P, « Reload Window »)."
    )


# ---------- applications de bureau : branchées fermées ----------
def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        raise ConnectError(f"{path} illisible") from None
    return data if isinstance(data, dict) else {}


def _running(image: str) -> bool:
    """Le programme `image` (« Jan-Desktop.exe ») tourne-t-il ?"""
    if sys.platform != "win32":
        return False
    out = subprocess.run(  # octets : tasklist écrit dans la page de code de la console (accents)
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"], capture_output=True, check=False
    ).stdout
    return f'"{image.lower()}"' in out.decode("ascii", "replace").lower()


def _close(image: str) -> None:
    subprocess.run(["taskkill", "/IM", image, "/T", "/F"], capture_output=True, check=False)
    for _ in range(20):
        if not _running(image):
            return
        time.sleep(0.5)


def _shortcut(name: str) -> Path | None:
    """Raccourci du menu Démarrer de l'application (« Jan », « Cherry Studio »)."""
    for var in ("APPDATA", "PROGRAMDATA"):
        menu = Path(os.environ.get(var, "")) / "Microsoft" / "Windows" / "Start Menu"
        for lnk in sorted(menu.rglob("*.lnk")) if menu.is_dir() else ():
            if lnk.stem.lower() == name.lower():
                return lnk
    return None


FIRST_RUN_WAIT = 120  # secondes : premier lancement d'une application (création de sa base)
_FRESH = threading.local()  # branchement juste après l'installation (connect(fresh=True))


def _program_of(lnk: Path) -> str | None:
    """Nom du programme lancé par le raccourci (« Jan-Desktop.exe ») : celui que Windows montre dans tasklist."""
    script = "(New-Object -ComObject WScript.Shell).CreateShortcut($env:LEZARD_LNK).TargetPath"
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=30,
            env={**os.environ, "LEZARD_LNK": str(lnk)},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    target = out.decode("utf-8", "replace").strip()
    return Path(target).name if target.lower().endswith(".exe") else None


def _wait_and_close(image: str, ready) -> None:
    deadline = time.time() + FIRST_RUN_WAIT
    while time.time() < deadline and not ready():
        time.sleep(1)
    time.sleep(5)  # fin de l'écriture de ses réglages de départ
    _close(image)


def _closed_and_ready(label: str, start: str, ready) -> None:
    """L'application est fermée et sa base existe : sinon elle est ouverte une fois (création de la base), puis
    refermée. Ouverte par l'utilisateur : ConnectError (elle écraserait les réglages en se fermant)."""
    lnk = _shortcut(start)
    image = _program_of(lnk) if lnk else None
    if not lnk or not image:
        raise ConnectError(f"{label} introuvable dans le menu Démarrer : installez-le d'abord.")
    if _running(image):
        if not _FRESH.on:
            raise ConnectError(
                f"{label} est ouvert : fermez-le (y compris près de l'horloge), puis « Brancher sur Lezard IA »."
            )
        _wait_and_close(image, ready)  # juste installé : ouvert par son installateur, personne ne s'en sert encore
    if not ready():
        env = {k: v for k, v in os.environ.items() if k.upper() != "ELECTRON_RUN_AS_NODE"}
        subprocess.Popen(["explorer.exe", str(lnk)], env=env)  # comme un double-clic sur son raccourci
        _wait_and_close(image, ready)
    if _running(image):
        raise ConnectError(f"{label} ne se ferme pas : fermez-le, puis « Brancher sur Lezard IA ».")
    if not ready():
        raise ConnectError(
            f"{label} n'a pas créé ses réglages : ouvrez-le une fois, fermez-le, puis « Brancher sur Lezard IA »."
        )


def _store_credential(target: str, user: str, secret: str) -> None:
    """Identifiant générique du Gestionnaire d'identification de Windows, tel que le crate Rust keyring l'écrit
    (cible « utilisateur.service », secret en UTF-16)."""
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    blob = secret.encode("utf-16-le")
    buf = ctypes.create_string_buffer(blob, len(blob))
    cred = CREDENTIAL(
        Type=1,  # CRED_TYPE_GENERIC
        TargetName=target,
        CredentialBlobSize=len(blob),
        CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)),
        Persist=3,  # CRED_PERSIST_ENTERPRISE, comme keyring
        UserName=user,
    )
    if not ctypes.windll.advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(f"Gestionnaire d'identification : erreur {ctypes.GetLastError()}")


JAN_PROVIDER = "Lezard IA"


def jan(store, owner: str, plan: str, port: int) -> str:
    """Jan : fournisseur personnalisé « Lezard IA » (comme « Ajouter un fournisseur » de ses réglages) dans
    data/settings.json ; sa clé dans le Gestionnaire d'identification de Windows, où Jan range les clés."""
    folder = _read_json(appdata() / "Jan" / "settings.json").get("data_folder")
    path = Path(folder or appdata() / "Jan" / "data") / "settings.json"
    _closed_and_ready("Jan", "Jan", lambda: "model-provider" in _read_json(path))
    settings = _read_json(path)
    try:
        persisted = json.loads(settings["model-provider"])
    except ValueError:
        raise ConnectError("réglages des fournisseurs de Jan illisibles") from None
    state = persisted.setdefault("state", {})
    base = f"http://127.0.0.1:{port}/v1"
    model = {"id": "auto", "name": "Lezard IA (auto)", "capabilities": ["completion", "tools"]}
    field = {"controller_type": "input"}
    provider = {
        "provider": JAN_PROVIDER,
        "active": True,
        "base_url": base,
        "models": [model],
        "settings": [
            {
                **field,
                "key": "api-key",
                "title": "API Key",
                "description": "Clé de Lezard IA (rangée dans le Gestionnaire d'identification de Windows).",
                "controller_props": {
                    "placeholder": "Insert API Key",
                    "value": "",
                    "type": "password",
                    "input_actions": ["unobscure", "copy"],
                },
            },
            {
                **field,
                "key": "base-url",
                "title": "Base URL",
                "description": "Adresse de Lezard IA sur ce PC.",
                "controller_props": {"placeholder": base, "value": base},
            },
        ],
    }
    others = [p for p in state.get("providers") or [] if p.get("provider") != JAN_PROVIDER]
    state["providers"] = [*others, provider]
    state["selectedProvider"], state["selectedModel"] = JAN_PROVIDER, model
    key = new_key(store, owner, plan, "Application : Jan")
    if sys.platform == "win32":
        _store_credential(f"{JAN_PROVIDER}.jan-providers", JAN_PROVIDER, json.dumps([key]))
    index = path.parent / "provider_secrets.index.json"  # fournisseurs dont une clé est rangée
    try:
        names = set(json.loads(index.read_text(encoding="utf-8"))) if index.exists() else set()
    except ValueError:
        names = set()
    index.write_text(json.dumps(sorted(names | {JAN_PROVIDER})), encoding="utf-8")
    settings["model-provider"] = json.dumps(persisted, ensure_ascii=False)
    _write(path, json.dumps(settings, ensure_ascii=False, indent=2))
    return "Jan utilise Lezard IA (fournisseur « Lezard IA », modèle « Lezard IA (auto) ») : ouvrez Jan."


CHERRY_ID = "lezard-ia"
CHERRY_ENDPOINT = "openai-chat-completions"


def cherrystudio(store, owner: str, plan: str, port: int) -> str:
    """Cherry Studio 2 : fournisseur « Lezard IA » et son modèle « auto » dans sa base (tables user_provider,
    user_model), choisi comme modèle par défaut des discussions, de la traduction et de l'assistant rapide."""
    path = appdata() / "CherryStudio" / "Data" / "cherrystudio.sqlite"

    def ready() -> bool:
        if not path.exists():
            return False
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as db:
                return bool(db.execute("select count(*) from user_provider").fetchone()[0])
        except sqlite3.Error:
            return False

    _closed_and_ready("Cherry Studio", "Cherry Studio", ready)
    _backup(path)
    now = int(time.time() * 1000)
    key = new_key(store, owner, plan, "Application : Cherry Studio")
    with sqlite3.connect(path, timeout=10) as db:
        last = db.execute("select max(order_key) from user_provider").fetchone()[0] or "a0"
        db.execute("delete from user_model where provider_id = ?", (CHERRY_ID,))
        db.execute("delete from user_provider where provider_id = ?", (CHERRY_ID,))
        db.execute(
            "insert into user_provider (provider_id, preset_provider_id, name, endpoint_configs, default_chat_endpoint,"
            " api_keys, is_enabled, order_key, created_at, updated_at) values (?, null, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                CHERRY_ID,
                "Lezard IA",
                json.dumps({CHERRY_ENDPOINT: {"baseUrl": f"http://127.0.0.1:{port}/v1"}}),
                CHERRY_ENDPOINT,
                json.dumps([{"id": str(uuid.uuid4()), "key": key, "label": "Lezard IA", "isEnabled": True}]),
                last + "V",  # clé d'ordre fractionnaire : un caractère de plus la place après la dernière
                now,
                now,
            ),
        )
        db.execute(
            "insert into user_model (id, provider_id, model_id, name, capabilities, endpoint_types, context_window,"
            " supports_streaming, is_enabled, is_hidden, is_deprecated, order_key, created_at, updated_at)"
            " values (?, ?, 'auto', 'Lezard IA (auto)', '[]', ?, ?, 1, 1, 0, 0, 'a0', ?, ?)",
            (f"{CHERRY_ID}::auto", CHERRY_ID, json.dumps([CHERRY_ENDPOINT]), CONTEXT, now, now),
        )
        for pref in ("chat.default_model_id", "feature.quick_assistant.model_id", "feature.translate.model_id"):
            db.execute(
                "insert into preference (scope, key, value, created_at, updated_at) values ('default', ?, ?, ?, ?)"
                " on conflict(scope, key) do update set value = excluded.value, updated_at = excluded.updated_at",
                (pref, json.dumps(f"{CHERRY_ID}::auto"), now, now),
            )
    return "Cherry Studio utilise Lezard IA (fournisseur « Lezard IA », modèle « Lezard IA (auto) ») : ouvrez Cherry Studio."


def anythingllm(store, owner: str, plan: str, port: int) -> str:
    """AnythingLLM Desktop : fournisseur « Generic OpenAI » dans storage/.env, que l'application relit au démarrage."""
    env = appdata() / "anythingllm-desktop" / "storage" / ".env"
    _closed_and_ready("AnythingLLM", "AnythingLLM", env.exists)
    text = env.read_text(encoding="utf-8")
    values = {
        "LLM_PROVIDER": "generic-openai",
        "GENERIC_OPEN_AI_BASE_PATH": f"http://127.0.0.1:{port}/v1",
        "GENERIC_OPEN_AI_MODEL_PREF": "auto",
        "GENERIC_OPEN_AI_MODEL_TOKEN_LIMIT": str(CONTEXT),
        "GENERIC_OPEN_AI_MAX_TOKENS": "4096",
        "GENERIC_OPEN_AI_API_KEY": new_key(store, owner, plan, "Application : AnythingLLM"),
    }
    for name, value in values.items():
        line = f"{name}='{value}'"
        text, n = re.subn(rf"^{name}=.*$", lambda _m, line=line: line, text, flags=re.M)
        if not n:
            text = text.rstrip("\n") + "\n" + line + "\n"
    _write(env, text)
    return "AnythingLLM utilise Lezard IA (fournisseur « Generic OpenAI », modèle « auto ») : ouvrez AnythingLLM."


def chatbox(store, owner: str, plan: str, port: int) -> str:
    """Chatbox CE (0.10) : fournisseur OpenAI vers Lezard IA, modèle personnalisé « auto » (config.json)."""
    path = appdata() / "xyz.chatboxapp.ce" / "config.json"
    _closed_and_ready("Chatbox", "Chatbox CE", lambda: "settings" in _read_json(path))
    config = _read_json(path)
    config["settings"].update(
        aiProvider="openai",
        apiHost=f"http://127.0.0.1:{port}",  # Chatbox ajoute /v1/chat/completions
        openaiKey=new_key(store, owner, plan, "Application : Chatbox"),
        model="custom-model",
        openaiCustomModel="auto",
    )
    _write(path, json.dumps(config, ensure_ascii=False, indent=2))
    return "Chatbox utilise Lezard IA (modèle « auto ») : ouvrez Chatbox."


def ccrouter(store, owner: str, plan: str, port: int) -> str:
    """Claude Code Router 3 : fournisseur « lezard » (modèle « auto ») dans sa base config.sqlite, choisi par défaut.
    Son service est arrêté le temps de l'écrire (« ccr start » le relance)."""
    from . import installers

    ccr = installers.program("ccr")
    if not ccr:
        raise ConnectError("commande ccr introuvable : installez Claude Code Router d'abord.")
    path = appdata() / "claude-code-router" / "config.sqlite"
    env = {k: v for k, v in os.environ.items() if k.upper() != "ELECTRON_RUN_AS_NODE"}

    def ccr_run(*args: str) -> None:
        subprocess.run([ccr, *args], capture_output=True, timeout=120, check=False, env=env)

    ccr_run("stop")
    if not path.exists():  # premier lancement : il crée sa base (seulement avec son gateway démarré)
        ccr_run("start", "--no-open")
        for _ in range(30):
            if path.exists():
                break
            time.sleep(1)
        ccr_run("stop")
    if not path.exists():
        raise ConnectError("Claude Code Router n'a pas créé ses réglages : lancez « ccr ui » une fois.")
    _backup(path)
    with sqlite3.connect(path, timeout=10) as db:
        row = db.execute("select value_json from app_config where key = 'default'").fetchone()
        config = json.loads(row[0]) if row else {}
        providers = [p for p in config.get("Providers") or [] if p.get("name") != "lezard"]
        providers.append(
            {
                "name": "lezard",
                "api_base_url": f"http://127.0.0.1:{port}/v1/chat/completions",
                "api_key": new_key(store, owner, plan, "Application : Claude Code Router"),
                "models": ["auto"],
            }
        )
        config["Providers"], config["preferredProvider"] = providers, "lezard"
        stamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        db.execute(
            "insert into app_config (key, value_json, updated_at) values ('default', ?, ?)"
            " on conflict(key) do update set value_json = excluded.value_json, updated_at = excluded.updated_at",
            (json.dumps(config, ensure_ascii=False), stamp),
        )
    return "Claude Code Router utilise Lezard IA (fournisseur « lezard », modèle « auto ») : « ccr start » le relance."


APPS = {
    "continue": continue_,
    "zed": zed,
    "goose": goose,
    "librechat": librechat,
    "cline": cline,
    "kilocode": kilocode,
    "jan": jan,
    "cherrystudio": cherrystudio,
    "anythingllm": anythingllm,
    "chatbox": chatbox,
}
TOOLS = {"claudemem": claudemem, "ccrouter": ccrouter}  # outils pour agents branchés de même, après leur installation


def connect(app: str, store, owner: str, plan: str, port: int, fresh: bool = False) -> str | None:
    """Branche l'application sur Lezard IA ; le message pour la page, ou None si elle n'est pas concernée.
    `fresh` : juste après son installation (une application ouverte par son installateur peut être refermée)."""
    fn = APPS.get(app) or TOOLS.get(app)
    if not fn:
        return None
    _FRESH.on = fresh
    try:
        return fn(store, owner, plan, port)
    except ConnectError as e:
        return f"Branchement automatique impossible : {e}"
    except OSError as e:
        return f"Branchement automatique impossible ({e}) : suivez le guide de l'application."
    except Exception as e:  # format d'une nouvelle version de l'application, base verrouillée…
        log.exception("Branchement automatique de %s", app)
        return f"Branchement automatique impossible ({type(e).__name__}) : suivez le guide de l'application."
    finally:
        _FRESH.on = False
