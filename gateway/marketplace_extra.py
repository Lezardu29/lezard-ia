"""Supplément signé de la marketplace : des fiches ajoutées, corrigées ou retirées sans nouvelle version de Lezard IA.

Le catalogue (marketplace.py) et ses recettes (installers.py) sont écrits dans le programme. Ce supplément les
complète : un fichier publié à côté des versions (dépôt lezard-ia-telecharger), lu au lancement puis toutes les six
heures, et appliqué seulement si sa signature est celle de la clé des mises à jour.

Format : {"catalog": "LZC1.<contenu>.<signature>"} (base64url), contenu JSON :
    {"v": 1, "kid": "u1", "serial": 3, "published": <date>,
     "mcp": [fiche…], "apps": [fiche…], "tools": [fiche…], "removed": [{"key": "tool:x", "reason": "…"}]}
Signature Ed25519 de « LZC1.<contenu> » : le préfixe diffère de celui des versions (LZU1), donc une annonce de
version ne peut pas servir de catalogue, ni l'inverse.

Ce que la signature ne suffit pas à garantir est vérifié ici, fiche par fiche (parse) :
- aucune commande libre : une recette est une suite d'étapes typées (npm, uv, winget, extension VS Code, extension
  de Claude Code, installateur de GitHub à l'empreinte figée), traduites en étapes par ce programme ;
- versions figées, adresses en https, identifiants et arguments limités à des caractères sans danger pour cmd.exe ;
- un champ inconnu fait refuser tout le supplément (une faute de frappe ne passe pas en silence) ;
- « serial » ne recule jamais : un ancien supplément, même bien signé, ne remplace pas un plus récent.
Une fiche du supplément qui porte l'identifiant d'une fiche du programme la remplace (version montée, texte
corrigé). Une fiche retirée reste affichée avec la raison, ne s'installe plus, et se désinstalle encore.

Outils (sur la machine qui détient la clé privée) :
    python -m gateway.marketplace_extra check catalogue/supplement.json      relit le fichier source, sans signer
    python -m gateway.marketplace_extra sign --key data/update-signing.key --in catalogue/supplement.json
        --out dist/catalogue.json                                            signe (refuse un « serial » qui recule)
    python -m gateway.marketplace_extra show dist/catalogue.json             vérifie un fichier signé et le résume
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import installers, marketplace
from .license import _b64, _unb64, load_private, public_key_b64
from .release import PUBLIC_KEYS

PREFIX = "LZC1."
URL = "https://raw.githubusercontent.com/Lezardu29/lezard-ia-telecharger/main/catalogue.json"
SOURCE = Path(__file__).resolve().parent.parent / "catalogue" / "supplement.json"  # fichier source (dépôt privé)
SETTING = "marketplace_extra"
MAX_BYTES = 400_000
MAX_ENTRIES = 200
INTERVAL = 6 * 3600
STARTUP_DELAY = 45
KINDS = {"mcp": "mcp", "apps": "app", "tools": "tool"}

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
_NPM = re.compile(r"^(@[a-z0-9][a-z0-9._-]{0,60}/)?[a-z0-9][a-z0-9._-]{0,80}$")
_PYPI = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}(\[[a-z0-9,-]{1,40}\])?$")
_VERSION = re.compile(r"^\d{1,5}(\.\d{1,8}){1,3}$")
_PYTHON = re.compile(r"^3\.\d{1,2}$")
_WINGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,60}(\.[A-Za-z0-9][A-Za-z0-9-]{0,60}){1,4}$")
_VSCODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,60}\.[A-Za-z0-9][A-Za-z0-9-]{0,60}$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_PLUGIN = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}@[a-z0-9][a-z0-9-]{0,60}$")
_ASSET = re.compile(
    r"^https://github\.com/[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}/releases/download/"
    r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}/[A-Za-z0-9][A-Za-z0-9._+-]{0,119}\.(exe|msi|zip)$"
)
_SHA = re.compile(r"^[0-9a-f]{64}$")
_FOLDER = re.compile(r"^[A-Za-z0-9]{1,40}$")  # comme setup_download.folder : lettres et chiffres seulement
_PROGRAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,79}$")  # nom d'un programme dans la liste de Windows
_ARG = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9=._:/-]{0,80}$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")
_ENV_VALUE = re.compile(r"^[A-Za-z0-9._:/-]{0,120}$")
_TOOL = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# variables qui changeraient le programme lancé ou ce qu'il charge : jamais fixées par une fiche
_ENV_REFUSED = ("PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "APPDATA",
                "LOCALAPPDATA", "TEMP", "TMP", "NODE_", "NPM_", "PYTHON", "LD_", "DYLD_", "GATEWAY_", "LEZARD_")  # fmt: skip
SILENT = ("/S", "/VERYSILENT", "/quiet", "/qn")  # installateurs NSIS, Inno Setup, MSI
COMMERCIAL = ("oui", "non", "conditions")


class CatalogError(ValueError):
    pass


# ---------- lecture stricte ----------
def _text(d: dict, key: str, limit: int, required: bool = True, lines: bool = False) -> str | None:
    """Un texte affiché tel quel : borné, sans caractère de contrôle (retours à la ligne permis pour « install »)."""
    value = d.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise CatalogError(f"« {key} » : texte de 1 à {limit} caractères.")
    if any(ord(c) < 32 and not (lines and c == "\n") for c in value) or "\x7f" in value:
        raise CatalogError(f"« {key} » : caractère de contrôle refusé.")
    return value.strip()


def _match(d: dict, key: str, pattern: re.Pattern, required: bool = True) -> str | None:
    value = d.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CatalogError(f"« {key} » invalide : {str(value)[:60]!r}.")
    return value


def _url(d: dict, key: str, required: bool = True) -> str | None:
    value = d.get(key)
    if value is None and not required:
        return None
    parsed = urlparse(value) if isinstance(value, str) else None
    ok = (
        parsed is not None
        and parsed.scheme == "https"
        and bool(parsed.hostname)
        and not parsed.username
        and len(value) <= 300
        and not re.search(r"[\s<>\"'`\\^{}|\x00-\x1f\x7f]", value)
    )
    if not ok:
        raise CatalogError(f"« {key} » : une adresse https, sans identifiants.")
    return value


def _only(d, allowed: tuple[str, ...], what: str) -> dict:
    if not isinstance(d, dict):
        raise CatalogError(f"{what} : un objet est attendu.")
    unknown = sorted(set(d) - set(allowed))
    if unknown:
        raise CatalogError(f"{what} : champ inconnu « {unknown[0]} ».")
    return d


def _list(d: dict, key: str, limit: int) -> list:
    value = d.get(key, [])
    if not isinstance(value, list) or len(value) > limit:
        raise CatalogError(f"« {key} » : une liste de {limit} éléments au plus.")
    return value


def _license(d: dict) -> dict:
    lic = _only(d.get("license"), ("name", "copyright", "url", "commercial", "summary"), "licence")
    commercial = lic.get("commercial", "oui")
    if commercial not in COMMERCIAL:
        raise CatalogError("licence : « commercial » vaut oui, non ou conditions.")
    return marketplace._lic(
        _text(lic, "name", 80),
        _text(lic, "copyright", 160),
        _url(lic, "url"),
        commercial,
        _text(lic, "summary", 500, required=False) or "",
    )


def _commands(d: dict, default: list[dict] | None = None) -> list[dict]:
    raw = _list(d, "commands", 12)
    if not raw:
        if default is None:
            raise CatalogError("« commands » : au moins une commande (ce que l'élément ajoute, et où le taper).")
        return default
    out = []
    for c in raw:
        c = _only(c, ("use", "what", "example", "where"), "commande")
        where = c.get("where", "chat")
        if where not in marketplace.WHERE:
            raise CatalogError(f"commande : « where » parmi {', '.join(marketplace.WHERE)}.")
        out.append(
            marketplace._cmd(_text(c, "use", 200), _text(c, "what", 300), _text(c, "example", 300, False), where)
        )
    return out


def _step(step) -> list[tuple[str, ...]]:
    """Une étape typée -> les étapes lancées. Jamais de commande libre : seuls ces six types existent."""
    kind = step.get("type") if isinstance(step, dict) else None
    if kind == "npm":
        s = _only(step, ("type", "package", "version"), "étape npm")
        return [("npm", "install", "--global", f"{_match(s, 'package', _NPM)}@{_match(s, 'version', _VERSION)}")]
    if kind == "uv":
        s = _only(step, ("type", "package", "version", "python"), "étape uv")
        python = _match(s, "python", _PYTHON, required=False)
        package = f"{_match(s, 'package', _PYPI)}=={_match(s, 'version', _VERSION)}"
        return [("uv", "tool", "install", "--force", *(("--python", python) if python else ()), package)]
    if kind == "winget":
        s = _only(step, ("type", "id"), "étape winget")
        return [installers._winget(_match(s, "id", _WINGET))]
    if kind == "vscode":
        s = _only(step, ("type", "extension"), "étape vscode")
        return [("code", "--install-extension", _match(s, "extension", _VSCODE))]
    if kind == "claude-plugin":
        s = _only(step, ("type", "source", "plugin"), "étape claude-plugin")
        return [
            ("claude", "plugin", "marketplace", "add", _match(s, "source", _REPO)),
            ("claude", "plugin", "install", _match(s, "plugin", _PLUGIN)),
        ]
    if kind == "setup":
        s = _only(step, ("type", "url", "sha256", "size", "silent", "folder"), "étape setup")
        url, size = _match(s, "url", _ASSET), s.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size < 5_000_000_000:
            raise CatalogError("étape setup : « size » en octets.")
        if url.endswith(".zip"):
            if s.get("silent") is not None:
                raise CatalogError("étape setup : une archive .zip se décompresse dans « folder », sans « silent ».")
            last = _match(s, "folder", _FOLDER)
        else:
            if s.get("folder") is not None or s.get("silent") not in SILENT:
                raise CatalogError(f"étape setup : « silent » parmi {', '.join(SILENT)}.")
            last = s["silent"]
        return [(installers.PYTHON, installers.SETUP, url, _match(s, "sha256", _SHA), str(size), last)]
    raise CatalogError(f"étape de type inconnu : {str(kind)[:40]!r} (npm, uv, winget, vscode, claude-plugin, setup).")


def _default_where(steps: list[tuple[str, ...]]) -> str:
    head = steps[0][0]
    if head == "npm":
        return installers.NPM
    if head == "claude":
        return installers.CLAUDE
    if head == "winget":
        return installers.WINGET
    if head == "code":
        return "Dans VS Code (extensions)"
    if head == "uv":
        return "Outils Python de ce PC (uv tool)"
    return "Programmes de Windows (dossier de l'utilisateur)"


def _recipe(key: str, raw) -> tuple[dict, str | None]:
    """La recette d'une fiche et, pour un programme Windows, le nom sous lequel Windows le liste."""
    r = _only(raw, ("steps", "requires", "size", "where", "after", "windows", "windows_name", "uninstall_name"),
              f"recette de {key}")  # fmt: skip
    typed = _list(r, "steps", 6)
    if not typed:
        raise CatalogError(f"recette de {key} : au moins une étape.")
    steps = [argv for s in typed for argv in _step(s)]
    requires = _list(r, "requires", 6)
    if any(dep not in installers.PROVIDER for dep in requires):
        raise CatalogError(f"recette de {key} : « requires » parmi {', '.join(sorted(installers.PROVIDER))}.")
    setup = [argv for argv in steps if argv[:2] == (installers.PYTHON, installers.SETUP)]
    native = bool(setup) or any(argv[0] == "winget" for argv in steps)
    if r.get("windows") not in (None, True, False) or (native and r.get("windows") is False):
        raise CatalogError(f"recette de {key} : winget et les installateurs sont pour Windows (« windows »: true).")
    out = {
        "steps": steps,
        "size": _text(r, "size", 120),
        "where": _text(r, "where", 200, required=False) or _default_where(steps),
        "after": _text(r, "after", 400),
    }
    if requires:
        out["requires"] = tuple(requires)
    if native or r.get("windows"):
        out["windows"] = True
    listed = any(argv[0] == "winget" for argv in steps) or any(not argv[2].endswith(".zip") for argv in setup)
    windows_name = _match(r, "windows_name", _PROGRAM, required=False)
    if listed and not windows_name:
        raise CatalogError(f"recette de {key} : « windows_name », le nom du programme dans Paramètres > Applications.")
    if any(not argv[2].endswith(".zip") for argv in setup):
        out["uninstall_name"] = _match(r, "uninstall_name", _PROGRAM)
    elif r.get("uninstall_name") is not None:
        raise CatalogError(f"recette de {key} : « uninstall_name » ne sert qu'à un installateur .exe ou .msi.")
    return out, windows_name.lower() if windows_name else None


def _card(raw, kind: str, extra: tuple[str, ...]) -> tuple[dict, dict]:
    d = _only(raw, ("id", "name", "description", "url", "license", "commands", "warning", *extra), f"fiche ({kind})")
    card = {"id": _match(d, "id", _ID), "name": _text(d, "name", 80), "description": _text(d, "description", 600)}
    if warning := _text(d, "warning", 500, required=False):
        card["warning"] = warning
    return d, card


def _mcp(raw) -> dict:
    d, card = _card(raw, "mcp", ("package", "version", "size", "needs_folder", "args", "env", "folder_env", "secret",
                                 "service", "only_tools", "example"))  # fmt: skip
    example = _text(d, "example", 300, required=False) or "/mcp <votre demande>"
    card |= {
        "package": _match(d, "package", _NPM),
        "version": _match(d, "version", _VERSION),
        "size": _text(d, "size", 120),
        "needs_folder": d.get("needs_folder", False),
        "license": _license(d),
        "commands": _commands(d, marketplace._mcp_cmd(example)),
    }
    if card["needs_folder"] not in (True, False):
        raise CatalogError("fiche (mcp) : « needs_folder » vaut true ou false.")
    if url := _url(d, "url", required=False):
        card["url"] = url
    if args := _list(d, "args", 8):
        if not all(isinstance(a, str) and _ARG.fullmatch(a) for a in args):
            raise CatalogError("fiche (mcp) : « args » ne prend que des options simples (--option ou --option=valeur).")
        card["args"] = tuple(args)
    env = d.get("env") or {}
    names = [*env, *(n for n in (d.get("folder_env"), (d.get("secret") or {}).get("env")) if n is not None)]
    if not isinstance(env, dict) or len(env) > 8:
        raise CatalogError("fiche (mcp) : « env » est un objet de 8 variables au plus.")
    for name in names:
        if not isinstance(name, str) or not _ENV.fullmatch(name) or name.startswith(_ENV_REFUSED):
            raise CatalogError(f"fiche (mcp) : variable refusée « {str(name)[:40]} ».")
    if any(not isinstance(v, str) or not _ENV_VALUE.fullmatch(v) for v in env.values()):
        raise CatalogError("fiche (mcp) : valeur de variable refusée (lettres, chiffres, . _ : / - seulement).")
    if env:
        card["env"] = dict(env)
    if d.get("folder_env") is not None:
        if not card["needs_folder"]:
            raise CatalogError("fiche (mcp) : « folder_env » demande « needs_folder »: true.")
        card["folder_env"] = d["folder_env"]
    if d.get("secret") is not None:
        s = _only(d["secret"], ("env", "label", "url"), "clé d'un serveur MCP")
        if s.get("env") is None:
            raise CatalogError("clé d'un serveur MCP : « env », la variable qui la reçoit.")
        card["secret"] = {"env": s["env"], "label": _text(s, "label", 80), "url": _url(s, "url")}
        card["service"] = _text(d, "service", 400)  # où partent les données : dit avant de coller la clé
    elif service := _text(d, "service", 400, required=False):
        card["service"] = service
    if tools := _list(d, "only_tools", 40):
        if not all(isinstance(t, str) and _TOOL.fullmatch(t) for t in tools):
            raise CatalogError("fiche (mcp) : « only_tools », des noms d'outils.")
        card["only_tools"] = tuple(tools)
    return card


def _item(raw, kind: str) -> tuple[dict, dict | None, str | None]:
    d, card = _card(raw, kind, ("install", "needs", "lezard", "recipe"))
    card |= {"url": _url(d, "url"), "license": _license(d), "commands": _commands(d)}
    for key, limit in (("needs", 300), ("lezard", 400)):
        if value := _text(d, key, limit, required=False):
            card[key] = value
    if install := _text(d, "install", 600, required=False, lines=True):
        card["install"] = install
    if kind == "tool":  # la page les affiche pour chaque outil
        card.setdefault("needs", "Rien de plus.")
        card.setdefault("lezard", "Fiche ajoutée au catalogue après la sortie de cette version.")
    if d.get("recipe") is None:
        return card, None, None
    recipe, windows_name = _recipe(f"{kind}:{card['id']}", d["recipe"])
    return card, recipe, windows_name


def parse(data) -> dict:
    """Le contenu d'un supplément, vérifié et traduit dans les formes du catalogue ; CatalogError à la première faute."""
    try:
        return _parse(data)
    except CatalogError:
        raise
    except (TypeError, AttributeError, KeyError, IndexError, ValueError, RecursionError) as e:
        # une valeur du mauvais type (liste à la place d'un objet…) : refusée comme le reste, jamais une erreur brute
        raise CatalogError(f"Supplément mal formé ({type(e).__name__}).") from None


def _parse(data) -> dict:
    d = _only(data, ("v", "kid", "serial", "published", "mcp", "apps", "tools", "removed"), "supplément")
    serial = d.get("serial")
    if not isinstance(serial, int) or isinstance(serial, bool) or not 1 <= serial < 10**9:
        raise CatalogError("« serial » : un entier qui augmente à chaque publication.")
    published = d.get("published") or 0
    if not isinstance(published, (int, float)) or isinstance(published, bool):
        raise CatalogError("« published » : une date (secondes).")
    out = {"serial": serial, "published": float(published), "mcp": [], "apps": [], "tools": [], "recipes": {},
           "windows_names": {}, "removed": {}}  # fmt: skip
    if sum(len(_list(d, k, MAX_ENTRIES)) for k in (*KINDS, "removed")) > MAX_ENTRIES:
        raise CatalogError(f"Supplément trop long : {MAX_ENTRIES} fiches au plus.")
    for raw in d.get("mcp", []):
        out["mcp"].append(_mcp(raw))
    for field, kind in (("apps", "app"), ("tools", "tool")):
        for raw in d.get(field, []):
            card, recipe, windows_name = _item(raw, kind)
            out[field].append(card)
            if recipe:
                out["recipes"][f"{kind}:{card['id']}"] = recipe
            if windows_name:
                out["windows_names"][f"{kind}:{card['id']}"] = windows_name
    for field in KINDS:
        ids = [c["id"] for c in out[field]]
        if len(ids) != len(set(ids)):
            raise CatalogError(f"« {field} » : un identifiant en double.")
    for raw in d.get("removed", []):
        r = _only(raw, ("key", "reason"), "retrait")
        key = r.get("key")
        if not isinstance(key, str) or key.count(":") != 1 or key.split(":")[0] not in KINDS.values():
            raise CatalogError("retrait : « key » de la forme mcp:<id>, app:<id> ou tool:<id>.")
        if not _ID.fullmatch(key.split(":")[1]):
            raise CatalogError(f"retrait : identifiant invalide dans « {key[:60]} ».")
        out["removed"][key] = _text(r, "reason", 300)
    return out


# ---------- signature ----------
def sign(private_key: Ed25519PrivateKey, kid: str, content: dict) -> str:
    parse(content)  # jamais de supplément signé que le programme refuserait
    data = {"v": 1, "kid": kid, **{k: v for k, v in content.items() if k not in ("v", "kid")}}
    body = _b64(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode())
    return f"{PREFIX}{body}.{_b64(private_key.sign((PREFIX + body).encode()))}"


def verify(token: str, keys: dict[str, str] | None = None) -> dict:
    """Contenu vérifié d'un supplément signé (parse), ou CatalogError avec la raison."""
    token = token.strip() if isinstance(token, str) else ""
    if not token.startswith(PREFIX) or token.count(".") != 2 or len(token) > MAX_BYTES:
        raise CatalogError("Supplément illisible.")
    body, sig = token[len(PREFIX) :].split(".")
    try:
        signature = _unb64(sig)
        raw = _unb64(body)
    except (ValueError, UnicodeDecodeError):
        raise CatalogError("Supplément illisible.")
    try:
        head = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise CatalogError("Supplément illisible.")
    if not isinstance(head, dict) or head.get("v") != 1:
        raise CatalogError("Format de supplément inconnu.")
    key = (keys or PUBLIC_KEYS).get(str(head.get("kid")))
    if not key:
        raise CatalogError("Supplément signé par une clé inconnue.")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(signature, (PREFIX + body).encode())
    except (InvalidSignature, ValueError):
        raise CatalogError("Signature du supplément invalide : fichier modifié ou contrefait, rien n'est appliqué.")
    return parse(head)


# ---------- application au catalogue ----------
# le catalogue du programme, tel qu'à l'import : chaque supplément repart de lui (appliquer deux fois ne change rien)
_BASE: dict | None = None
_applied: dict = {"serial": 0, "published": 0.0, "added": [], "replaced": [], "removed": {}}


def _base() -> dict:
    global _BASE
    if _BASE is None:
        from . import detection

        _BASE = {
            "mcp": marketplace.MCP_SERVERS,
            "apps": marketplace.APPS,
            "tools": marketplace.AGENT_TOOLS,
            "recipes": dict(installers.RECIPES),
            "windows_names": dict(detection.WINDOWS_NAMES),
        }
    return _BASE


def _merge(base: tuple, extra: list[dict], kind: str, removed: dict, notes: dict) -> tuple:
    """Les fiches du programme, celles du supplément par-dessus (même identifiant : remplacée), puis les retraits."""
    by_id = {e["id"]: e for e in base}
    for card in extra:
        notes["replaced" if card["id"] in by_id else "added"].append(f"{kind}:{card['id']}")
        if kind == "app" and card["id"] in by_id:
            card = {"guide": by_id[card["id"]].get("guide"), **card}  # son guide de branchement reste
        by_id[card["id"]] = card
    out = []
    for card in by_id.values():
        reason = removed.get(f"{kind}:{card['id']}")
        if reason:
            card = {**card, "retired": True, "warning": f"Retiré du catalogue : {reason}"}
        out.append(card)
    return tuple(out)


def apply(content: dict | None) -> dict:
    """Applique un supplément vérifié (parse) au catalogue en mémoire ; None : le catalogue du programme seul."""
    from . import detection

    base = _base()
    content = content or {"serial": 0, "published": 0.0, "mcp": [], "apps": [], "tools": [], "recipes": {},
                          "windows_names": {}, "removed": {}}  # fmt: skip
    notes = {"added": [], "replaced": []}
    removed = content["removed"]
    marketplace.MCP_SERVERS = _merge(base["mcp"], content["mcp"], "mcp", removed, notes)
    marketplace.APPS = _merge(base["apps"], content["apps"], "app", removed, notes)
    marketplace.AGENT_TOOLS = _merge(base["tools"], content["tools"], "tool", removed, notes)
    installers.RECIPES.clear()
    installers.RECIPES.update({**base["recipes"], **content["recipes"]})
    # une fiche remplacée sans recette perd l'ancienne : ce que la page annonce est ce qui serait lancé
    for field, kind in (("apps", "app"), ("tools", "tool")):
        for card in content[field]:
            if f"{kind}:{card['id']}" not in content["recipes"]:
                installers.RECIPES.pop(f"{kind}:{card['id']}", None)
    detection.WINDOWS_NAMES.clear()
    detection.WINDOWS_NAMES.update({**base["windows_names"], **content["windows_names"]})
    marketplace.RETIRED.clear()
    marketplace.RETIRED.update(removed)
    detection.forget()
    _applied.update(serial=content["serial"], published=content["published"], removed=dict(removed), **notes)
    return status()


def status() -> dict:
    return {**_applied, "added": list(_applied["added"]), "replaced": list(_applied["replaced"])}


def apply_source(path: Path | None = None) -> dict | None:
    """Pour les outils du dépôt (export du site, versions figées) : le fichier source, vérifié mais pas signé."""
    path = path or SOURCE
    if not path.is_file():
        return None
    return apply(parse(json.loads(path.read_text(encoding="utf-8"))))


# ---------- lecture en ligne ----------
def enabled() -> bool:
    """Comme les mises à jour : activé d'office pour l'installation Windows, sur demande ailleurs."""
    from .updates import installer_mode

    default = "on" if installer_mode() else "off"
    return os.getenv("GATEWAY_CATALOG_CHECK", default).strip().lower() in ("1", "on", "true", "yes")


def catalog_url() -> str:
    return os.getenv("LEZARD_CATALOG_URL", "").strip() or URL


class CatalogUpdater:
    """Le supplément gardé dans la base (jeton signé, revérifié à chaque lecture) et sa relecture en ligne."""

    def __init__(self, store, clock=time.time, transport=None):
        self.store, self.clock, self.transport = store, clock, transport
        self.last: dict = {}

    def load(self) -> dict:
        """Au lancement : le dernier supplément reçu, s'il est toujours bien signé ; sinon le catalogue du programme."""
        saved = self.store.get_setting(SETTING) or {}
        try:
            return apply(verify(saved["token"]) if saved.get("token") else None)
        except CatalogError:
            return apply(None)  # réglage abîmé ou modifié : ignoré, il sera relu en ligne

    def _fetch(self) -> str | None:
        with httpx.Client(transport=self.transport, timeout=20, follow_redirects=False) as client:
            with client.stream("GET", catalog_url()) as r:
                if r.status_code != 200:
                    return None
                raw = b""
                for chunk in r.iter_bytes(65536):
                    raw += chunk
                    if len(raw) > MAX_BYTES:
                        raise CatalogError("Supplément trop gros.")
        data = json.loads(raw.decode("utf-8"))
        return data.get("catalog") if isinstance(data, dict) and isinstance(data.get("catalog"), str) else None

    def refresh(self) -> dict:
        """Relit le supplément en ligne. Rien ne change s'il est illisible, mal signé ou plus ancien que le nôtre."""
        result: dict = {"at": self.clock()}
        try:
            token = self._fetch()
            if token is None:
                result["error"] = "Catalogue en ligne injoignable ou absent : celui de ce PC est gardé."
            else:
                content = verify(token)
                current = (self.store.get_setting(SETTING) or {}).get("serial") or 0
                if content["serial"] < current:
                    result["error"] = "Catalogue en ligne plus ancien que celui de ce PC : ignoré."
                else:
                    if content["serial"] > current or _applied["serial"] != content["serial"]:
                        self.store.set_setting(
                            SETTING, {"token": token, "serial": content["serial"], "at": result["at"]}
                        )
                        apply(content)
                    result["serial"] = content["serial"]
        except CatalogError as e:
            result["error"] = str(e)
        except (httpx.HTTPError, ValueError, UnicodeDecodeError, RecursionError):
            result["error"] = "Catalogue en ligne injoignable ou illisible : celui de ce PC est gardé."
        self.last = result
        return result

    def view(self) -> dict:
        return {**status(), "enabled": enabled(), "last": self.last or None}

    async def run(self) -> None:
        await asyncio.sleep(STARTUP_DELAY)
        while True:
            if enabled():
                try:
                    await asyncio.to_thread(self.refresh)
                except Exception:  # une lecture ratée n'arrête jamais les suivantes
                    pass
            await asyncio.sleep(INTERVAL)


# ---------- outils ----------
def _summary(content: dict) -> str:
    parts = [
        f"{len(content[k])} {label}"
        for k, label in (("mcp", "serveur(s) MCP"), ("apps", "application(s)"), ("tools", "outil(s)"))
    ]
    return f"serial {content['serial']} : {', '.join(parts)}, {len(content['recipes'])} recette(s), {len(content['removed'])} retrait(s)"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Supplément signé de la marketplace (machine qui détient la clé privée).")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="relit un fichier source, sans signer")
    c.add_argument("source")
    s = sub.add_parser("sign", help="signe le fichier source")
    s.add_argument("--key", required=True)
    s.add_argument("--kid", default="u1")
    s.add_argument("--in", dest="source", required=True)
    s.add_argument("--out", required=True)
    v = sub.add_parser("show", help="vérifie un fichier signé et le résume")
    v.add_argument("signed")
    a = p.parse_args(argv)
    try:
        if a.cmd == "show":
            content = verify(json.loads(Path(a.signed).read_text(encoding="utf-8"))["catalog"])
            print(f"Signature valide. {_summary(content)}")
            return 0
        source = json.loads(Path(a.source).read_text(encoding="utf-8"))
        content = parse(source)
        if a.cmd == "check":
            print(f"Fichier source valide. {_summary(content)}")
            return 0
        out = Path(a.out)
        if out.is_file():  # le fichier signé précédent : « serial » doit avancer, sinon les PC l'ignoreraient
            previous = verify(json.loads(out.read_text(encoding="utf-8"))["catalog"])["serial"]
            if content["serial"] <= previous:
                print(
                    f"« serial » doit dépasser {previous} (valeur du fichier déjà signé) : augmentez-le dans {a.source}."
                )
                return 2
        key = load_private(a.key)
        if public_key_b64(key) != PUBLIC_KEYS.get(a.kid):
            print("Cette clé n'est pas celle que Lezard IA connaît : les PC refuseraient ce supplément.")
            return 2
        token = sign(key, a.kid, {**source, "published": int(time.time())})
        verify(token)  # relu comme le fera chaque PC
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"catalog": token}) + "\n", encoding="ascii")
        print(f"Supplément signé : {out}. {_summary(content)}")
        return 0
    except (CatalogError, OSError, ValueError, KeyError, TypeError) as e:
        print(f"Refusé : {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
