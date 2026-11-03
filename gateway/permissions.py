"""Permissions et restrictions par clé virtuelle (tâche 4.2).

Champ `permissions` d'une clé (tout est optionnel) :
{
  "models": {"allow": ["ollama/*", "openai/gpt-4o-mini"], "deny": ["openai/gpt-4o"]},
  "hosting": "local" | "cloud",           # ex. "local" = aucune donnée envoyée au cloud
  "ip_allow": ["192.168.1.0/24", "203.0.113.7"],
  "origins": ["https://app.example.com", "*.example.com"],   # en-tête Origin / Referer
  "modes": ["economy"],                   # modes de routage permis ; un seul = mode imposé
  "orchestration": false,                 # interdit le multi-agents
  "embeddings": false,                    # interdit /v1/embeddings (permis par défaut, 16.1)
  "max_effort": "medium",                 # effort de raisonnement maximal
  "when_busy": "free",                    # PC occupé (18.4) : free (offres cloud gratuites, défaut) | cloud | refuse
  "tools": "web"                          # outils de la gateway (aucun par défaut) : basic | web | pc (contrôle du PC)
}
Les motifs de modèles s'appliquent à "provider/modèle" (jokers * ?) ; "ollama" seul = "ollama/*".
Le refus l'emporte sur l'autorisation. L'expiration est portée par la clé (`expires_at`).

Limite de « origins » (9.6) : elle ne protège que contre l'usage d'une clé par un AUTRE SITE dans un navigateur
(qui ne peut pas mentir sur Origin). Un script, curl ou un serveur forge Origin/Referer à volonté : ce n'est PAS
une authentification. Une clé publiée dans une page web reste utilisable ailleurs ; la combiner avec des limites
(RPM, budget) et, si possible, « ip_allow ».
"""

from __future__ import annotations

import fnmatch
import ipaddress
from urllib.parse import urlparse

from .effort import EFFORTS, EffortError, normalize_effort
from .registry import ModelInfo
from .router import RoutingError, normalize_mode

EFFORT_RANK = {e: i for i, e in enumerate(EFFORTS)}
KEYS = {
    "models",
    "hosting",
    "ip_allow",
    "origins",
    "modes",
    "orchestration",
    "max_effort",
    "embeddings",
    "when_busy",
    "tools",
}
TOOL_LEVELS = ("basic", "web", "pc")
WHEN_BUSY = ("free", "cloud", "refuse")


class PermissionDenied(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------- validation ----------


def _patterns(value, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise PermissionDenied(400, f"'{name}' doit être une liste de motifs (ex. \"ollama/*\").")
    return [v.strip().lower() if "/" in v else f"{v.strip().lower()}/*" for v in value]


def validate_permissions(p) -> dict:
    if p is None:
        return {}
    if not isinstance(p, dict):
        raise PermissionDenied(400, "'permissions' doit être un objet.")
    unknown = set(p) - KEYS
    if unknown:
        raise PermissionDenied(400, f"Permissions inconnues : {', '.join(sorted(unknown))}.")
    out: dict = {}
    if p.get("models"):
        m = p["models"]
        if not isinstance(m, dict) or set(m) - {"allow", "deny"}:
            raise PermissionDenied(400, '\'models\' doit être {"allow": [...], "deny": [...]}.')
        out["models"] = {k: _patterns(m.get(k), f"models.{k}") for k in ("allow", "deny") if m.get(k)}
    if p.get("hosting"):
        if p["hosting"] not in ("local", "cloud"):
            raise PermissionDenied(400, "'hosting' doit valoir local ou cloud.")
        out["hosting"] = p["hosting"]
    if p.get("ip_allow"):
        if not isinstance(p["ip_allow"], list):
            raise PermissionDenied(400, "'ip_allow' doit être une liste d'IP ou de plages CIDR.")
        try:
            out["ip_allow"] = [str(ipaddress.ip_network(str(n).strip(), strict=False)) for n in p["ip_allow"]]
        except ValueError as e:
            raise PermissionDenied(400, f"Plage IP invalide : {e}.")
    if p.get("origins"):
        if not isinstance(p["origins"], list) or not all(isinstance(o, str) and o.strip() for o in p["origins"]):
            raise PermissionDenied(400, "'origins' doit être une liste de domaines.")
        out["origins"] = [_host(o) for o in p["origins"]]
    if p.get("modes"):
        try:
            out["modes"] = list(dict.fromkeys(normalize_mode(m) for m in p["modes"]))
        except (RoutingError, TypeError, AttributeError):
            raise PermissionDenied(400, "'modes' : valeurs speed, economy, balanced.")
    if "orchestration" in p and p["orchestration"] is not None:
        if not isinstance(p["orchestration"], bool):
            raise PermissionDenied(400, "'orchestration' doit être un booléen.")
        out["orchestration"] = p["orchestration"]
    if "embeddings" in p and p["embeddings"] is not None:
        if not isinstance(p["embeddings"], bool):
            raise PermissionDenied(400, "'embeddings' doit être un booléen.")
        out["embeddings"] = p["embeddings"]
    if p.get("when_busy"):
        if p["when_busy"] not in WHEN_BUSY:
            raise PermissionDenied(400, "'when_busy' : free (offres cloud gratuites), cloud ou refuse.")
        out["when_busy"] = p["when_busy"]
    if p.get("tools"):
        if p["tools"] not in TOOL_LEVELS:
            raise PermissionDenied(
                400, "'tools' : basic (calcul, date), web (+ recherche web) ou pc (+ contrôle du PC)."
            )
        out["tools"] = p["tools"]
    if p.get("max_effort"):
        try:
            out["max_effort"] = normalize_effort(p["max_effort"])
        except EffortError as e:
            raise PermissionDenied(400, str(e))
    return out


def _host(origin: str) -> str:
    """'https://App.example.com:443/x' -> 'app.example.com' ; conserve un joker '*.'."""
    o = origin.strip().lower()
    if "://" in o:
        o = urlparse(o).hostname or ""
    return o.split("/")[0].split(":")[0]


# ---------- contrôles ----------


def check_network(perms: dict, ip: str, origin: str | None) -> None:
    if perms.get("ip_allow"):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise PermissionDenied(403, "Adresse IP non autorisée pour cette clé.")
        if not any(addr in ipaddress.ip_network(n) for n in perms["ip_allow"]):
            raise PermissionDenied(403, f"Adresse IP {ip} non autorisée pour cette clé.")
    if perms.get("origins"):
        host = _host(origin) if origin else ""
        if not host or not any(fnmatch.fnmatch(host, pat) or host == pat.lstrip("*.") for pat in perms["origins"]):
            raise PermissionDenied(
                403, "Domaine d'origine non autorisé pour cette clé (en-tête Origin/Referer requis)."
            )


def model_allowed(perms: dict | None, info: ModelInfo) -> bool:
    if not perms:
        return True
    if perms.get("hosting") and info.hosting != perms["hosting"]:
        return False
    ref = info.ref.lower()
    rules = perms.get("models") or {}
    if any(fnmatch.fnmatch(ref, pat) for pat in rules.get("deny", [])):
        return False
    allow = rules.get("allow")
    return not allow or any(fnmatch.fnmatch(ref, pat) for pat in allow)


PRIVATE_NONE = (
    "Mode privé : aucune IA locale n'est en ligne (Ollama, LM Studio… sur ce PC ou votre réseau). "
    "Démarrez-en une ou désactivez le mode privé pour cette conversation."
)


def private_permissions(perms: dict | None) -> dict:
    """Mode privé d'une requête : seules les IA hébergées en local (ce PC ou le réseau local) la reçoivent —
    routage, modèle demandé, secours, résumé de l'historique, transcription. Ajouté aux permissions de la clé ;
    marqueur `private` pour des refus explicites. Jamais enregistré sur une clé."""
    if (perms or {}).get("hosting") == "cloud":
        raise PermissionDenied(403, "Mode privé impossible : cette clé est limitée aux IA cloud.")
    return {**(perms or {}), "hosting": "local", "private": True}


def check_model(perms: dict | None, info: ModelInfo) -> None:
    if not model_allowed(perms, info):
        if (perms or {}).get("private") and info.hosting != "local":
            raise PermissionDenied(
                403,
                f"Mode privé : '{info.ref}' est une IA cloud, vos messages n'y sont pas "
                "envoyés. Choisissez une IA locale (ou « auto ») ou désactivez le mode privé.",
            )
        raise PermissionDenied(403, f"Modèle '{info.ref}' non autorisé pour cette clé.")


def filter_models(perms: dict | None, models: list[ModelInfo]) -> list[ModelInfo]:
    return [m for m in models if model_allowed(perms, m)] if perms else models


def resolve_mode(perms: dict | None, requested: str | None) -> str | None:
    """Mode effectif : refuse un mode non permis ; impose le premier mode permis par défaut."""
    modes = (perms or {}).get("modes")
    if not modes:
        return requested
    if requested:
        mode = normalize_mode(requested)
        if mode not in modes:
            raise PermissionDenied(403, f"Mode '{mode}' non autorisé pour cette clé (permis : {', '.join(modes)}).")
        return mode
    return modes[0]


def resolve_orchestration(perms: dict | None, requested: str | None) -> str | None:
    if (perms or {}).get("orchestration") is False:
        if (requested or "").lower() == "multi":
            raise PermissionDenied(403, "Orchestration multi-agents interdite pour cette clé.")
        return "single"
    return requested


def cap_effort(perms: dict | None, effort: str, explicit: bool) -> str:
    cap = (perms or {}).get("max_effort")
    if cap and EFFORT_RANK[effort] > EFFORT_RANK[cap]:
        if explicit:
            raise PermissionDenied(403, f"Effort '{effort}' non autorisé pour cette clé (max : {cap}).")
        return cap
    return effort
