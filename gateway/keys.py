"""Clés API virtuelles `sk-app-…` (tâche 4.1).

- Le secret est généré aléatoirement (256 bits) et renvoyé UNE seule fois, à la création
  ou à la rotation ; seule son empreinte HMAC-SHA256 est stockée (+ un préfixe d'affichage).
- Une clé appartient au compte qui l'a créée : elle accède à ses projets et documents,
  avec un plan au plus égal au sien, et peut fixer un projet par défaut.
- Métadonnées : nom, description, projet, tags libres (`metadata`).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime, UTC

from .limits import LimitError, validate_limits
from .permissions import PermissionDenied, validate_permissions
from .plans import PLANS, RANK, normalize_plan

PREFIX = "sk-app-"
MAX_METADATA = 20


class VirtualKeyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# métadonnée d'une clé web (compte lezard-ia.fr) : depuis Internet, code de double authentification exigé
WEB_2FA = "lezard_web_2fa"


def generate_secret() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    pepper = os.getenv("GATEWAY_KEY_PEPPER", "").encode()
    return hmac.new(pepper, secret.encode(), hashlib.sha256).hexdigest()


def display_prefix(secret: str) -> str:
    return f"{secret[:11]}…{secret[-4:]}"


def is_virtual(token: str | None) -> bool:
    return bool(token) and token.startswith(PREFIX)


_DURATION = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$", re.IGNORECASE)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_expiry(expires_at, expires_in, now: float | None = None) -> float | None:
    """`expires_at` : ISO 8601 ou timestamp ; `expires_in` : secondes ou "30d", "12h", "2w".
    None/"" = pas d'expiration."""
    now = now if now is not None else time.time()
    if expires_in not in (None, ""):
        m = _DURATION.match(str(expires_in))
        if not m or int(m.group(1)) <= 0:
            raise VirtualKeyError(400, '\'expires_in\' : durée positive, ex. 3600, "12h", "30d", "2w".')
        return now + int(m.group(1)) * _UNITS[m.group(2).lower()]
    if expires_at in (None, ""):
        return None
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        ts = float(expires_at)
    else:
        try:
            dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError:
            raise VirtualKeyError(400, "'expires_at' : date ISO 8601 (ex. 2026-12-31T23:59:00Z) ou timestamp.")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        ts = dt.timestamp()
    if ts <= now:
        raise VirtualKeyError(400, "La date d'expiration doit être dans le futur.")
    return ts


def validate_key(data: dict, creator_plan: str, partial: bool = False) -> dict:
    out: dict = {}
    if "name" in data or not partial:
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise VirtualKeyError(400, "'name' est requis.")
        out["name"] = name.strip()[:100]
    if "description" in data:
        if data["description"] is not None and not isinstance(data["description"], str):
            raise VirtualKeyError(400, "'description' doit être une chaîne.")
        out["description"] = data["description"] or None
    if "project" in data:
        if data["project"] is not None and not isinstance(data["project"], str):
            raise VirtualKeyError(400, "'project' doit être un identifiant de projet.")
        out["project_id"] = data["project"] or None
    if "metadata" in data:
        md = data["metadata"] or {}
        if not isinstance(md, dict) or len(md) > MAX_METADATA:
            raise VirtualKeyError(400, f"'metadata' doit être un objet de {MAX_METADATA} entrées max.")
        for k, v in md.items():
            if not isinstance(v, (str, int, float, bool)) or len(str(k)) > 64 or len(str(v)) > 500:
                raise VirtualKeyError(
                    400, "Valeurs de 'metadata' : texte/nombre/booléen (clé 64, valeur 500 car. max)."
                )
        out["metadata"] = {str(k): v for k, v in md.items()}
    if "permissions" in data:
        try:
            out["permissions"] = validate_permissions(data["permissions"])
        except PermissionDenied as e:
            raise VirtualKeyError(e.status, e.message)
    if "limits" in data:
        try:
            out["limits"] = validate_limits(data["limits"])
        except LimitError as e:
            raise VirtualKeyError(400, str(e))
    if "expires_at" in data or "expires_in" in data:
        out["expires_at"] = parse_expiry(data.get("expires_at"), data.get("expires_in"))
    if "plan" in data or not partial:
        try:
            plan = normalize_plan(data.get("plan") or creator_plan)
        except Exception:
            raise VirtualKeyError(400, f"Plan inconnu. Valeurs : {', '.join(PLANS)}.")
        if RANK[plan] > RANK[creator_plan]:
            raise VirtualKeyError(403, f"Plan '{plan}' supérieur à celui du compte ({creator_plan}).")
        out["plan"] = plan
    return out
