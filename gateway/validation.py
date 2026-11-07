"""Validation des entrées (tâche 10.1) : une requête mal formée reçoit une erreur 400 explicite,
jamais une erreur 500 (exception non gérée au fond du pipeline).

- json_object : corps JSON qui doit être un objet ;
- validate_chat : requête /v1/chat/completions (format OpenAI) ;
- validate_messages_body : requête /v1/messages (format Anthropic).
On vérifie la STRUCTURE dont la gateway dépend (rôles, types, champs lus) ; le fond (valeurs
acceptées par tel modèle) reste au fournisseur.
"""

from __future__ import annotations

import base64
import binascii
import json
import ipaddress
import os
import re
from urllib.parse import urlsplit

OPENAI_ROLES = {"system", "developer", "user", "assistant", "tool", "function"}
ANTHROPIC_ROLES = {"user", "assistant"}
MAX_MESSAGES = 10_000
# images jointes au chat (19.2) : le type annoncé doit correspondre au contenu réel
IMAGE_MAGIC = {
    "png": (bytes.fromhex("89504e470d0a1a0a"),),
    "jpeg": (bytes.fromhex("ffd8ff"),),
    "gif": (b"GIF87a", b"GIF89a"),
    "webp": (b"RIFF",),
}
_DATA_URL = re.compile(r"data:image/(png|jpeg|jpg|gif|webp);base64,", re.I)


def max_image_bytes() -> int:
    return int(os.getenv("CHAT_IMAGE_MAX_BYTES", str(5 * 1024 * 1024)))


def check_image_url(url: str, where: str) -> None:
    """Image en ligne (http/https) ou incluse (data:image/png|jpeg|gif|webp;base64) : autres schémas refusés
    (file:, javascript:…) ; une image incluse doit avoir le contenu de son type et rester sous CHAT_IMAGE_MAX_BYTES."""
    if url.startswith(("http://", "https://")):
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        try:
            ip = ipaddress.ip_address(host)
            internal = not ip.is_global
        except ValueError:
            internal = (
                not host
                or "." not in host
                or host == "localhost"
                or host.endswith((".local", ".localhost", ".internal", ".lan", ".home"))
            )
        if internal:  # le serveur d'IA qui télécharge l'image joindrait le réseau local (SSRF)
            raise RequestError(f"{where} : image en ligne refusée (adresse locale) : joignez le fichier à la place.")
        return
    m = _DATA_URL.match(url)
    if not m:
        if url.startswith("data:"):
            raise RequestError(f"{where} : image refusée (types acceptés : PNG, JPEG, GIF, WebP).", 415)
        raise RequestError(f"{where} : 'image_url' doit être une adresse http(s) ou une image data:image/…;base64.")
    payload = url[m.end() :]
    if len(payload) * 3 // 4 > max_image_bytes():
        raise RequestError(f"{where} : image trop volumineuse (max {max_image_bytes() // (1024 * 1024)} Mo).", 413)
    try:
        head = base64.b64decode(payload[:64] + "=" * (-len(payload[:64]) % 4))
        base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise RequestError(f"{where} : image base64 invalide.")
    kind = "jpeg" if m.group(1).lower() == "jpg" else m.group(1).lower()
    if not head.startswith(IMAGE_MAGIC[kind]) or (kind == "webp" and head[8:12] != b"WEBP"):
        raise RequestError(f"{where} : le contenu n'est pas une image {kind.upper()} (type annoncé trompeur).", 415)


class RequestError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


async def json_object(request, empty: dict | None = None) -> dict:
    """Corps JSON de la requête, qui doit être un objet. `empty` : valeur si le corps est vide
    (sinon un corps vide est refusé)."""
    raw = await request.body()
    if not raw.strip():
        if empty is not None:
            return dict(empty)
        raise RequestError("Corps JSON requis.")
    try:
        data = json.loads(raw)
    except ValueError as e:
        msg = f"{e.msg} (ligne {e.lineno}, colonne {e.colno})" if isinstance(e, json.JSONDecodeError) else str(e)
        raise RequestError(f"Corps JSON invalide : {msg}.")
    if not isinstance(data, dict):
        raise RequestError("Le corps doit être un objet JSON ({…}).")
    return data


def _where(i: int, extra: str = "") -> str:
    return f"messages[{i}]{extra}"


def _check_parts(parts, where: str) -> None:
    if not isinstance(parts, list):
        raise RequestError(f"{where} : 'content' doit être une chaîne ou une liste de parties.")
    for j, p in enumerate(parts):
        w = f"{where}.content[{j}]"
        if not isinstance(p, dict) or not isinstance(p.get("type"), str):
            raise RequestError(f"{w} : objet avec un champ 'type' attendu.")
        if p["type"] == "text" and not isinstance(p.get("text"), str):
            raise RequestError(f"{w} : 'text' (chaîne) attendu.")
        if p["type"] == "image_url":
            u = p.get("image_url")
            url = u.get("url") if isinstance(u, dict) else u
            if not isinstance(url, str) or not url:
                raise RequestError(f"{w} : 'image_url' doit contenir une URL.")
            check_image_url(url, w)


STR_OPTIONS = ("mode", "effort", "orchestration", "plan", "project", "prompt_id", "compression", "split")


def _check_options(body: dict) -> None:
    """Options propres à la gateway (et paramètres que la gateway lit elle-même)."""
    for k in STR_OPTIONS:
        if body.get(k) is not None and not isinstance(body[k], str):
            raise RequestError(f"'{k}' doit être une chaîne.")
    for k in ("fallbacks", "documents"):
        v = body.get(k)
        if (
            v is not None
            and not isinstance(v, str)
            and not (isinstance(v, list) and all(isinstance(x, str) for x in v))
        ):
            raise RequestError(f"'{k}' doit être une chaîne ou une liste de chaînes.")
    if body.get("prompt_vars") is not None and not isinstance(body["prompt_vars"], dict):
        raise RequestError("'prompt_vars' doit être un objet.")
    for k in ("max_tokens", "max_completion_tokens", "documents_budget", "max_context_tokens"):
        v = body.get(k)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 1):
            raise RequestError(f"'{k}' doit être un entier positif.")
    for k in ("stream", "private"):
        if body.get(k) is not None and not isinstance(body[k], bool):
            raise RequestError(f"'{k}' doit être un booléen.")


def validate_chat(body: dict) -> None:
    """Requête au format OpenAI chat/completions."""
    _check_options(body)
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        raise RequestError("'messages' est requis (liste non vide).")
    if len(msgs) > MAX_MESSAGES:
        raise RequestError(f"'messages' : {MAX_MESSAGES} messages maximum.")
    if "model" in body and body["model"] is not None and not isinstance(body["model"], str):
        raise RequestError('\'model\' doit être une chaîne (ex. "auto" ou "ollama/llama3.1:8b").')
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            raise RequestError(f"{_where(i)} : objet attendu.")
        role = m.get("role")
        if role not in OPENAI_ROLES:
            raise RequestError(f"{_where(i)} : 'role' doit valoir {', '.join(sorted(OPENAI_ROLES))}.")
        c = m.get("content")
        if c is None:
            if not (role == "assistant" and m.get("tool_calls")):
                raise RequestError(f"{_where(i)} : 'content' est requis.")
        elif not isinstance(c, str):
            _check_parts(c, _where(i))
        if role == "tool" and not isinstance(m.get("tool_call_id"), str):
            raise RequestError(f"{_where(i)} : 'tool_call_id' est requis pour un message 'tool'.")
        calls = m.get("tool_calls")
        if calls is not None:
            if not isinstance(calls, list):
                raise RequestError(f"{_where(i)} : 'tool_calls' doit être une liste.")
            for j, tc in enumerate(calls):
                fn = tc.get("function") if isinstance(tc, dict) else None
                if not (
                    isinstance(fn, dict)
                    and isinstance(fn.get("name"), str)
                    and isinstance(tc.get("id"), str)
                    and isinstance(fn.get("arguments", ""), str)
                ):
                    raise RequestError(
                        f"{_where(i, f'.tool_calls[{j}]')} : {{id, function: {{name, arguments}}}} "
                        "attendu (arguments : chaîne JSON)."
                    )
    tools = body.get("tools")
    if tools is not None and (
        not isinstance(tools, list)
        or not all(
            isinstance(t, dict) and isinstance(t.get("function"), dict) and isinstance(t["function"].get("name"), str)
            for t in tools
        )
    ):
        raise RequestError("'tools' : liste de {type: \"function\", function: {name, parameters}} attendue.")
    rf = body.get("response_format")
    if rf is not None and not isinstance(rf, dict):
        raise RequestError('\'response_format\' doit être un objet (ex. {"type": "json_object"}).')
    from .structured import check_format  # sorties structurées (16.3) ; import tardif : cycle

    check_format(rf)


def validate_messages_body(body: dict) -> None:
    """Requête au format Anthropic Messages."""
    _check_options(body)
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        raise RequestError("'messages' est requis (liste non vide).")
    if len(msgs) > MAX_MESSAGES:
        raise RequestError(f"'messages' : {MAX_MESSAGES} messages maximum.")
    if "model" in body and body["model"] is not None and not isinstance(body["model"], str):
        raise RequestError("'model' doit être une chaîne.")
    sys = body.get("system")
    if sys is not None and not isinstance(sys, str):
        _check_parts(sys, "system")
    for i, m in enumerate(msgs):
        if not isinstance(m, dict) or m.get("role") not in ANTHROPIC_ROLES:
            raise RequestError(f"{_where(i)} : objet avec 'role' user ou assistant attendu.")
        c = m.get("content")
        if isinstance(c, str):
            continue
        if not isinstance(c, list):
            raise RequestError(f"{_where(i)} : 'content' doit être une chaîne ou une liste de blocs.")
        for j, b in enumerate(c):
            w = _where(i, f".content[{j}]")
            t = b.get("type") if isinstance(b, dict) else None
            if not isinstance(t, str):
                raise RequestError(f"{w} : bloc avec un champ 'type' attendu.")
            if t == "text" and not isinstance(b.get("text"), str):
                raise RequestError(f"{w} : 'text' (chaîne) attendu.")
            if t == "image":
                s = b.get("source")
                ok = isinstance(s, dict) and (
                    (s.get("type") == "url" and isinstance(s.get("url"), str))
                    or (
                        s.get("type") == "base64"
                        and isinstance(s.get("media_type"), str)
                        and isinstance(s.get("data"), str)
                    )
                )
                if not ok:
                    raise RequestError(
                        f"{w} : 'source' {{type: url, url}} ou {{type: base64, media_type, data}} attendu."
                    )
            if t == "tool_use" and not (isinstance(b.get("id"), str) and isinstance(b.get("name"), str)):
                raise RequestError(f"{w} : 'id' et 'name' requis.")
            if t == "tool_result":
                if not isinstance(b.get("tool_use_id"), str):
                    raise RequestError(f"{w} : 'tool_use_id' requis.")
                rc = b.get("content", "")
                if not isinstance(rc, str):
                    _check_parts(rc, w)
    tools = body.get("tools")
    if tools is not None and (
        not isinstance(tools, list) or not all(isinstance(t, dict) and isinstance(t.get("name"), str) for t in tools)
    ):
        raise RequestError("'tools' : liste de {name, description, input_schema} attendue.")


def tool_arguments(raw) -> dict:
    """Arguments d'appel d'outil (chaîne JSON côté OpenAI) -> objet (côté Anthropic). Un modèle peut produire
    du JSON invalide : on le transmet tel quel plutôt que d'échouer."""
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"arguments": raw}
    return v if isinstance(v, dict) else {"arguments": v}
