"""Applications de la marketplace branchées d'office sur Lezard IA, juste après leur installation.

Pour celles qui lisent leurs réglages dans un fichier : Continue (~/.continue/config.yaml), Zed (settings.json de Zed),
Goose (fournisseur déclaré dans custom_providers, choisi dans config.yaml) et LibreChat (librechat.yaml et .env du
dossier LibreChat). Les autres (Jan, Chatbox, Cursor, Cline, Kilo Code, Cherry Studio, AnythingLLM…) rangent leurs
réglages dans une base interne, ou leur éditeur déconseille de les modifier : elles gardent leur guide.

- Une clé virtuelle par application (« Application : Continue »), sans droits d'administration, révocable seule ;
  rebrancher l'application la remplace (l'ancienne est révoquée). Zed et Goose ne lisent une clé que dans une
  variable d'environnement : elles partagent la variable LEZARD_API_KEY de l'utilisateur Windows, et sa clé.
- Un fichier existant n'est jamais perdu : copie « .avant-lezard » avant la première modification, seuls les
  réglages de Lezard IA sont ajoutés ou remplacés.
- L'adresse est celle de ce PC (127.0.0.1) ; LibreChat tourne dans Docker : host.docker.internal.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .keys import display_prefix, generate_secret, hash_secret, validate_key

ENV_NAME = "LEZARD_API_KEY"  # Zed (fournisseur « Lezard ») et Goose (api_key_env) la lisent
SHARED_KEY = "Applications : Zed et Goose (variable LEZARD_API_KEY)"
CONTEXT = 32768


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
      baseURL: "http://host.docker.internal:{port}/v1"
      models:
        default: ["auto"]
        fetch: true
      titleConvo: true
      titleModel: "current_model"
      modelDisplayLabel: "Lezard IA"
"""


def librechat(store, owner: str, plan: str, port: int) -> str:
    root = Path.home() / "LibreChat"
    if not (root / "docker-compose.yml").is_file():
        raise ConnectError("dossier LibreChat introuvable : branchez-le avec le guide « LibreChat ».")
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
    override = root / "docker-compose.override.yml"
    if override.exists() and "librechat.yaml" not in override.read_text(encoding="utf-8"):
        o = override.read_text(encoding="utf-8")  # le fichier est monté dans le conteneur, qui joint ce PC
        o = o.replace(
            "  api:\n",
            "  api:\n    volumes:\n      - type: bind\n        source: ./librechat.yaml\n        target: /app/librechat.yaml\n"
            '    extra_hosts:\n      - "host.docker.internal:host-gateway"\n',
            1,
        )
        _write(override, o)
    docker = shutil.which("docker")
    if docker:  # le serveur relit ses réglages
        subprocess.run(
            [docker, "compose", "--project-directory", str(root), "up", "--detach"],
            capture_output=True,
            timeout=600,
            check=False,
        )
    return "LibreChat utilise Lezard IA (fournisseur « Lezard IA ») : rechargez la page http://localhost:3080."


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


APPS = {"continue": continue_, "zed": zed, "goose": goose, "librechat": librechat}
TOOLS = {"claudemem": claudemem}  # outils pour agents branchés de même, à la fin de leur installation


def connect(app: str, store, owner: str, plan: str, port: int) -> str | None:
    """Branche l'application sur Lezard IA ; le message pour la page, ou None si elle n'est pas concernée."""
    fn = APPS.get(app) or TOOLS.get(app)
    if not fn:
        return None
    try:
        return fn(store, owner, plan, port)
    except ConnectError as e:
        return f"Branchement automatique impossible : {e}"
    except OSError as e:
        return f"Branchement automatique impossible ({e}) : suivez le guide de l'application."
