"""Compteurs partagés et mémoire bornée (10.3)."""

import fnmatch
import os
import re
import time

import pytest

from gateway.cache import ResponseCache
from gateway.counters import MemoryCounters, RedisCounters, SqliteCounters, make_counters
from gateway.limits import AuthThrottle, KeyLimiter, RateLimited
from gateway.provider_rules import ProviderGuard
from gateway.store import DocumentStore


class FakeRedis:
    """Sous-ensemble de redis-py utilisé par RedisCounters (valeurs et clés en bytes, comme le vrai client)."""

    def __init__(self):
        self.h: dict[str, dict[bytes, float]] = {}
        self.ttl: dict[str, int] = {}
        self.down = False

    def _check(self):
        if self.down:
            raise ConnectionError("Connection refused")

    def pipeline(self):
        redis, ops = self, []

        class Pipe:
            def hincrbyfloat(self, *a):
                ops.append(("hincrbyfloat", a))

            def expire(self, *a):
                ops.append(("expire", a))

            def hset(self, *a):
                ops.append(("hset", a))

            def execute(self):
                redis._check()
                return [getattr(redis, name)(*a) for name, a in ops]

        return Pipe()

    def hincrbyfloat(self, key, field, amount):
        h = self.h.setdefault(key, {})
        f = field.encode()
        h[f] = h.get(f, 0.0) + amount
        return h[f]

    def hset(self, key, field, value):
        self.h.setdefault(key, {})[field.encode()] = value

    def hget(self, key, field):
        v = self.h.get(key, {}).get(field.encode())
        return None if v is None else str(v).encode()

    def expire(self, key, seconds):
        self.ttl[key] = seconds

    def hgetall(self, key):
        self._check()
        return {f: str(v).encode() for f, v in self.h.get(key, {}).items()}

    def hdel(self, key, *fields):
        for f in fields:
            self.h.get(key, {}).pop(f.encode(), None)

    def hlen(self, key):
        return len(self.h.get(key, {}))

    def delete(self, key):
        self._check()
        self.h.pop(key, None)

    def scan_iter(self, match):
        self._check()
        pattern = re.sub(r"\\(.)", r"[\1]", match)
        return [k.encode() for k in list(self.h) if fnmatch.fnmatchcase(k, pattern)]


def backends():
    return [
        MemoryCounters(),
        SqliteCounters(DocumentStore(":memory:")),
        RedisCounters(FakeRedis(), fallback=MemoryCounters()),
    ]


@pytest.mark.parametrize("c", backends(), ids=lambda c: c.kind)
def test_backends_behave_the_same(c):
    c.add("key:a:rpm", 60, 1, 1000.0)
    c.add("key:a:rpm", 60, 2, 1000.4)  # même seau d'1 s
    c.add("key:a:rpm", 60, 1, 1030.0)
    c.add("key:b:rpm", 60, 5, 1030.0)
    assert c.used("key:a:rpm", 60, 1030.0) == 4
    assert c.buckets("key:a:rpm", 60, 1030.0) == [(1000.0, 3.0), (1030.0, 1.0)]
    # limite 4 atteinte : il faut attendre que le seau de 1000 s sorte de la fenêtre
    assert c.retry_after("key:a:rpm", 60, 1030.0, 1, 4) == 30
    assert c.used("key:a:rpm", 60, 1060.0) == 1  # premier seau expiré
    assert c.scopes("key:", 1030.0) == ["key:a:rpm", "key:b:rpm"]
    assert c.scopes("key:a", 1030.0) == ["key:a:rpm"]
    c.reset("key:a:rpm")
    assert c.used("key:a:rpm", 60, 1030.0) == 0 and c.scopes("key:", 1030.0) == ["key:b:rpm"]
    assert c.scopes("key:", 2000.0) == []  # tout a expiré
    # fenêtre 24 h : seaux d'une minute
    c.add("key:a:tpd", 86400, 100, 1000.0)
    c.add("key:a:tpd", 86400, 50, 1019.0)
    assert c.buckets("key:a:tpd", 86400, 1020.0) == [(960.0, 150.0)]


def test_like_wildcards_in_prefix_are_literal():
    c = SqliteCounters(DocumentStore(":memory:"))
    c.add("authblock:10_0_0_1", 900, 1, 1000.0)
    c.add("authblock:10a0a0a1", 900, 1, 1000.0)
    assert c.scopes("authblock:10_", 1000.0) == ["authblock:10_0_0_1"]


def test_memory_counters_are_purged():
    c = MemoryCounters()
    for i in range(5000):
        c.add(f"ip:10.0.{i // 250}.{i % 250}", 3600, 1, 1000.0)
    assert len(c) == 5000
    c.add("ip:autre", 3600, 1, 1000.0 + 3600 + 61)  # passage suivant : les fenêtres vides partent
    assert len(c) == 1


def test_sqlite_counters_table_is_purged():
    store = DocumentStore(":memory:")
    c = SqliteCounters(store)
    for i in range(300):
        c.add(f"key:k{i}:rpm", 60, 1, 1000.0)
    c.add("key:k0:rpm", 60, 1, 1200.0)
    rows = store._db.execute("SELECT COUNT(*) FROM counters").fetchone()[0]
    assert rows == 1


# ---------- survie au redémarrage et partage entre workers ----------


def test_limits_survive_restart_and_are_shared_between_workers(tmp_path):
    path = str(tmp_path / "g.db")
    now = [1000.0]
    key, limits = {"id": "k1"}, {"rpm": 2, "tpm": 1000}

    def worker():  # un worker uvicorn = son propre DocumentStore
        store = DocumentStore(path)
        return KeyLimiter(store, lambda: now[0], SqliteCounters(store))

    w1, w2 = worker(), worker()
    w1.hit_request(key, limits)
    w2.hit_request(key, limits)
    with pytest.raises(RateLimited):  # 3e requête : refusée quel que soit le worker
        w1.hit_request(key, limits)
    w2.record(key, limits, 900, 0)
    with pytest.raises(RateLimited, match="tokens/minute"):
        w1.check_usage(key, limits, 200, 0)
    restarted = worker()  # redémarrage : la limite tient toujours
    with pytest.raises(RateLimited):
        restarted.hit_request(key, limits)
    assert restarted.status("k1", limits)["tpm"]["used"] == 900
    now[0] += 61
    restarted.hit_request(key, limits)  # fenêtre écoulée


def test_auth_block_survives_restart_and_is_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_MAX_FAILURES", "3")
    path, now = str(tmp_path / "g.db"), [1000.0]
    t1 = AuthThrottle(lambda: now[0], SqliteCounters(DocumentStore(path)))
    t2 = AuthThrottle(lambda: now[0], SqliteCounters(DocumentStore(path)))
    t1.fail("203.0.113.9")
    t2.fail("203.0.113.9")  # deux workers : les échecs s'additionnent
    t1.fail("203.0.113.9")
    with pytest.raises(RateLimited):
        t2.check("203.0.113.9")
    t3 = AuthThrottle(lambda: now[0], SqliteCounters(DocumentStore(path)))  # redémarrage
    with pytest.raises(RateLimited) as e:
        t3.check("203.0.113.9")
    assert 840 <= e.value.retry_after <= 900
    assert [b["ip"] for b in t3.status()["blocked"]] == ["203.0.113.9"]
    assert t3.unblock("203.0.113.9") and not t3.unblock("203.0.113.9")
    t1.check("203.0.113.9")
    now[0] += 901
    assert t1.status()["blocked"] == []


def test_provider_windows_are_shared():
    store = DocumentStore(":memory:")
    now = [1000.0]
    g1 = ProviderGuard(store, lambda: now[0], SqliteCounters(store))
    g1.set("openai", {"rpm": 2})
    g2 = ProviderGuard(store, lambda: now[0], SqliteCounters(store))
    g1.record("openai", 10, 0)
    g2.record("openai", 10, 0)
    assert g1.saturation("openai") and g2.status("openai")["rpm"]["used"] == 2
    g1.record("groq", 10, 0)  # pas de règle : rien n'est écrit
    assert store._db.execute("SELECT COUNT(*) FROM counters WHERE scope LIKE 'provider:groq%'").fetchone()[0] == 0


# ---------- Redis ----------


def test_redis_falls_back_to_sqlite_then_recovers():
    redis, store, now = FakeRedis(), DocumentStore(":memory:"), [1000.0]
    c = RedisCounters(redis, fallback=SqliteCounters(store), retry_seconds=30, clock=lambda: now[0])
    c.add("key:a:rpm", 60, 1, now[0])
    assert redis.h["gw:c:key:a:rpm"][b"1000.0"] == 1 and not c.degraded
    redis.down = True
    c.add("key:a:rpm", 60, 1, now[0])  # panne : écrit dans SQLite, sans erreur
    assert c.degraded and c.status()["errors"] == 1 and "Connection refused" in c.status()["last_error"]
    assert c.used("key:a:rpm", 60, now[0]) == 1  # lu dans le repli
    now[0] += 31
    redis.down = False
    c.add("key:a:rpm", 60, 1, now[0])
    assert not c.degraded and c.used("key:a:rpm", 60, now[0]) == 2  # de retour sur Redis


def test_redis_scopes_with_special_characters():
    c = RedisCounters(FakeRedis(), fallback=MemoryCounters())
    c.add("authblock:[::1]", 900, 1, 1000.0)
    c.add("authblock:x*", 900, 1, 1000.0)
    c.add("authblock:xy", 900, 1, 1000.0)
    assert c.scopes("authblock:x*", 1000.0) == ["authblock:x*"]
    assert c.scopes("authblock:[", 1000.0) == ["authblock:[::1]"]


@pytest.mark.skipif(not os.getenv("REDIS_TEST_URL"), reason="REDIS_TEST_URL non défini (vrai serveur Redis)")
def test_real_redis():
    import redis

    client = redis.Redis.from_url(os.environ["REDIS_TEST_URL"])
    c = RedisCounters(client, fallback=MemoryCounters())
    scope = f"test:{time.time()}"
    now = time.time()
    c.add(scope, 60, 2, now)
    c.add(scope, 60, 1, now)
    assert c.used(scope, 60, now) == 3 and c.scopes(scope, now) == [scope] and not c.degraded
    assert 0 < client.ttl(RedisCounters.PREFIX + scope) <= 62
    c.reset(scope)
    assert c.used(scope, 60, now) == 0


def test_make_counters(monkeypatch):
    store = DocumentStore(":memory:")
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert make_counters(store).kind == "sqlite" and make_counters().kind == "memory"
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # rien n'écoute : repli dès le premier appel
    c = make_counters(store)
    assert c.kind == "redis" and c.fallback.kind == "sqlite"
    c.add("key:a:rpm", 60, 1, 1000.0)
    assert c.degraded and c.used("key:a:rpm", 60, 1000.0) == 1


def test_app_uses_shared_counters(tmp_path, monkeypatch):
    """Limite de requêtes d'une clé (rpm) : elle tient après un redémarrage de l'application."""
    import httpx
    from fastapi.testclient import TestClient
    from gateway.config import ProviderConfig
    from gateway.main import create_app
    from gateway.plans import PlanResolver

    monkeypatch.delenv("REDIS_URL", raising=False)
    master = "gw-master-0123456789abcdefghijklmnop"
    path = str(tmp_path / "g.db")

    def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
        return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1,
            "total_tokens": 2}})  # fmt: skip

    def app():
        return create_app(
            [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
            httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
            plans=PlanResolver({}, "free", master_key=master),
            store=DocumentStore(path),
        )

    body = {"model": "ollama/llama3.1:8b", "messages": [{"role": "user", "content": "salut"}]}
    with TestClient(app()) as c:
        admin = {"Authorization": f"Bearer {master}"}
        key = c.post("/v1/keys", headers=admin, json={"name": "app", "limits": {"rpm": 2}}).json()["key"]
        h = {"Authorization": f"Bearer {key}"}
        assert [c.post("/v1/chat/completions", headers=h, json=body).status_code for _ in range(3)] == [200, 200, 429]
        assert c.get("/v1/admin/remote", headers=admin).json()["counters"] == {"backend": "sqlite"}
    with TestClient(app()) as c:  # redémarrage
        assert c.post("/v1/chat/completions", headers=h, json=body).status_code == 429


# ---------- mémoire bornée ----------


def test_response_cache_stats_and_entries_bounded():
    now = [1000.0]
    cache = ResponseCache(max_entries=100, clock=lambda: now[0])
    cache.max_owners = 50
    for i in range(200):
        cache.get(f"k{i}", f"acct:{i}")
    assert len(cache._stats) == 50
    cache.get("x", "acct:199")
    assert "acct:199" in cache._stats
    resp = {"choices": [{"message": {"content": "ok"}}]}
    for i in range(10):
        cache.put(f"e{i}", "o", resp, {}, ttl=10)
    now[0] += 120
    cache.put("neuf", "o", resp, {}, ttl=10)  # passage de purge : les expirées sont libérées
    assert list(cache._data) == ["neuf"]
