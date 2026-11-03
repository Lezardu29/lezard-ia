"""Prompts système réutilisables et projets / workspaces (tâche 3.2).

- Prompt : modèle de texte avec variables `{{nom}}` (+ variables intégrées {{date}}, {{project}}).
- Projet : prompt système (texte libre ou prompt de la bibliothèque), réglages par défaut
  (modèle, mode, effort), documents attachés, et fenêtre de contexte propre :
  `max_context_tokens` (au-delà, les plus anciens échanges sont retirés) et `max_output_tokens`.
Les valeurs envoyées dans la requête priment toujours sur celles du projet.
"""

from __future__ import annotations

import re
from datetime import date

from .classifier import IMAGE_TOKENS, _text_and_images, estimate_tokens
from .effort import EffortError, normalize_effort
from .router import RoutingError, normalize_mode

VAR = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}")
BUILTINS = ("date", "project")
INT_FIELDS = {"documents_budget": 100, "max_context_tokens": 1024, "max_output_tokens": 16}


class WorkspaceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------- prompts ----------


def extract_vars(content: str) -> list[str]:
    return sorted({v for v in VAR.findall(content) if v not in BUILTINS})


def render(content: str, variables: dict | None, project_name: str | None = None) -> str:
    values = {
        "date": date.today().isoformat(),
        "project": project_name or "",
        **{k: str(v) for k, v in (variables or {}).items()},
    }
    missing = sorted({v for v in VAR.findall(content) if v not in values})
    if missing:
        raise WorkspaceError(400, f"Variables de prompt manquantes : {', '.join(missing)}.")
    return VAR.sub(lambda m: values[m.group(1)], content)


def validate_prompt(data: dict, partial: bool = False) -> dict:
    out = {}
    for key, required in (("name", True), ("content", True), ("description", False)):
        if key in data:
            v = data[key]
            if v is not None and not isinstance(v, str):
                raise WorkspaceError(400, f"'{key}' doit être une chaîne.")
            if required and not (v or "").strip():
                raise WorkspaceError(400, f"'{key}' ne peut pas être vide.")
            out[key] = v
        elif required and not partial:
            raise WorkspaceError(400, f"'{key}' est requis.")
    if "content" in out:
        out["variables"] = extract_vars(out["content"])
    return out


# ---------- projets ----------


def validate_project(data: dict, partial: bool = False) -> dict:
    out: dict = {}
    if "name" in data or not partial:
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise WorkspaceError(400, "'name' est requis.")
        out["name"] = name.strip()[:100]
    for key in ("description", "system_prompt", "prompt_id", "model"):
        if key in data:
            if data[key] is not None and not isinstance(data[key], str):
                raise WorkspaceError(400, f"'{key}' doit être une chaîne.")
            out[key] = data[key] or None
    if "prompt_vars" in data:
        pv = data["prompt_vars"] or {}
        if not isinstance(pv, dict):
            raise WorkspaceError(400, "'prompt_vars' doit être un objet.")
        out["prompt_vars"] = {str(k): str(v) for k, v in pv.items()}
    try:
        if data.get("mode"):
            out["mode"] = normalize_mode(data["mode"])
        elif "mode" in data:
            out["mode"] = None
        if data.get("effort"):
            out["effort"] = "auto" if str(data["effort"]).lower() == "auto" else normalize_effort(data["effort"])
        elif "effort" in data:
            out["effort"] = None
    except (RoutingError, EffortError) as e:
        raise WorkspaceError(400, getattr(e, "message", str(e)))
    if "compression" in data:
        c = data["compression"]
        if c is not None and c not in ("summary", "trim", "off"):
            raise WorkspaceError(400, "'compression' doit valoir summary, trim ou off.")
        out["compression"] = c
    for key, minimum in INT_FIELDS.items():
        if key in data:
            v = data[key]
            if v is None:
                out[key] = None
                continue
            if not isinstance(v, int) or isinstance(v, bool) or v < minimum:
                raise WorkspaceError(400, f"'{key}' doit être un entier >= {minimum}.")
            out[key] = v
    if "documents" in data:
        docs = data["documents"] or []
        if not isinstance(docs, list) or not all(isinstance(d, str) for d in docs):
            raise WorkspaceError(400, "'documents' doit être une liste d'identifiants.")
        out["documents"] = list(dict.fromkeys(docs))
    return out


# ---------- fenêtre de contexte ----------


def message_tokens(m: dict) -> int:
    text, images = _text_and_images(m.get("content"))
    extra = estimate_tokens(str(m["tool_calls"])) if m.get("tool_calls") else 0
    return estimate_tokens(text) + images * IMAGE_TOKENS + extra + 4


def trim_to_window(messages: list[dict], limit: int, reserve: int = 0) -> tuple[list[dict], int]:
    """Retire les plus anciens échanges (hors messages système) jusqu'à tenir dans
    `limit - reserve` tokens. Le dernier message est toujours conservé ; un message `tool`
    n'est jamais laissé orphelin de son appel. Retourne (messages, nb retirés)."""
    budget = limit - reserve
    system = [m for m in messages if m.get("role") in ("system", "developer")]
    convo = [m for m in messages if m.get("role") not in ("system", "developer")]
    total = sum(map(message_tokens, messages))
    dropped = 0
    while total > budget and len(convo) > 1:
        total -= message_tokens(convo.pop(0))
        dropped += 1
        while len(convo) > 1 and convo[0].get("role") == "tool":
            total -= message_tokens(convo.pop(0))
            dropped += 1
    if total > budget:
        raise WorkspaceError(
            413,
            f"La requête (~{total} tokens) dépasse la fenêtre de contexte du projet "
            f"({limit} tokens dont {reserve} réservés à la réponse).",
        )
    # remet les messages système en tête, dans leur ordre d'origine
    return system + convo, dropped
