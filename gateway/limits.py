"""Plafonds financiers et rate-limiting par clé virtuelle (tâche 4.3).

Champ `limits` d'une clé :
{
  "rpm": 60,                 # requêtes / minute (fenêtre glissante)
  "tpm": 100000,             # tokens / minute (fenêtre glissante)
  "tpd": 2000000,            # tokens / 24 h (fenêtre glissante)
  "budget": {"amount": 20, "currency": "EUR", "period": "month"}   # ou une liste (day/week/month)
}
- RPM/TPM/TPD : compteurs glissants partagés (gateway/counters.py : SQLite par défaut, Redis si REDIS_URL).
- Budgets : dépenses persistées en SQLite par période calendaire UTC (jour, semaine ISO, mois).
- Contrôle a priori : requête refusée (429 + Retry-After) si une limite est atteinte OU si la
  requête estimée la ferait dépasser ; dépense et tokens réels enregistrés après coup. Entre les deux,
  l'estimation est réservée (10.4) : les requêtes simultanées d'une même clé se voient mutuellement.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections import deque
from datetime import datetime, timedelta, UTC

from .counters import Counters, MemoryCounters, Reservation, pending
from .registry import usd_eur_rate

log = logging.getLogger("gateway.limits")
PERIODS = {
    "day": "day",
    "jour": "day",
    "daily": "day",
    "quotidien": "day",
    "week": "week",
    "semaine": "week",
    "weekly": "week",
    "hebdomadaire": "week",
    "month": "month",
    "mois": "month",
    "monthly": "month",
    "mensuel": "month",
}
CURRENCIES = ("EUR", "USD")
WINDOWS = {"rpm": 60, "tpm": 60, "tpd": 86400, "rpd": 86400}  # rpd : règles fournisseur (offres gratuites, 13.8)
PENDING_RETRY = 5  # refus dû aux seules requêtes en cours : elles se terminent vite


class LimitError(ValueError):
    pass


class RateLimited(Exception):
    def __init__(self, message: str, retry_after: float, limit: str):
        super().__init__(message)
        self.status = 429
        self.message = message
        self.retry_after = max(1, math.ceil(retry_after))
        self.limit = limit


# ---------- validation ----------


def _positive_int(v, name: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise LimitError(f"'{name}' doit être un entier > 0.")
    return v


def validate_limits(data) -> dict:
    if data is None:
        return {}
    if not isinstance(data, dict) or set(data) - {"rpm", "tpm", "tpd", "budget"}:
        raise LimitError("'limits' accepte rpm, tpm, tpd et budget.")
    out = {k: _positive_int(data[k], k) for k in ("rpm", "tpm", "tpd") if data.get(k) is not None}
    if data.get("budget"):
        raw = data["budget"] if isinstance(data["budget"], list) else [data["budget"]]
        budgets = []
        for b in raw:
            if not isinstance(b, dict):
                raise LimitError("Budget : {amount, currency, period}.")
            amount = b.get("amount")
            if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
                raise LimitError("Budget : 'amount' doit être > 0.")
            cur = str(b.get("currency", "EUR")).upper()
            if cur not in CURRENCIES:
                raise LimitError("Budget : 'currency' doit valoir EUR ou USD.")
            period = PERIODS.get(str(b.get("period", "month")).lower())
            if not period:
                raise LimitError("Budget : 'period' doit valoir day, week ou month.")
            budgets.append({"amount": float(amount), "currency": cur, "period": period})
        if len({b["period"] for b in budgets}) != len(budgets):
            raise LimitError("Un seul budget par période.")
        out["budget"] = budgets
    return out


# ---------- périodes calendaires ----------


def period_bounds(period: str, now: float) -> tuple[str, float]:
    """(identifiant de période, timestamp de la prochaine remise à zéro), en UTC."""
    dt = datetime.fromtimestamp(now, tz=UTC)
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return day.strftime("%Y-%m-%d"), (day + timedelta(days=1)).timestamp()
    if period == "week":
        start = day - timedelta(days=day.weekday())
        return dt.strftime("%G-W%V"), (start + timedelta(days=7)).timestamp()
    start = day.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return dt.strftime("%Y-%m"), nxt.timestamp()


def period_range(period: str, now: float) -> tuple[str, float, float]:
    """(identifiant, début, fin) de la période calendaire UTC contenant `now`."""
    pid, end = period_bounds(period, now)
    dt = datetime.fromtimestamp(now, tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        dt -= timedelta(days=dt.weekday())
    elif period == "month":
        dt = dt.replace(day=1)
    return pid, dt.timestamp(), end


def to_usd(amount: float, currency: str) -> float:
    return amount if currency == "USD" else amount / usd_eur_rate()


# ---------- compteurs glissants : gateway/counters.py (10.3) ----------


class AuthThrottle:
    """Blocage de la recherche de clés par force brute (12.3) : au-delà de AUTH_MAX_FAILURES identifiants
    invalides en AUTH_FAILURE_WINDOW secondes, l'IP est bloquée AUTH_BLOCK_SECONDS (toute authentification
    refusée, même avec une bonne clé : sinon une clé trouvée resterait utilisable).
    Échecs et blocages sont des compteurs partagés (10.3) : plusieurs workers ne multiplient pas les essais, et un
    redémarrage ne lève pas un blocage. Le blocage est un compteur de fenêtre AUTH_BLOCK_SECONDS (à la minute près)."""

    def __init__(self, clock=time.time, counters: Counters | None = None):
        self.clock = clock
        self.counters = counters or MemoryCounters()
        self.max_failures = int(os.getenv("AUTH_MAX_FAILURES", "20"))
        self.window = int(os.getenv("AUTH_FAILURE_WINDOW", "600"))
        self.block_seconds = int(os.getenv("AUTH_BLOCK_SECONDS", "900"))
        self.recent: deque[dict] = deque(maxlen=50)  # derniers blocages (affichés dans l'admin)
        self.failures = 0

    def _blocked_until(self, ip: str, now: float) -> float | None:
        items = self.counters.buckets(f"authblock:{ip}", self.block_seconds, now)
        return items[-1][0] + self.block_seconds if items else None

    def check(self, ip: str) -> None:
        now = self.clock()
        until = self._blocked_until(ip, now)
        if until is not None and until > now:
            raise RateLimited(
                "Trop d'identifiants invalides depuis votre adresse : authentification suspendue.", until - now, "auth"
            )

    def fail(self, ip: str) -> None:
        now, scope = self.clock(), f"authfail:{ip}"
        self.failures += 1
        self.counters.add(scope, self.window, 1, now)
        if self.counters.used(scope, self.window, now) >= self.max_failures:
            self.counters.add(f"authblock:{ip}", self.block_seconds, 1, now)
            self.counters.reset(scope)
            until = self._blocked_until(ip, now)
            self.recent.append({"ip": ip, "at": now, "until": until})
            log.warning(
                "IP %s bloquée %s s : %s identifiants invalides en %s s.",
                ip,
                self.block_seconds,
                self.max_failures,
                self.window,
            )

    def unblock(self, ip: str) -> bool:
        now = self.clock()
        blocked = self._blocked_until(ip, now) is not None
        self.counters.reset(f"authfail:{ip}")
        self.counters.reset(f"authblock:{ip}")
        return blocked

    def status(self) -> dict:
        now = self.clock()
        blocked = []
        for scope in self.counters.scopes("authblock:", now):
            ip = scope.split(":", 1)[1]
            until = self._blocked_until(ip, now)
            if until is not None:
                blocked.append({"ip": ip, "until": until})
        return {
            "blocked": blocked,
            "recent": list(self.recent)[-10:],
            "failures_total": self.failures,
            "max_failures": self.max_failures,
            "window_s": self.window,
            "block_s": self.block_seconds,
        }


class KeyLimiter:
    def __init__(self, store, clock=time.time, counters: Counters | None = None):
        self.store = store
        self.clock = clock
        self.counters = counters or MemoryCounters()

    @staticmethod
    def _scope(key_id: str, name: str) -> str:
        return f"key:{key_id}:{name}"

    def _used(self, key_id: str, name: str, now: float) -> float:
        return self.counters.used(self._scope(key_id, name), WINDOWS[name], now)

    # -- contrôles a priori --
    def hit_request(self, key: dict | None, limits: dict | None) -> None:
        if not key or not (limits or {}).get("rpm"):
            return
        now, scope = self.clock(), self._scope(key["id"], "rpm")
        if self.counters.used(scope, 60, now) + 1 > limits["rpm"]:
            raise RateLimited(
                f"Limite de {limits['rpm']} requêtes/minute atteinte pour cette clé.",
                self.counters.retry_after(scope, 60, now, 1, limits["rpm"]),
                "rpm",
            )
        self.counters.add(scope, 60, 1, now)

    def check_usage(
        self,
        key: dict | None,
        limits: dict | None,
        est_tokens: int,
        est_cost_usd: float,
        reservation: Reservation | None = None,
        hold_tokens: int | None = None,
    ) -> None:
        """Contrôle a priori. Les requêtes en cours de la clé comptent (10.4) : avec `reservation`, la requête
        réserve à son tour `hold_tokens` (défaut `est_tokens`) et `est_cost_usd`, dans le même pas synchrone que
        le contrôle — aucune autre requête ne peut s'intercaler entre les deux."""
        if not key or not limits:
            return
        now, kid = self.clock(), key["id"]
        held_tokens = pending(self.counters, f"keyres:{kid}:tokens", now)
        held_cost = pending(self.counters, f"keyres:{kid}:cost", now)
        for name in ("tpm", "tpd"):
            lim = limits.get(name)
            if not lim:
                continue
            scope, used = self._scope(kid, name), self._used(kid, name, now)
            if used + held_tokens + est_tokens > lim:
                unit = "minute" if name == "tpm" else "jour"
                busy = f", en cours : {int(held_tokens)}" if held_tokens else ""
                raise RateLimited(
                    f"Limite de {lim} tokens/{unit} atteinte pour cette clé "
                    f"(utilisés : {int(used)}{busy}, requête estimée : {est_tokens}).",
                    self.counters.retry_after(scope, WINDOWS[name], now, est_tokens + held_tokens, lim),
                    name,
                )
        for b in limits.get("budget", []) if est_cost_usd > 0 else []:  # 0 € (local) : jamais bloqué
            pid, reset = period_bounds(b["period"], now)
            spent = self.store.key_spend(kid, pid)
            cap = to_usd(b["amount"], b["currency"])
            if spent + est_cost_usd > cap:
                raise RateLimited(
                    f"Budget {b['period']} de {b['amount']:g} {b['currency']} "
                    f"atteint pour cette clé (remise à zéro le "
                    f"{datetime.fromtimestamp(reset, tz=UTC):%Y-%m-%d %H:%M} UTC).",
                    reset - now,
                    f"budget_{b['period']}",
                )
            if spent + held_cost + est_cost_usd > cap:  # le reste du budget est pris par les requêtes en cours
                raise RateLimited(
                    f"Budget {b['period']} de {b['amount']:g} {b['currency']} presque atteint : "
                    "le reste est réservé par les requêtes en cours de cette clé. Réessayez dans "
                    "quelques secondes.",
                    PENDING_RETRY,
                    f"budget_{b['period']}",
                )
        if reservation is not None:
            if limits.get("tpm") or limits.get("tpd"):
                reservation.hold(self.counters, f"keyres:{kid}:tokens", hold_tokens or est_tokens)
            if limits.get("budget"):
                reservation.hold(self.counters, f"keyres:{kid}:cost", est_cost_usd)

    # -- enregistrement a posteriori --
    def record(self, key: dict | None, limits: dict | None, tokens: int, cost_usd: float) -> None:
        if not key:
            return
        now = self.clock()
        for name in ("tpm", "tpd"):
            if (limits or {}).get(name) and tokens:
                self.counters.add(self._scope(key["id"], name), WINDOWS[name], tokens, now)
        if cost_usd > 0:
            for period in ("day", "week", "month"):  # toujours suivi (dashboard phase 6)
                self.store.add_key_spend(key["id"], period_bounds(period, now)[0], cost_usd)

    # -- état --
    def status(self, key_id: str, limits: dict | None) -> dict:
        now, limits = self.clock(), limits or {}
        out: dict = {}
        for name in ("rpm", "tpm", "tpd"):
            used = int(self._used(key_id, name, now))
            out[name] = {
                "limit": limits.get(name),
                "used": used,
                "remaining": (limits[name] - used) if limits.get(name) else None,
            }
        spend = {}
        for period in ("day", "week", "month"):
            pid, reset = period_bounds(period, now)
            usd = self.store.key_spend(key_id, pid)
            spend[period] = {
                "period": pid,
                "spent_usd": round(usd, 6),
                "spent_eur": round(usd * usd_eur_rate(), 6),
                "resets_at": reset,
            }
        out["spend"] = spend
        budgets = []
        for b in limits.get("budget", []):
            s = spend[b["period"]]
            spent = s["spent_eur"] if b["currency"] == "EUR" else s["spent_usd"]
            budgets.append(
                {
                    **b,
                    "spent": round(spent, 6),
                    "remaining": round(max(0.0, b["amount"] - spent), 6),
                    "resets_at": s["resets_at"],
                }
            )
        out["budgets"] = budgets
        out["reserved"] = {
            "tokens": int(pending(self.counters, f"keyres:{key_id}:tokens", now)),
            "cost_usd": round(pending(self.counters, f"keyres:{key_id}:cost", now), 6),
        }
        return out

    def headers(self, key: dict | None, limits: dict | None) -> dict[str, str]:
        if not key or not limits:
            return {}
        st, h = self.status(key["id"], limits), {}
        for name, label in (("rpm", "requests"), ("tpm", "tokens")):
            if limits.get(name):
                h[f"x-ratelimit-limit-{label}"] = str(limits[name])
                h[f"x-ratelimit-remaining-{label}"] = str(max(0, st[name]["remaining"]))
        if limits.get("tpd"):
            h["x-ratelimit-remaining-tokens-day"] = str(max(0, st["tpd"]["remaining"]))
        for b in st["budgets"]:
            h[f"x-budget-remaining-{b['period']}"] = f"{b['remaining']:.4f} {b['currency']}"
        return h
