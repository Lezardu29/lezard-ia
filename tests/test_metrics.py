import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import registry
from gateway.config import ProviderConfig
from gateway.fx import FxRates
from gateway.main import create_app
from gateway.metrics import MetricsRecorder
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer master"}
ECB = (
    "<gesmes:Envelope><Cube><Cube time='2026-10-16'><Cube currency='USD' rate='1.0800'/>"
    "<Cube currency='JPY' rate='160.10'/></Cube></Cube></gesmes:Envelope>"
)


@pytest.fixture(autouse=True)
def reset_fx():
    registry.set_live_rate(None)
    yield
    registry.set_live_rate(None)


# ---------- enregistreur ----------


def test_recorder_batches_and_never_blocks():
    store = DocumentStore(":memory:")

    async def scenario():
        rec = MetricsRecorder(store, batch=50, interval=0.05, maxsize=100)
        rec.start()
        t = time.perf_counter()
        for i in range(150):  # 50 de trop : abandonnées, pas bloquantes
            rec.capture({"owner": "a", "status": "ok", "total_tokens": i})
        assert time.perf_counter() - t < 0.05
        assert rec.dropped == 50 and rec.captured == 100
        await asyncio.sleep(0.3)
        await rec.stop()
        return rec.stats()

    st = asyncio.run(scenario())
    assert st == {"captured": 100, "written": 100, "dropped": 50, "pending": 0}
    rows, total = store.query_metrics("a", limit=5)
    assert total == 100 and rows[0]["ts"] > 0


def test_recorder_survives_storage_errors_and_flushes_on_stop():
    class Broken(DocumentStore):
        fail = True

        def insert_metrics(self, rows):
            if self.fail:
                self.fail = False
                raise RuntimeError("disque plein")
            super().insert_metrics(rows)

    store = Broken(":memory:")

    async def scenario():
        rec = MetricsRecorder(store, interval=0.01)
        rec.start()
        rec.capture({"owner": "a", "status": "ok"})
        await asyncio.sleep(0.1)  # 1re écriture échoue : ligne perdue, comptée
        rec.capture({"owner": "a", "status": "ok"})
        rec.interval = 10  # l'écrivain attend ; l'arrêt doit vider
        rec.capture({"owner": "a", "status": "ok"})
        await asyncio.sleep(0.05)
        await rec.stop()
        return rec.stats()

    st = asyncio.run(scenario())
    assert st["dropped"] == 1 and st["written"] == 2 and store.query_metrics("a")[1] == 2


# ---------- taux de change ----------


def test_fx_refresh_persist_and_priority(monkeypatch):
    store = DocumentStore(":memory:")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=ECB)))
    fx = FxRates(store, client)
    assert registry.usd_eur_rate() == 0.92 and fx.status()["source"] == "default"
    asyncio.run(fx.refresh())
    assert registry.usd_eur_rate() == round(1 / 1.08, 6)
    assert fx.status()["source"] == "BCE" and fx.status()["as_of"] == "2026-10-16"
    registry.set_live_rate(None)
    FxRates(store, client)  # redémarrage hors ligne : taux relu en base
    assert registry.usd_eur_rate() == round(1 / 1.08, 6)
    monkeypatch.setenv("USD_EUR_RATE", "0.5")
    assert registry.usd_eur_rate() == 0.5  # forçage manuel prioritaire
    assert "manuel" in FxRates(store, client).status()["source"]


def test_fx_rejects_bad_data():
    store = DocumentStore(":memory:")
    for body in ("<Cube currency='USD' rate='0.0001'/>", "<html>maintenance</html>"):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r, b=body: httpx.Response(200, text=b)))
        with pytest.raises(ValueError):
            asyncio.run(FxRates(store, client).refresh())
    assert registry.usd_eur_rate() == 0.92


# ---------- capture de bout en bout ----------


class Upstream:
    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "www.ecb.europa.eu":
            return httpx.Response(200, text=ECB)
        if request.url.path.endswith("/models"):
            ids = {"ollama": ["llama3.1:8b"], "openai": ["gpt-4o", "gpt-4o-mini"]}[host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        body = json.loads(request.content)
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": 600},
        }
        if body.get("stream"):
            chunks = [
                {"choices": [{"delta": {"content": "Bon"}}]},
                {"choices": [{"delta": {"content": "jour"}}]},
                {"choices": [], "usage": usage},
            ]
            return httpx.Response(200, text="".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n")
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": usage,
            },
        )


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("METRICS_FLUSH_SECONDS", "0.05")
    app = create_app(
        [
            ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
            ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(Upstream())),
        plans=PlanResolver({"compte": "pro"}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        yield c


def chat(model, **kw):
    return {"model": model, "messages": [{"role": "user", "content": "Bonjour"}], **kw}


def rows(c, headers=MASTER, **params):
    return c.get("/v1/metrics/requests", headers=headers, params=params).json()


def test_requests_are_captured_with_costs_tokens_and_timings(api):
    c = api
    k = c.post("/v1/keys", headers=MASTER, json={"name": "app", "permissions": {"orchestration": False}}).json()
    hk = {"Authorization": f"Bearer {k['key']}"}
    r = c.post("/v1/chat/completions", headers=hk, json=chat("openai/gpt-4o")).json()
    c.post("/v1/chat/completions", headers=hk, json=chat("ollama/llama3.1:8b", stream=True))
    c.post(
        "/v1/chat/completions", headers=hk, json=chat("openai/gpt-4o", orchestration="multi")
    )  # interdit à cette clé (403)

    data = rows(c)["data"]
    assert len(data) == 3
    err, local, cloud = data  # plus récent d'abord
    assert cloud["status"] == "ok" and cloud["key_id"] == k["id"] and cloud["endpoint"] == "chat"
    assert (cloud["provider"], cloud["model"], cloud["hosting"]) == ("openai", "gpt-4o", "cloud")
    assert (cloud["prompt_tokens"], cloud["completion_tokens"], cloud["cached_tokens"]) == (1000, 200, 600)
    assert cloud["cost_usd"] == pytest.approx(r["x_gateway"]["cost_usd"]) and cloud["cost_usd"] > 0
    assert cloud["billed_eur"] == pytest.approx(cloud["billed_usd"] * cloud["fx_rate"])
    assert cloud["saved_usd"] > 0  # économie du prompt caching
    assert cloud["stream"] is False and cloud["ttft_ms"] is None and cloud["latency_ms"] >= 0
    assert local["stream"] is True and local["ttft_ms"] is not None and local["cost_usd"] == 0
    assert local["hosting"] == "local" and local["task"] == "chat"
    assert err["status"] == "error" and err["error_code"] == 403 and err["model"] == "openai/gpt-4o"


def test_cache_hit_and_multi_agent_rows(api):
    c = api
    for _ in range(2):
        c.post("/v1/chat/completions", headers=MASTER, json=chat("openai/gpt-4o", cache=True))
    hit, miss = rows(c, limit=2)["data"]
    assert hit["cache_hit"] is True and hit["cost_usd"] == 0 and hit["saved_usd"] == pytest.approx(miss["cost_usd"])
    c.post("/v1/chat/completions", headers=MASTER, json=chat("auto", orchestration="multi"))
    assert rows(c, limit=1)["data"][0]["agents"] == 2  # découpage + réponse (non découpée par ce faux)


def test_scopes_filters_and_pagination(api):
    c = api
    ka = c.post("/v1/keys", headers=MASTER, json={"name": "a"}).json()
    kb = c.post("/v1/keys", headers=MASTER, json={"name": "b"}).json()
    for k, n in ((ka, 3), (kb, 2)):
        for _ in range(n):
            c.post(
                "/v1/chat/completions", headers={"Authorization": f"Bearer {k['key']}"}, json=chat("ollama/llama3.1:8b")
            )
    c.post("/v1/chat/completions", headers={"Authorization": "Bearer compte"}, json=chat("ollama/llama3.1:8b"))

    assert rows(c)["total"] == 5  # compte maître : ses 2 clés
    assert rows(c, {"Authorization": f"Bearer {ka['key']}"})["total"] == 3  # une clé : les siennes
    assert rows(c, {"Authorization": f"Bearer {ka['key']}"}, key_id=kb["id"])["total"] == 3  # pas d'évasion
    assert rows(c, {"Authorization": "Bearer compte"})["total"] == 1  # autre compte isolé
    assert rows(c, scope="all")["total"] == 6  # instance entière : clé maître
    assert (
        c.get("/v1/metrics/requests", headers={"Authorization": "Bearer compte"}, params={"scope": "all"}).status_code
        == 403
    )
    page = rows(c, limit=2, offset=2)
    assert len(page["data"]) == 2 and page["total"] == 5
    assert rows(c, key_id=kb["id"])["total"] == 2
    assert rows(c, since=time.time() + 60)["total"] == 0 and rows(c, status="error")["total"] == 0
    assert c.get("/v1/metrics/requests", headers=MASTER, params={"limit": 0}).status_code == 400


def test_fx_endpoints_and_rate_snapshot(api):
    c = api
    assert c.get("/v1/fx").json()["usd_eur"] == 0.92
    c.post("/v1/chat/completions", headers=MASTER, json=chat("openai/gpt-4o"))
    assert c.post("/v1/admin/fx/refresh", headers={"Authorization": "Bearer compte"}).status_code == 403
    st = c.post("/v1/admin/fx/refresh", headers=MASTER).json()
    assert st["source"] == "BCE" and st["usd_eur"] == round(1 / 1.08, 6)
    c.post("/v1/chat/completions", headers=MASTER, json=chat("openai/gpt-4o"))
    after, before = rows(c, limit=2)["data"]
    assert before["fx_rate"] == 0.92 and after["fx_rate"] == round(1 / 1.08, 6)  # taux figé par requête
    status = c.get("/v1/admin/metrics/status", headers=MASTER).json()
    assert status["recorder"]["written"] >= 2 and status["recorder"]["dropped"] == 0
