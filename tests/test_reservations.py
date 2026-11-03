"""Requêtes simultanées : réservation de la consommation estimée (10.4)."""

import asyncio

import httpx
import pytest

from gateway.config import ProviderConfig
from gateway.counters import MemoryCounters, RedisCounters, Reservation, SqliteCounters, pending
from gateway.limits import KeyLimiter, RateLimited
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore
from tests.test_counters import FakeRedis

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}


def fake_redis():
    r = FakeRedis()
    r.hexists = lambda key, field: field.encode() in r.h.get(key, {})
    return RedisCounters(r, fallback=MemoryCounters())


@pytest.mark.parametrize(
    "c", [MemoryCounters(), SqliteCounters(DocumentStore(":memory:")), fake_redis()], ids=lambda c: c.kind
)
def test_reservation_hold_and_release(c):
    now = [1000.0]
    r1, r2 = Reservation(lambda: now[0]), None
    r1.hold(c, "keyres:k:cost", 0.4)
    now[0] = 1090.0  # autre seau, même fenêtre
    r2 = Reservation(lambda: now[0])
    r2.hold(c, "keyres:k:cost", 0.3)
    assert pending(c, "keyres:k:cost", now[0]) == pytest.approx(0.7)
    r1.release()
    r1.release()  # idempotent
    assert pending(c, "keyres:k:cost", now[0]) == pytest.approx(0.3)
    r2.release()
    assert pending(c, "keyres:k:cost", now[0]) == pytest.approx(0.0)


def test_forgotten_reservation_expires():
    c, now = MemoryCounters(), [1000.0]
    Reservation(lambda: now[0]).hold(c, "keyres:k:tokens", 500)  # gateway arrêtée avant règlement
    assert pending(c, "keyres:k:tokens", 1000.0) == 500
    assert pending(c, "keyres:k:tokens", 1000.0 + 3600 + 60) == 0


def test_limiter_counts_requests_in_flight():
    store = DocumentStore(":memory:")
    lim = KeyLimiter(store, counters=SqliteCounters(store))
    key = store.add_key("acct:a", {"name": "k", "plan": "free", "key_prefix": "sk-app-x"}, "h1")
    limits = {"budget": [{"amount": 1.0, "currency": "USD", "period": "month"}], "tpm": 1000}
    held = [Reservation() for _ in range(2)]
    for r in held:
        lim.check_usage(key, limits, 100, 0.4, r, hold_tokens=400)
    with pytest.raises(RateLimited) as e:  # 0,8 $ réservés : 0,4 de plus dépasserait 1 $
        lim.check_usage(key, limits, 100, 0.4, Reservation())
    assert "requêtes en cours" in e.value.message and e.value.retry_after == 5
    assert lim.status(key["id"], limits)["reserved"] == {"tokens": 800, "cost_usd": 0.8}
    held[0].release()
    lim.record(key, limits, 50, 0.05)  # usage réel, bien inférieur à l'estimation
    lim.check_usage(key, limits, 100, 0.4, Reservation())  # la place est libérée
    # en cours : 400 + 100 (dernière réservation) ; réels : 50 -> 450 de plus tiennent, 500 dépassent 1000
    lim.check_usage(key, limits, 450, 0.0)
    with pytest.raises(RateLimited, match="tokens/minute"):
        lim.check_usage(key, limits, 500, 0.0)


# ---------- parcours complet : requêtes réellement simultanées ----------


class SlowUpstream:
    """Modèle cloud qui ne répond qu'une fois `go` déclenché (les requêtes se chevauchent)."""

    def __init__(self):
        self.go = asyncio.Event()
        self.started = 0
        self.fail = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]})
        self.started += 1
        await self.go.wait()
        if self.fail:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        )


async def scenario(budget_usd: float, n: int, plan_keys=None, fail=False, stream=False):
    up = SlowUpstream()
    up.fail = fail
    app = create_app(
        [ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k")],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver(plan_keys or {}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            k = (
                await c.post(
                    "/v1/keys",
                    headers=MASTER,
                    json={
                        "name": "k",
                        "limits": {"budget": {"amount": budget_usd, "currency": "USD", "period": "month"}},
                    },
                )
            ).json()
            h, key_id = {"Authorization": f"Bearer {k['key']}"}, k["id"]
            body = {
                "model": "openai/gpt-4o-mini",
                "max_tokens": 1000,
                "stream": stream,
                "messages": [{"role": "user", "content": "Bonjour"}],
            }
            tasks = [asyncio.create_task(c.post("/v1/chat/completions", headers=h, json=body)) for _ in range(n)]
            for _ in range(200):  # tous les contrôles sont passés ou refusés
                await asyncio.sleep(0.01)
                if up.started + sum(t.done() for t in tasks) >= n:
                    break
            up.go.set()
            codes = sorted(r.status_code for r in await asyncio.gather(*tasks))
            usage = (await c.get(f"/v1/keys/{key_id}/usage", headers=MASTER)).json() if key_id else None
            after = (await c.post("/v1/chat/completions", headers=h, json=body)).status_code
            return codes, usage, after


def test_parallel_requests_cannot_overshoot_budget():
    # estimation par requête ≈ 1000 tokens de sortie × 0,60 $/M ≈ 0,0006 $ : une seule tient dans 0,001 $
    codes, usage, after = asyncio.run(scenario(0.001, 4))
    assert codes == [200, 429, 429, 429]  # avant 10.4 : 4 × 200, budget dépassé
    assert usage["reserved"] == {"tokens": 0, "cost_usd": 0.0}  # tout est réglé
    assert usage["spend"]["month"]["spent_usd"] < 0.0001  # coût réel (20 tokens), bien sous l'estimation
    assert after == 200  # la place réservée a été rendue


def test_failed_requests_release_their_reservation():
    for stream in (False, True):
        codes, usage, _ = asyncio.run(scenario(0.001, 1, fail=True, stream=stream))
        assert codes[0] >= 500
        assert usage["reserved"] == {"tokens": 0, "cost_usd": 0.0}
