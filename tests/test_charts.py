import time
from datetime import datetime, UTC

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import registry as reg
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer master"}
BASE = (1000 * 2.5 + 200 * 10) / 1e6  # gpt-4o pour 1000 entrée / 200 sortie


@pytest.fixture(autouse=True)
def reset_fx():
    reg.set_live_rate(None)
    yield
    reg.set_live_rate(None)


def upstream(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/models"):
        ids = {
            "ollama": ["llama3.1:8b"],
            "openai": ["gpt-4o-mini"],
            "anthropic": ["claude-haiku-4-5"],
            "groq": ["llama-3.1-8b-instant"],
        }[request.url.host]
        return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
    if request.url.host == "anthropic":
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1000, "output_tokens": 200},
            },
        )
    return httpx.Response(
        200,
        json={
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
        },
    )


@pytest.fixture
def env():
    store = DocumentStore(":memory:")
    app = create_app(
        [
            ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
            ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k"),
            ProviderConfig("anthropic", "anthropic", "http://anthropic/v1", "cloud", api_key="k"),
            ProviderConfig("groq", "openai", "http://groq/v1", "cloud", api_key="k"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        plans=PlanResolver({"compte": "free"}, "free", master_key="master"),
        store=store,
    )
    with TestClient(app) as c:
        yield c, store


def chat(model):
    return {"model": model, "messages": [{"role": "user", "content": "Bonjour"}]}


def charts(c, headers=MASTER, **params):
    r = c.get("/v1/metrics/charts", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_charts_payload(env):
    c, _ = env
    for m in (
        "ollama/llama3.1:8b",
        "ollama/llama3.1:8b",
        "openai/gpt-4o-mini",
        "anthropic/claude-haiku-4-5",
        "groq/llama-3.1-8b-instant",
    ):
        c.post("/v1/chat/completions", headers=MASTER, json=chat(m))
    d = charts(c, since=time.time() - 86400)
    ts = d["timeseries"]
    assert ts["bucket"] == "hour" and ts["baseline_model"] == "openai/gpt-4o"
    assert 24 <= len(ts["points"]) <= 26  # 24 h en seaux horaires, vides à zéro
    assert sum(p["requests"] for p in ts["points"]) == 5
    assert sum(p["baseline_eur"] for p in ts["points"]) == pytest.approx(5 * BASE * 0.92)
    assert sum(p["cost_eur"] for p in ts["points"]) > 0
    split = {s["provider"]: s for s in d["split"]}
    assert split["local"]["requests"] == 2 and split["local"]["cost_eur"] == 0  # fournisseurs locaux regroupés
    assert set(split) == {"local", "openai", "anthropic", "groq"}
    assert len(d["scatter"]) == 5 and {p["hosting"] for p in d["scatter"]} == {"local", "cloud"}
    assert d["heatmap"]["max"] >= 1 and sum(map(sum, d["heatmap"]["counts"])) == 5
    assert len(d["heatmap"]["counts"]) == 7 and all(len(r) == 24 for r in d["heatmap"]["counts"])


def _insert(store, ts, owner_identity):
    store.insert_metrics(
        [
            {
                "ts": ts,
                "owner": owner_identity,
                "status": "ok",
                "provider": "ollama",
                "hosting": "local",
                "prompt_tokens": 10,
                "completion_tokens": 10,
                "total_tokens": 20,
                "cost_usd": 0,
                "fx_rate": 0.92,
                "latency_ms": 100,
            }
        ]
    )


def test_day_buckets_and_local_time(env):
    c, store = env
    owner = c.get("/v1/me", headers=MASTER).json()  # noqa: F841 (identité utilisée ci-dessous)
    from gateway.plans import account_identity

    ident = account_identity("master")
    # mardi 13/10/2026 23:30 UTC = mercredi 01:30 à Paris (UTC+2, getTimezoneOffset = -120)
    t = datetime(2026, 10, 13, 23, 30, tzinfo=UTC).timestamp()
    _insert(store, t, ident)
    _insert(store, t - 5 * 86400, ident)
    d = charts(c, since=t - 7 * 86400, until=t + 3600, tz_offset=-120)
    assert d["timeseries"]["bucket"] == "day" and 7 <= len(d["timeseries"]["points"]) <= 9
    heat = d["heatmap"]["counts"]
    assert heat[2][1] == 1  # mer. (index 2), 1 h locale
    utc = charts(c, since=t - 7 * 86400, until=t + 3600, tz_offset=0)["heatmap"]["counts"]
    assert utc[1][23] == 1  # mar. 23 h en UTC
    days = [p for p in d["timeseries"]["points"] if p["requests"]]
    local_day = datetime.fromtimestamp(days[-1]["t"], tz=UTC)
    assert (local_day.hour, local_day.minute) == (22, 0)  # minuit à Paris = 22:00 UTC


def test_scopes_and_validation(env):
    c, _ = env
    k = c.post("/v1/keys", headers=MASTER, json={"name": "a"}).json()
    c.post("/v1/chat/completions", headers={"Authorization": f"Bearer {k['key']}"}, json=chat("ollama/llama3.1:8b"))
    c.post("/v1/chat/completions", headers=MASTER, json=chat("ollama/llama3.1:8b"))
    assert len(charts(c)["scatter"]) == 2
    assert len(charts(c, {"Authorization": f"Bearer {k['key']}"})["scatter"]) == 1  # une clé : les siennes
    assert charts(c, {"Authorization": "Bearer compte"})["heatmap"]["max"] == 0  # autre compte
    assert c.get("/v1/metrics/charts", headers=MASTER, params={"tz_offset": 5000}).status_code == 400
    empty = charts(c, since=time.time() + 100, until=time.time() + 200)
    assert all(p["requests"] == 0 for p in empty["timeseries"]["points"]) and empty["split"] == []


def test_admin_page_has_charts(env):
    c, _ = env
    html = c.get("/admin?frame=1").text
    for chart in ("cost", "split", "scatter", "heat"):
        assert f'data-chart="{chart}"' in html
    assert html.count('class="view-toggle"') == 4  # une vue tableau par graphique
    assert c.get("/static/charts.js").status_code == 200 and c.get("/static/charts.css").status_code == 200
