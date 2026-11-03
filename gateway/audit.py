"""Journal d'audit de l'administration (20.2).

Qui a fait quoi, d'où, avec quel résultat : création / modification / suppression de clés, identifiants des
fournisseurs, tunnel, mode réseau local, modèles téléchargés ou supprimés, IP débloquées, sauvegardes restaurées,
double authentification…

Un middleware ASGI unique, avec la table des routes sensibles (ACTIONS) : aucune route n'est oubliée par
inadvertance, et le CORPS des requêtes n'est jamais lu — aucun secret (clé API, jeton, phrase de passe) ne peut
finir dans le journal. Une route peut préciser la cible ou un détail sans secret via `request.state.audit`.

Acteur : celui qu'a reconnu l'authentification (clé maître, compte, clé virtuelle ; session d'administration 20.1).
Les identifiants refusés (sans acteur) n'y figurent pas : l'anti force brute (12.3) s'en charge.
Alerte (6.5) : action réussie depuis une adresse jamais vue dans le journal (hors ce PC) -> notification.
Conservation : AUDIT_RETENTION_DAYS (365 par défaut), réglable dans l'administration ; 0 = illimitée.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from datetime import datetime, UTC
from collections.abc import Iterable, Iterator

from starlette.requests import Request

SETTING = "audit"
DEFAULT_RETENTION_DAYS = 365
DEFAULT_CAPTURE_DAYS = 7  # captures d'écran du contrôle du PC (21.4) : données sensibles, gardées peu de temps


def capture_days() -> float:
    """Conservation des captures d'écran du journal (AUDIT_CAPTURE_DAYS) ; 0 : aucune n'est gardée."""
    try:
        return max(0.0, float(os.getenv("AUDIT_CAPTURE_DAYS", str(DEFAULT_CAPTURE_DAYS)) or 0))
    except ValueError:
        return float(DEFAULT_CAPTURE_DAYS)


_T = r"(?P<target>[^/]+)"
_TP = r"(?P<target>.+)"

# (méthode, chemin, action, libellé)
ACTIONS: list[tuple[str, re.Pattern, str, str]] = [
    (m, re.compile(f"^{p}/?$"), a, label)
    for m, p, a, label in (
        ("POST", r"/v1/keys", "key.create", "Clé virtuelle créée"),
        ("POST", r"/v1/keys/bulk", "key.bulk", "Action groupée sur des clés"),
        ("PATCH", rf"/v1/keys/{_T}", "key.update", "Clé virtuelle modifiée"),
        ("POST", rf"/v1/keys/{_T}/rotate", "key.rotate", "Clé virtuelle renouvelée"),
        ("POST", rf"/v1/keys/{_T}/suspend", "key.suspend", "Clé virtuelle suspendue"),
        ("POST", rf"/v1/keys/{_T}/resume", "key.resume", "Clé virtuelle réactivée"),
        ("POST", rf"/v1/keys/{_T}/revoke", "key.revoke", "Clé virtuelle révoquée"),
        ("DELETE", rf"/v1/keys/{_T}", "key.delete", "Clé virtuelle supprimée"),
        ("PUT", rf"/v1/admin/credentials/{_T}", "credentials.set", "Clé d'un fournisseur enregistrée"),
        ("DELETE", rf"/v1/admin/credentials/{_T}", "credentials.delete", "Clé d'un fournisseur supprimée"),
        ("POST", r"/v1/admin/providers", "provider.add", "Fournisseur ajouté"),
        ("DELETE", rf"/v1/admin/providers/{_T}", "provider.delete", "Fournisseur retiré"),
        ("PUT", rf"/v1/admin/providers/{_T}/rules", "provider.rules", "Restrictions d'un fournisseur"),
        ("PUT", rf"/v1/admin/free-tiers/{_T}", "provider.free_tier", "Offre gratuite d'un fournisseur"),
        ("POST", r"/v1/admin/remote/start", "remote.start", "Accès distant démarré"),
        ("POST", r"/v1/admin/remote/stop", "remote.stop", "Accès distant arrêté"),
        ("POST", r"/v1/admin/local/ollama/start", "ollama.start", "Ollama démarré"),
        ("POST", r"/v1/admin/ollama/pull", "model.pull", "Téléchargement de modèle"),
        ("DELETE", rf"/v1/admin/ollama/models/{_TP}", "model.delete", "Modèle supprimé"),
        ("DELETE", rf"/v1/admin/security/blocks/{_T}", "security.unblock", "Adresse IP débloquée"),
        ("POST", r"/v1/admin/backups", "backup.create", "Sauvegarde créée"),
        ("POST", rf"/v1/admin/backups/{_T}/restore", "backup.restore", "Restauration préparée"),
        ("DELETE", r"/v1/admin/backups/restore", "backup.restore_cancel", "Restauration annulée"),
        ("POST", r"/v1/admin/encryption-key/export", "encryption_key.export", "Clé de chiffrement exportée"),
        ("POST", r"/v1/admin/restart", "gateway.restart", "Redémarrage demandé"),
        ("PUT", rf"/v1/admin/pricing/{_TP}", "pricing.set", "Tarif modifié"),
        ("DELETE", rf"/v1/admin/pricing/{_TP}", "pricing.delete", "Tarif rétabli"),
        ("POST", r"/v1/admin/pricing/import", "pricing.import", "Grille tarifaire importée"),
        ("POST", r"/v1/admin/totp/enable", "totp.enable", "Double authentification activée"),
        ("POST", r"/v1/admin/totp/disable", "totp.disable", "Double authentification désactivée"),
        ("POST", r"/v1/admin/totp/recovery-codes", "totp.recovery_codes", "Codes de secours renouvelés"),
        ("POST", r"/v1/admin/session", "admin_session.open", "Connexion à l'administration (code)"),
        ("DELETE", rf"/v1/admin/sessions/{_T}", "admin_session.revoke", "Session d'administration fermée"),
        ("DELETE", r"/v1/admin/sessions", "admin_session.revoke_all", "Toutes les sessions fermées"),
        ("POST", r"/v1/bridges", "bridge.create", "Pont créé"),
        ("DELETE", rf"/v1/bridges/{_T}", "bridge.delete", "Pont supprimé"),
        ("POST", r"/v1/alerts", "alert.create", "Alerte créée"),
        ("PATCH", rf"/v1/alerts/{_T}", "alert.update", "Alerte modifiée"),
        ("DELETE", rf"/v1/alerts/{_T}", "alert.delete", "Alerte supprimée"),
        ("PUT", r"/v1/admin/audit/settings", "audit.settings", "Conservation du journal modifiée"),
        ("GET", rf"/v1/admin/audit/{_T}/capture", "audit.capture", "Capture d'écran du contrôle du PC consultée"),
        ("PUT", r"/v1/admin/gpu/settings", "gpu.settings", "Réglages des modèles locaux"),
        ("PUT", r"/v1/admin/supervision", "supervision.settings", "Réglages de la supervision"),
        ("POST", r"/v1/admin/accounts", "account.create", "Compte créé"),
        ("PATCH", rf"/v1/admin/accounts/{_T}", "account.update", "Compte modifié"),
        ("POST", rf"/v1/admin/accounts/{_T}/rotate", "account.rotate", "Nouveau jeton de compte"),
        ("DELETE", rf"/v1/admin/accounts/{_T}", "account.delete", "Compte supprimé"),
        ("POST", r"/v1/admin/gpu/busy", "gpu.busy", "Mode PC occupé (bouton)"),
        ("POST", r"/v1/admin/gpu/eco", "gpu.eco", "Mode économie (bouton)"),
        ("PUT", r"/v1/admin/embeddings", "embeddings.choice", "Modèle d'embeddings choisi"),
        ("PUT", r"/v1/admin/voice/reading", "voice.reading", "Voix de lecture réglée"),
        ("PUT", r"/v1/admin/voice", "voice.choice", "Modèle de dictée ou de lecture vocale choisi"),
        ("POST", r"/v1/admin/gpu/unload", "gpu.unload", "Modèle local déchargé"),
        ("POST", r"/v1/admin/license", "license.activate", "Licence activée"),
        ("DELETE", r"/v1/admin/license", "license.remove", "Licence retirée"),
        ("POST", r"/v1/admin/update/install", "update.install", "Mise à jour lancée"),
    )
]
LABELS = {a: label for _, _, a, label in ACTIONS} | {
    "lan.enabled": "Mode réseau local activé",
    "lan.disabled": "Mode réseau local désactivé",
    "gpu.busy_on": "PC occupé : début",
    "gpu.busy_off": "PC occupé : fin",
    "gpu.eco_off": "Mode économie : fin",
    "outil": "Outil / contrôle du PC",
}
CATEGORIES = sorted({a.split(".")[0] for a in LABELS})


def match(method: str, path: str) -> tuple[str, str | None] | None:
    for m, pattern, action, _ in ACTIONS:
        if m == method:
            found = pattern.match(path)
            if found:
                return action, found.groupdict().get("target")
    return None


def retention_days(store) -> float:
    saved = (store.get_setting(SETTING) or {}).get("retention_days")
    if isinstance(saved, (int, float)) and saved >= 0:
        return float(saved)
    try:
        return max(0.0, float(os.getenv("AUDIT_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)) or 0))
    except ValueError:
        return float(DEFAULT_RETENTION_DAYS)


def _is_ip(value) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(str(value))
        return True
    except ValueError:
        return False


def safe_url(url) -> str | None:
    """Adresse sans identifiants ni paramètres (user:motdepasse@, ?key=…) : seuls le schéma, l'hôte et le chemin."""
    from urllib.parse import urlsplit

    try:
        u = urlsplit(str(url))
    except ValueError:
        return None
    host = u.hostname or ""
    return f"{u.scheme}://{host}{f':{u.port}' if u.port else ''}{u.path}" if host else None


def describe_actor(ctx, session: str | None) -> tuple[str, str]:
    """(type, libellé) : jamais le jeton lui-même, seulement l'identité dérivée (empreinte) ou le nom de la clé."""
    if ctx is None:
        return "anonyme", "anonyme"
    if ctx.is_admin:
        return "master", "clé maître" + (f" (session {session[:8]})" if session else "")
    if ctx.key:
        return "key", f"clé « {ctx.key.get('name') or ''} » ({ctx.key.get('id')})"
    if ctx.identity.startswith("acct:") and ctx.can_manage:
        return "account", f"compte {ctx.identity}"
    return "anonyme", ctx.identity  # sans clé, ou jeton non reconnu (accès anonyme permis)


class AuditMiddleware:
    """Enregistre les actions sensibles après leur exécution (statut final connu)."""

    def __init__(self, app, holder: dict, shared):
        self.app, self.holder, self.shared = app, holder, shared

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        found = match(scope["method"], scope["path"])
        if not found:
            return await self.app(scope, receive, send)
        status = {"code": 500}

        async def capture(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            try:
                self.record(scope, found, status["code"])
            except Exception as e:  # le journal ne doit jamais casser la requête
                from .core import log

                log.error("Journal d'audit : enregistrement impossible (%s).", e)

    def record(self, scope, found: tuple[str, str | None], status: int) -> None:
        state = scope.get("state") or {}
        ctx = state.get("gw_ctx")
        if ctx is None:  # identifiant refusé : compté par l'anti force brute
            return
        store = self.holder.get("store")
        if store is None:
            return
        action, target = found
        extra = state.get("audit") or {}
        request = Request(scope)
        ip = self.shared.client_ip(request)
        kind, actor = describe_actor(ctx, state.get("admin_session_id"))
        if kind == "anonyme" and status >= 400:  # visiteur sans clé refusé : pas une action d'admin
            return
        entry = {
            "at": time.time(),
            "actor_kind": kind,
            "actor": actor,
            "owner": ctx.identity,
            "ip": ip,
            "action": action,
            "target": str(extra.get("target") or target or "")[:200] or None,
            "status": status,
            "detail": {k: v for k, v in (extra.get("detail") or {}).items()} or None,
        }
        ok = status < 400
        known = store.audit_ip_known(ip) if ok else True
        store.add_audit(entry)
        if ok and not known and _is_ip(ip) and not self.shared.is_local_console(ip):
            master = self.holder["gw"].plans.master_key
            if master:
                from .plans import account_identity

                store.add_notification(
                    account_identity(master),
                    "warning",
                    "Action d'administration depuis une adresse inconnue",
                    f"{LABELS.get(action, action)} par {actor} depuis {ip}, adresse jamais vue dans le journal "
                    "d'audit. Si ce n'est pas vous : changez la clé maître, fermez les sessions d'administration "
                    "et consultez le journal (tableau de bord, Journal d'audit).",
                    {"event": "audit.unknown_ip", "ip": ip, "action": action},
                )


def record_system(store, action: str, detail: dict | None = None) -> None:
    """Événement sans requête (démarrage en mode réseau local…)."""
    store.add_audit(
        {
            "at": time.time(),
            "actor_kind": "system",
            "actor": "système",
            "owner": None,
            "ip": None,
            "action": action,
            "target": None,
            "status": 200,
            "detail": detail,
        }
    )


def _safe(value) -> str:
    """Cellule CSV : une valeur saisie (nom de clé…) commençant par = + - @ serait exécutée comme formule."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def csv_rows(rows: Iterable[dict]) -> Iterator[str]:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    yield "﻿"  # BOM : accents corrects dans Excel
    w.writerow(
        ["date_utc", "acteur", "type_acteur", "ip", "action", "libelle", "cible", "statut", "resultat", "detail"]
    )
    for r in rows:
        w.writerow(
            [
                datetime.fromtimestamp(r["at"], tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                _safe(r["actor"]),
                r["actor_kind"],
                _safe(r["ip"]),
                r["action"],
                LABELS.get(r["action"], r["action"]),
                _safe(r["target"]),
                r["status"],
                "ok" if r["status"] < 400 else "refusé" if r["status"] < 500 else "erreur",
                _safe(json.dumps(r["detail"], ensure_ascii=False) if r["detail"] else ""),
            ]
        )
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate()
