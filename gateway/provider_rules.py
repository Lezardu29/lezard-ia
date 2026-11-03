"""Restrictions sur les IA en entrée : règles par fournisseur, à l'image des clés virtuelles (4.2 / 4.3).

Règles d'un fournisseur (tout est optionnel), réglées depuis la page Modèles :
{
  "models": {"allow": ["llama3*"], "deny": ["*70b*"]},   # motifs sur l'identifiant du modèle (jokers * ?)
  "plans": ["pro"],                  # plans qui peuvent l'utiliser (free, pro) ; clé maître exemptée
  "rpm": 30, "rpd": 1000, "tpm": 6000, "tpd": 500000,      # débit maximal envoyé à ce fournisseur
  "budget": [{"amount": 10, "currency": "EUR", "period": "month"}],   # dépense maximale (coût réel)
  "max_tokens": 2048                 # plafond de tokens générés par requête
}
Un fournisseur saturé (débit ou budget atteint) sort du routage automatique : la requête bascule sur
une autre IA. Demandé explicitement, il répond 429 (débit, budget) ou 403 (modèle, plan).
"""

from __future__ import annotations

import fnmatch
import time
from datetime import datetime, UTC

from .counters import Counters, MemoryCounters, Reservation, pending
from .limits import PENDING_RETRY, WINDOWS, LimitError, RateLimited, period_bounds, to_usd, validate_limits
from .permissions import PermissionDenied
from .registry import ModelInfo, usd_eur_rate

PLANS = ("free", "pro")
KEYS = {"models", "plans", "rpm", "rpd", "tpm", "tpd", "budget", "max_tokens"}
# rpd (requêtes/jour, fenêtre glissante de 24 h) : quotas des offres gratuites (13.8), propre aux fournisseurs
RATE_RULES = (("rpm", "requêtes/minute"), ("rpd", "requêtes/jour"), ("tpm", "tokens/minute"), ("tpd", "tokens/jour"))
REQUEST_RULES = ("rpm", "rpd")


def _held(name: str) -> str:
    return "requests" if name in REQUEST_RULES else "tokens"


def _patterns(value, name: str) -> list[str]:
    if not value:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise LimitError(f"'{name}' doit être une liste de motifs (ex. \"llama3*\").")
    return [v.strip().lower() for v in value]


def validate_rules(data) -> dict:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise LimitError("Les restrictions doivent être un objet.")
    unknown = set(data) - KEYS
    if unknown:
        raise LimitError(f"Restrictions inconnues : {', '.join(sorted(unknown))}.")
    out: dict = {}
    m = data.get("models")
    if m:
        if not isinstance(m, dict) or set(m) - {"allow", "deny"}:
            raise LimitError('\'models\' doit être {"allow": [...], "deny": [...]}.')
        rules = {k: _patterns(m.get(k), f"models.{k}") for k in ("allow", "deny")}
        if any(rules.values()):
            out["models"] = {k: v for k, v in rules.items() if v}
    if data.get("plans"):
        plans = data["plans"]
        if isinstance(plans, list) and "sandbox" in plans:  # règle enregistrée avant le retrait de l'offre d'essai
            plans = [p for p in plans if p != "sandbox"] or ["pro"]  # jamais plus d'accès qu'avant
        if not isinstance(plans, list) or not plans or set(plans) - set(PLANS):
            raise LimitError("'plans' : liste parmi free, pro.")
        out["plans"] = [p for p in PLANS if p in plans]
    out.update(validate_limits({k: data[k] for k in ("rpm", "tpm", "tpd", "budget") if k in data}))
    if data.get("rpd") is not None:
        v = data["rpd"]
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise LimitError("'rpd' doit être un entier > 0 (requêtes par jour).")
        out["rpd"] = v
    if data.get("max_tokens") is not None:
        v = data["max_tokens"]
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise LimitError("'max_tokens' doit être un entier > 0.")
        out["max_tokens"] = v
    return out


class ProviderGuard:
    """Règles en mémoire + compteurs glissants ; dépenses persistées par période calendaire UTC."""

    def __init__(self, store=None, clock=time.time, counters: Counters | None = None):
        self.store = store
        self.clock = clock
        self.rules: dict[str, dict] = dict(store.list_provider_rules()) if store else {}
        self.counters = counters or MemoryCounters()  # partagés entre workers (10.3)

    def set(self, provider: str, rules: dict) -> None:
        if self.store:
            self.store.set_provider_rules(provider, rules)
        if rules:
            self.rules[provider] = rules
        else:
            self.rules.pop(provider, None)

    def forget(self, provider: str) -> None:
        self.set(provider, {})

    def _used(self, provider: str, name: str, now: float) -> float:
        return self.counters.used(f"provider:{provider}:{name}", WINDOWS[name], now)

    def _pending(self, provider: str, what: str, now: float) -> float:
        return pending(self.counters, f"provres:{provider}:{what}", now)

    def admit(self, provider: str, tokens: int, cost_usd: float) -> Reservation | None:
        """Au moment de l'ENVOI (9.6) : contrôle le débit et le budget du fournisseur en comptant les appels déjà
        partis et non réglés, puis réserve celui-ci (1 requête, tokens et coût estimés). Sans cela, N requêtes
        simultanées passaient toutes le contrôle avant que la première soit enregistrée (rpm 1 → N appels).
        Contrôle et réservation sans `await` entre eux. La réservation est libérée après `record` (ou en cas
        d'échec). Une requête plus grosse que la limite passe si rien d'autre n'est en cours (sinon jamais)."""
        r = self.rules.get(provider)
        if not r:
            return None
        now = self.clock()
        for name, unit in RATE_RULES:
            lim = r.get(name)
            if not lim:
                continue
            busy = self._used(provider, name, now) + self._pending(provider, _held(name), now)
            need = 1 if name in REQUEST_RULES else max(1, tokens)
            if busy + 1 > lim or (busy > 0 and busy + need > lim):
                raise self._limited(provider, name, unit, lim, now)
        if r.get("budget") and self.store:
            held = self._pending(provider, "cost", now)
            for b in r["budget"]:
                pid, reset = period_bounds(b["period"], now)
                spent, cap = self.store.provider_spend(provider, pid) + held, to_usd(b["amount"], b["currency"])
                if spent >= cap or (spent > 0 and spent + cost_usd > cap):
                    raise self._over_budget(provider, b, reset, now)
        res = Reservation(self.clock)
        res.hold(self.counters, f"provres:{provider}:requests", 1)
        if r.get("tpm") or r.get("tpd"):
            res.hold(self.counters, f"provres:{provider}:tokens", tokens)
        if r.get("budget"):
            res.hold(self.counters, f"provres:{provider}:cost", cost_usd)
        return res

    def _limited(self, provider: str, name: str, unit: str, lim: int, now: float) -> RateLimited:
        scope = f"provider:{provider}:{name}"
        wait = self.counters.retry_after(scope, WINDOWS[name], now, 1, lim)
        if self.counters.used(scope, WINDOWS[name], now) + 1 <= lim:
            wait = PENDING_RETRY  # saturé par des appels en cours : ils finissent vite
        return RateLimited(f"Fournisseur '{provider}' : limite de {lim} {unit} atteinte.", wait, f"provider_{name}")

    @staticmethod
    def _over_budget(provider: str, b: dict, reset: float, now: float) -> RateLimited:
        return RateLimited(
            f"Fournisseur '{provider}' : budget {b['period']} de {b['amount']:g} "
            f"{b['currency']} atteint (remise à zéro le "
            f"{datetime.fromtimestamp(reset, tz=UTC):%Y-%m-%d %H:%M} UTC).",
            reset - now,
            f"provider_budget_{b['period']}",
        )

    # -- autorisations --
    def denial(self, info: ModelInfo, plan: str, is_admin: bool = False) -> str | None:
        r = self.rules.get(info.provider)
        if not r:
            return None
        if r.get("plans") and not is_admin and plan not in r["plans"]:
            return f"Fournisseur '{info.provider}' réservé aux plans {', '.join(r['plans'])}."
        mid, rules = info.id.lower(), r.get("models") or {}
        if any(fnmatch.fnmatch(mid, p) for p in rules.get("deny", [])) or (
            rules.get("allow") and not any(fnmatch.fnmatch(mid, p) for p in rules["allow"])
        ):
            return f"Modèle '{info.ref}' non autorisé sur ce fournisseur."
        return None

    def saturation(self, provider: str) -> RateLimited | None:
        """Limite de débit ou budget déjà atteint pour ce fournisseur, appels en cours compris (None sinon)."""
        r = self.rules.get(provider)
        if not r:
            return None
        now = self.clock()
        for name, unit in RATE_RULES:
            lim = r.get(name)
            if lim and self._used(provider, name, now) + self._pending(provider, _held(name), now) + 1 > lim:
                return self._limited(provider, name, unit, lim, now)
        for b in r.get("budget", []):
            pid, reset = period_bounds(b["period"], now)
            if self.store and self.store.provider_spend(provider, pid) >= to_usd(b["amount"], b["currency"]):
                return self._over_budget(provider, b, reset, now)
        return None

    def usable(self, info: ModelInfo, plan: str, is_admin: bool = False) -> bool:
        return self.denial(info, plan, is_admin) is None and self.saturation(info.provider) is None

    def check(self, info: ModelInfo, plan: str, is_admin: bool = False) -> None:
        reason = self.denial(info, plan, is_admin)
        if reason:
            raise PermissionDenied(403, reason)
        sat = self.saturation(info.provider)
        if sat:
            raise sat

    # -- requête envoyée --
    def cap(self, provider: str, payload: dict) -> dict:
        cap = (self.rules.get(provider) or {}).get("max_tokens")
        if not cap:
            return payload
        keys = [k for k in ("max_tokens", "max_completion_tokens") if payload.get(k)]
        if not keys:
            return {**payload, "max_tokens": cap}
        return {**payload, **{k: min(payload[k], cap) for k in keys}}

    def record(self, provider: str, tokens: int, cost_usd: float) -> None:
        now, r = self.clock(), self.rules.get(provider) or {}
        for name, amount in (("rpm", 1), ("rpd", 1), ("tpm", tokens), ("tpd", tokens)):
            if r.get(name) and amount:  # compté seulement si une règle le demande
                self.counters.add(f"provider:{provider}:{name}", WINDOWS[name], amount, now)
        if cost_usd > 0 and self.store:
            for period in ("day", "week", "month"):
                self.store.add_provider_spend(provider, period_bounds(period, now)[0], cost_usd)

    def status(self, provider: str) -> dict:
        now, r = self.clock(), self.rules.get(provider) or {}
        out: dict = {}
        for name in ("rpm", "rpd", "tpm", "tpd"):
            used = int(self._used(provider, name, now)) if r.get(name) else 0
            out[name] = {"limit": r.get(name), "used": used}
        spend = {}
        for period in ("day", "week", "month"):
            pid, reset = period_bounds(period, now)
            usd = self.store.provider_spend(provider, pid) if self.store else 0.0
            spend[period] = {
                "spent_usd": round(usd, 6),
                "spent_eur": round(usd * usd_eur_rate(), 6),
                "resets_at": reset,
            }
        out["spend"] = spend
        sat = self.saturation(provider)
        out["saturated"] = sat.message if sat else None
        return out
