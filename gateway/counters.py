"""Compteurs glissants partagés (10.3) : limites RPM/TPM/TPD des clés et des fournisseurs, anti force brute.

Un compteur (`scope`, ex. « key:k_12:rpm ») est une somme glissante sur `window` secondes, rangée par seaux
de 1 s (fenêtres ≤ 1 min), 1 min (≤ 24 h) ou 1 h au-delà. Trois stockages, même comportement :
- SQLite (défaut) : table `counters` de la base de la gateway — survit aux redémarrages et est partagé entre
  plusieurs workers uvicorn de la même machine ;
- Redis (`REDIS_URL`) : partagé entre machines ; si Redis ne répond plus, repli automatique sur SQLite
  (nouvel essai toutes les REDIS_RETRY_SECONDS) plutôt que de bloquer ou de laisser tout passer ;
- mémoire : seulement sans base (tests, composants isolés).
Les seaux expirés sont purgés au fil de l'eau (au plus une fois par PURGE_INTERVAL) : mémoire et table bornées.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque

log = logging.getLogger("gateway.counters")
PURGE_INTERVAL = 60


def bucket_size(window: float) -> int:
    return 1 if window <= 60 else 60 if window <= 86400 else 3600


def _bucket(now: float, window: float) -> float:
    size = bucket_size(window)
    return now - (now % size)


class SlidingWindow:
    """Somme glissante sur `window` secondes, par seaux (voir bucket_size)."""

    def __init__(self, window: int):
        self.window = window
        self.bucket = bucket_size(window)
        self.items: deque[tuple[float, float]] = deque()
        self.total = 0.0

    def _expire(self, now: float) -> None:
        while self.items and self.items[0][0] <= now - self.window:
            self.total -= self.items.popleft()[1]

    def add(self, amount: float, now: float) -> None:
        self._expire(now)
        b = now - (now % self.bucket)
        if self.items and self.items[-1][0] == b:
            self.items[-1] = (b, self.items[-1][1] + amount)
        else:
            self.items.append((b, amount))
        self.total += amount

    def used(self, now: float) -> float:
        self._expire(now)
        return self.total

    def retry_after(self, now: float, need: float, limit: float) -> float:
        """Délai avant que `need` tienne sous `limit`."""
        self._expire(now)
        return _retry_after(list(self.items), self.total, self.window, now, need, limit)


def _retry_after(items, total: float, window: float, now: float, need: float, limit: float) -> float:
    excess, t = total + need - limit, now
    for ts, amount in items:
        if excess <= 0:
            break
        excess -= amount
        t = ts + window
    return max(1.0, t - now)


class Counters:
    kind = "?"

    def add(self, scope: str, window: float, amount: float, now: float) -> None:
        raise NotImplementedError

    def buckets(self, scope: str, window: float, now: float) -> list[tuple[float, float]]:
        """Seaux encore dans la fenêtre, du plus ancien au plus récent."""
        raise NotImplementedError

    def scopes(self, prefix: str, now: float) -> list[str]:
        """Compteurs non vides dont le nom commence par `prefix`."""
        raise NotImplementedError

    def reset(self, scope: str) -> None:
        raise NotImplementedError

    def release(self, scope: str, window: float, amount: float, at: float, now: float) -> None:
        """Retire `amount` du seau où il a été ajouté (à l'instant `at`), s'il est encore dans la fenêtre."""
        raise NotImplementedError

    def used(self, scope: str, window: float, now: float) -> float:
        return sum(a for _, a in self.buckets(scope, window, now))

    def retry_after(self, scope: str, window: float, now: float, need: float, limit: float) -> float:
        items = self.buckets(scope, window, now)
        return _retry_after(items, sum(a for _, a in items), window, now, need, limit)

    def status(self) -> dict:
        return {"backend": self.kind}


class MemoryCounters(Counters):
    kind = "memory"

    def __init__(self):
        self._w: dict[str, SlidingWindow] = {}
        self._next_purge = 0.0

    def _purge(self, now: float) -> None:
        if now >= self._next_purge:
            self._next_purge = now + PURGE_INTERVAL
            self._w = {k: w for k, w in self._w.items() if w.used(now)}

    def add(self, scope, window, amount, now):
        self._purge(now)
        w = self._w.get(scope)
        if w is None or w.window != window:
            w = self._w[scope] = SlidingWindow(window)
        w.add(amount, now)

    def buckets(self, scope, window, now):
        w = self._w.get(scope)
        if w is None:
            return []
        w.used(now)
        return list(w.items)

    def scopes(self, prefix, now):
        return sorted(k for k, w in self._w.items() if k.startswith(prefix) and w.used(now))

    def reset(self, scope):
        self._w.pop(scope, None)

    def release(self, scope, window, amount, at, now):
        w, b = self._w.get(scope), _bucket(at, window)
        if w is None or b <= now - window:
            return
        for i, (ts, a) in enumerate(w.items):
            if ts == b:
                w.items[i] = (ts, a - amount)
                w.total -= amount
                return

    def __len__(self) -> int:
        return len(self._w)


class SqliteCounters(Counters):
    kind = "sqlite"

    def __init__(self, store):
        self.store = store
        self._next_purge = 0.0

    def add(self, scope, window, amount, now):
        if now >= self._next_purge:
            self._next_purge = now + PURGE_INTERVAL
            self.store.counter_purge(now)
        b = _bucket(now, window)
        self.store.counter_add(scope, b, amount, b + window)

    def buckets(self, scope, window, now):
        return [(b, a) for b, a in self.store.counter_buckets(scope, now - window)]

    def scopes(self, prefix, now):
        return self.store.counter_scopes(prefix, now)

    def reset(self, scope):
        self.store.counter_reset(scope)

    def release(self, scope, window, amount, at, now):
        b = _bucket(at, window)
        if b > now - window:
            self.store.counter_sub(scope, b, amount)


RESERVATION_WINDOW = 3600  # une réservation oubliée (arrêt brutal) s'efface d'elle-même au bout d'1 h


class Reservation:
    """Consommation estimée d'une requête en cours (10.4) : comptée dans les limites dès le contrôle, pour que
    N requêtes simultanées ne passent pas toutes un contrôle qui ignore les autres. Libérée quand l'usage réel
    est enregistré, ou quand la requête échoue / le client se déconnecte."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.at = clock()
        self.items: list[tuple[Counters, str, float]] = []
        self.released = False

    def hold(self, counters: Counters, scope: str, amount: float) -> None:
        if amount > 0:
            counters.add(scope, RESERVATION_WINDOW, amount, self.at)
            self.items.append((counters, scope, amount))

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        now = self.clock()
        for counters, scope, amount in self.items:
            counters.release(scope, RESERVATION_WINDOW, amount, self.at, now)


def pending(counters: Counters, scope: str, now: float) -> float:
    """Total réservé par les requêtes en cours."""
    return max(0.0, counters.used(scope, RESERVATION_WINDOW, now))


def _glob_escape(s: str) -> str:
    return "".join("\\" + c if c in "*?[]\\" else c for c in s)


class RedisCounters(Counters):
    """Un hash Redis par compteur (champ = début du seau, valeur = somme), expirant avec la fenêtre."""

    kind = "redis"
    PREFIX = "gw:c:"

    def __init__(self, client, fallback: Counters, retry_seconds: float | None = None, clock=time.time):
        self.client = client
        self.fallback = fallback
        self.retry_seconds = (
            retry_seconds if retry_seconds is not None else float(os.getenv("REDIS_RETRY_SECONDS", "30"))
        )
        self.clock = clock
        self._down_until = 0.0
        self.errors = 0
        self.last_error: str | None = None

    def _call(self, name: str, *args):
        """Exécute l'opération sur Redis, ou sur le repli tant que Redis est indisponible."""
        if self.clock() < self._down_until:
            return getattr(self.fallback, name)(*args)
        try:
            return getattr(self, "_r_" + name)(*args)
        except Exception as e:  # connexion refusée, délai dépassé, Redis plein…
            self.errors += 1
            self.last_error = f"{type(e).__name__}: {e}"
            self._down_until = self.clock() + self.retry_seconds
            log.warning(
                "Redis indisponible (%s) : compteurs sur %s pendant %.0f s.",
                self.last_error,
                self.fallback.kind,
                self.retry_seconds,
            )
            return getattr(self.fallback, name)(*args)

    @property
    def degraded(self) -> bool:
        return self.clock() < self._down_until

    def add(self, scope, window, amount, now):
        return self._call("add", scope, window, amount, now)

    def buckets(self, scope, window, now):
        return self._call("buckets", scope, window, now)

    def scopes(self, prefix, now):
        return self._call("scopes", prefix, now)

    def reset(self, scope):
        return self._call("reset", scope)

    def release(self, scope, window, amount, at, now):
        return self._call("release", scope, window, amount, at, now)

    def _r_release(self, scope, window, amount, at, now):
        key, b = self.PREFIX + scope, _bucket(at, window)
        if b > now - window and self.client.hexists(key, repr(b)):
            self.client.hincrbyfloat(key, repr(b), -amount)

    def _r_add(self, scope, window, amount, now):
        key, b = self.PREFIX + scope, _bucket(now, window)
        pipe = self.client.pipeline()
        pipe.hincrbyfloat(key, repr(b), amount)
        pipe.hset(key, self.EXPIRES, repr(b + window))  # fin du seau le plus récent (cf. scopes)
        pipe.expire(key, int(window + bucket_size(window)) + 1)
        pipe.execute()

    EXPIRES = "exp"

    def _r_buckets(self, scope, window, now):
        key = self.PREFIX + scope
        live, stale = [], []
        for field, value in self.client.hgetall(key).items():
            field = field.decode() if isinstance(field, bytes) else field
            if field == self.EXPIRES:
                continue
            b = float(field)
            (live if b > now - window else stale).append((b, float(value)))
        if stale:
            self.client.hdel(key, *[repr(b) for b, _ in stale])
        return sorted(live)

    def _r_scopes(self, prefix, now):
        out = []
        for key in self.client.scan_iter(match=self.PREFIX + _glob_escape(prefix) + "*"):
            key = key.decode() if isinstance(key, bytes) else key
            expires = self.client.hget(key, self.EXPIRES)
            if expires is not None and float(expires) > now:
                out.append(key[len(self.PREFIX) :])
        return sorted(out)

    def _r_reset(self, scope):
        self.client.delete(self.PREFIX + scope)

    def status(self) -> dict:
        return {
            "backend": "redis",
            "degraded": self.degraded,
            "fallback": self.fallback.kind,
            "errors": self.errors,
            "last_error": self.last_error,
        }


def make_counters(store=None) -> Counters:
    """Redis si REDIS_URL est défini, sinon la base SQLite de la gateway, sinon la mémoire."""
    local: Counters = SqliteCounters(store) if store is not None else MemoryCounters()
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return local
    try:
        import redis
    except ImportError:
        raise RuntimeError(
            "REDIS_URL est défini mais le paquet 'redis' n'est pas installé "
            "(pip install redis), ou retirez REDIS_URL pour utiliser SQLite."
        )
    client = redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
    return RedisCounters(client, fallback=local)
