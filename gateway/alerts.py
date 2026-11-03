"""Alertes de dépassement de budget en € (tâche 6.5).

Règle : {name, key_id? (sinon tout le compte), period: day|week|month, amount, currency: EUR|USD,
         thresholds: [50, 80, 100], webhook_url?, enabled}
- Évaluée en tâche de fond, après l'écriture des métriques (jamais sur le chemin d'une requête).
- Chaque palier ne se déclenche qu'UNE fois par période calendaire (UTC).
- Canaux : notification dans l'interface + webhook optionnel (Slack, Discord ou JSON générique),
  signé HMAC-SHA256 (en-tête X-Gateway-Signature: sha256=…).
- Les budgets des clés (4.3) déclenchent aussi des notifications automatiques à 80 % et 100 %.

Sécurité (SSRF) : les URL de webhook sont fournies par les utilisateurs ; seules les URL https vers
des adresses publiques sont acceptées, sauf ALERT_ALLOW_PRIVATE_WEBHOOKS=true (réseau maîtrisé).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import time
from urllib.parse import ParseResult, urlparse, urlunparse

import httpx

from .limits import PERIODS, period_range, to_usd
from .registry import usd_eur_rate

log = logging.getLogger("gateway.alerts")
PERIOD_LABELS = {"day": "aujourd'hui", "week": "cette semaine", "month": "ce mois-ci"}
KEY_BUDGET_THRESHOLDS = (80, 100)


class AlertError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def allow_private() -> bool:
    return os.getenv("ALERT_ALLOW_PRIVATE_WEBHOOKS", "false").lower() in ("1", "true", "yes")


def resolve_webhook(url: str) -> tuple[ParseResult, list[str]]:
    """Valide l'URL et résout son hôte UNE fois : (URL analysée, adresses publiques vérifiées).
    Liste vide = dérogation ALERT_ALLOW_PRIVATE_WEBHOOKS (aucune adresse imposée)."""
    if not isinstance(url, str) or len(url) > 2000:
        raise AlertError("'webhook_url' invalide.")
    u = urlparse(url.strip())
    if u.scheme not in ("https", "http") or not u.hostname:
        raise AlertError("'webhook_url' doit être une URL http(s) complète.")
    if allow_private():
        return u, []
    if u.scheme != "https":
        raise AlertError("Les webhooks doivent utiliser https.")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP)
    except (OSError, ValueError):
        raise AlertError(f"Hôte du webhook introuvable : {u.hostname}.")
    ips: list[str] = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not ip.is_global:
            raise AlertError(
                "Webhook vers une adresse privée ou locale refusé (protection SSRF). "
                "Réglez ALERT_ALLOW_PRIVATE_WEBHOOKS=true si c'est voulu."
            )
        if str(ip) not in ips:
            ips.append(str(ip))
    if not ips:
        raise AlertError(f"Hôte du webhook introuvable : {u.hostname}.")
    return u, ips


def check_webhook_url(url: str) -> str:
    """Refuse les webhooks vers le réseau interne (SSRF), sauf dérogation explicite."""
    resolve_webhook(url)
    return url.strip()


def pinned_request(u: ParseResult, ip: str) -> tuple[str, dict, dict]:
    """(URL vers l'IP vérifiée, en-têtes, extensions) : la connexion part vers `ip` et nulle part ailleurs
    (pas de seconde résolution DNS, donc pas de DNS rebinding — 9.4) ; Host et nom TLS (SNI + vérification
    du certificat) restent ceux de l'URL d'origine."""
    host = f"[{ip}]" if ":" in ip else ip
    netloc = f"{host}:{u.port}" if u.port else host
    if "@" in u.netloc:  # authentification basique de l'URL conservée
        netloc = u.netloc.rsplit("@", 1)[0] + "@" + netloc
    return (urlunparse(u._replace(netloc=netloc)), {"Host": u.netloc.rsplit("@", 1)[-1]}, {"sni_hostname": u.hostname})


def validate_rule(data: dict, partial: bool = False) -> dict:
    if not isinstance(data, dict):
        raise AlertError("Corps JSON attendu.")
    unknown = set(data) - {"name", "key_id", "period", "amount", "currency", "thresholds", "webhook_url", "enabled"}
    if unknown:
        raise AlertError(f"Champs inconnus : {', '.join(sorted(unknown))}.")
    out: dict = {}
    if "name" in data or not partial:
        if not isinstance(data.get("name"), str) or not data["name"].strip():
            raise AlertError("'name' est requis.")
        out["name"] = data["name"].strip()[:100]
    if "key_id" in data:
        if data["key_id"] is not None and not isinstance(data["key_id"], str):
            raise AlertError("'key_id' doit être un identifiant de clé.")
        out["key_id"] = data["key_id"] or None
    if "period" in data or not partial:
        p = PERIODS.get(str(data.get("period", "month")).lower())
        if not p:
            raise AlertError("'period' doit valoir day, week ou month.")
        out["period"] = p
    if "amount" in data or not partial:
        a = data.get("amount")
        if not isinstance(a, (int, float)) or isinstance(a, bool) or not 0 < a <= 1_000_000:
            raise AlertError("'amount' doit être un montant > 0.")
        out["amount"] = float(a)
    if "currency" in data or not partial:
        c = str(data.get("currency", "EUR")).upper()
        if c not in ("EUR", "USD"):
            raise AlertError("'currency' doit valoir EUR ou USD.")
        out["currency"] = c
    if "thresholds" in data or not partial:
        t = data.get("thresholds", [50, 80, 100])
        if (
            not isinstance(t, list)
            or not 1 <= len(t) <= 5
            or not all(isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= 200 for x in t)
        ):
            raise AlertError("'thresholds' : 1 à 5 pourcentages entiers entre 1 et 200.")
        out["thresholds"] = sorted(set(t))
    if "webhook_url" in data:
        out["webhook_url"] = check_webhook_url(data["webhook_url"]) if data["webhook_url"] else None
    if "enabled" in data:
        out["enabled"] = bool(data["enabled"])
    return out


def public_rule(rule: dict, spent: tuple[float, float] | None = None) -> dict:
    out = {k: v for k, v in rule.items() if k != "owner"}
    if spent is not None:
        value = spent[1] if rule["currency"] == "EUR" else spent[0]
        out["current"] = {"spent": round(value, 6), "pct": round(100 * value / rule["amount"], 1)}
    return out


def _fmt(amount: float, currency: str) -> str:
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",") + (" €" if currency == "EUR" else " $")


def webhook_payload(url: str, event: dict) -> dict:
    """Format adapté à la cible : Slack, Discord, sinon JSON générique."""
    text = f"{event['title']} — {event['message']}"
    host = urlparse(url).hostname or ""
    if host.endswith("hooks.slack.com"):
        return {"text": text}
    if host.endswith("discord.com") or host.endswith("discordapp.com"):
        return {"content": text}
    return event


class AlertEngine:
    def __init__(self, store, client: httpx.AsyncClient, clock=time.time):
        self.store = store
        self.client = client
        self.clock = clock
        self.sent = self.failed = 0
        self.webhook_transport: httpx.AsyncBaseTransport | None = None  # remplaçable dans les tests

    def rule_spend(self, rule: dict, now: float | None = None) -> tuple[str, float, float, tuple[float, float]]:
        pid, start, end = period_range(rule["period"], now or self.clock())
        return pid, start, end, self.store.spend_between(rule["owner"], start, end, rule.get("key_id"))

    async def evaluate(self, owners: set[str], key_ids: set[str]) -> int:
        """Évalue les règles des comptes concernés ; retourne le nombre d'alertes émises."""
        fired = 0
        now = self.clock()
        for rule in await asyncio.to_thread(self._rules_for, owners):
            if rule.get("key_id") and rule["key_id"] not in key_ids:
                continue  # la clé surveillée n'a rien consommé
            pid, _, _, spent = await asyncio.to_thread(self.rule_spend, rule, now)
            value = spent[1] if rule["currency"] == "EUR" else spent[0]
            pct = 100 * value / rule["amount"]
            for t in rule["thresholds"]:
                if pct >= t and await asyncio.to_thread(self.store.mark_fired, rule["id"], pid, t):
                    await self._fire(rule, t, pct, value)
                    fired += 1
        fired += await self._key_budgets(key_ids, now)
        return fired

    def _rules_for(self, owners: set[str]) -> list[dict]:
        return [r for r in self.store.list_alert_rules(enabled_only=True) if r["owner"] in owners]

    async def _fire(self, rule: dict, threshold: int, pct: float, value: float) -> None:
        level = "critical" if threshold >= 100 else "warning"
        scope = "la clé" if rule.get("key_id") else "le compte"
        title = f"Budget « {rule['name']} » : {pct:.0f} % atteint"
        message = (
            f"{_fmt(value, rule['currency'])} dépensés sur {_fmt(rule['amount'], rule['currency'])} "
            f"{PERIOD_LABELS[rule['period']]} pour {scope} (palier {threshold} %)."
        )
        event = {
            "event": "budget.threshold",
            "rule_id": rule["id"],
            "rule": rule["name"],
            "key_id": rule.get("key_id"),
            "period": rule["period"],
            "threshold": threshold,
            "percent": round(pct, 1),
            "spent": round(value, 6),
            "budget": rule["amount"],
            "currency": rule["currency"],
            "title": title,
            "message": message,
            "at": self.clock(),
        }
        await asyncio.to_thread(self.store.add_notification, rule["owner"], level, title, message, event)
        if rule.get("webhook_url"):
            await self.send(rule["webhook_url"], rule.get("webhook_secret"), event)

    async def send(self, url: str, secret: str | None, event: dict) -> bool:
        """Webhook signé ; 2 tentatives ; un échec est journalisé, jamais propagé."""
        try:
            u, ips = await asyncio.to_thread(resolve_webhook, url)  # re-vérifié à l'envoi (changement de DNS)
        except AlertError as e:
            log.warning("Webhook refusé : %s", e.message)
            self.failed += 1
            return False
        body = json.dumps(webhook_payload(url, event), ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "smart-ai-gateway-alerts"}
        if secret:
            headers["X-Gateway-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        for attempt in range(2):
            try:
                if ips:  # adresse vérifiée imposée (9.4)
                    r = await self._post_pinned(u, ips[attempt % len(ips)], body, headers)
                else:  # ALERT_ALLOW_PRIVATE_WEBHOOKS : envoi direct
                    r = await self.client.post(url, content=body, headers=headers, timeout=10)
                if r.status_code < 300:
                    self.sent += 1
                    return True
                log.warning("Webhook %s : HTTP %s", url, r.status_code)
            except httpx.HTTPError as e:
                log.warning("Webhook %s : %s", url, e)
            await asyncio.sleep(0.5 * (attempt + 1))
        self.failed += 1
        return False

    async def _post_pinned(self, u: ParseResult, ip: str, body: bytes, headers: dict) -> httpx.Response:
        """Client dédié, sans pool partagé : une connexion TLS ouverte pour un autre nom hébergé sur la même IP
        n'est jamais réutilisée ; ni proxy d'environnement ni redirection (qui mènerait ailleurs)."""
        target, host, extensions = pinned_request(u, ip)
        async with httpx.AsyncClient(
            transport=self.webhook_transport, trust_env=False, follow_redirects=False
        ) as client:
            return await client.post(
                target, content=body, headers={**headers, **host}, extensions=extensions, timeout=10
            )

    async def _key_budgets(self, key_ids: set[str], now: float) -> int:
        """Notifications automatiques des budgets de clés (4.3) à 80 % et 100 %."""
        fired = 0
        for owner, kid, name, limits in await asyncio.to_thread(self.store.key_budgets, {k for k in key_ids if k}):
            for b in limits["budget"]:
                pid, _, _ = period_range(b["period"], now)
                spent_usd = await asyncio.to_thread(self.store.key_spend, kid, pid)
                cap = to_usd(b["amount"], b["currency"])
                pct = 100 * spent_usd / cap if cap else 0
                for t in KEY_BUDGET_THRESHOLDS:
                    if pct >= t and await asyncio.to_thread(self.store.mark_fired, f"key:{kid}:{b['period']}", pid, t):
                        spent = spent_usd * usd_eur_rate() if b["currency"] == "EUR" else spent_usd
                        title = (
                            f"Clé « {name} » : budget épuisé, requêtes payantes bloquées"
                            if t >= 100
                            else f"Clé « {name} » : {pct:.0f} % du budget"
                        )
                        message = (
                            f"{_fmt(spent, b['currency'])} sur {_fmt(b['amount'], b['currency'])} "
                            f"{PERIOD_LABELS[b['period']]}."
                        )
                        await asyncio.to_thread(
                            self.store.add_notification,
                            owner,
                            "critical" if t >= 100 else "warning",
                            title,
                            message,
                            {"event": "key.budget", "key_id": kid, "threshold": t},
                        )
                        fired += 1
        return fired


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(24)
