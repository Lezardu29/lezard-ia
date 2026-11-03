import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import registry as reg
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.pricing import PricingEngine, PricingError, breakdown, validate_price
from gateway.registry import CLOUD_MODELS, ModelRegistry, infer_local
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer master"}
SONNET = next(m for m in CLOUD_MODELS if m.id == "claude-sonnet-4-5")
LITELLM = {
    "gpt-4o": {
        "input_cost_per_token": 2.0e-6,
        "output_cost_per_token": 8.0e-6,
        "cache_read_input_token_cost": 1.0e-6,
        "max_input_tokens": 128000,
        "mode": "chat",
    },
    "claude-sonnet-4-5": {
        "input_cost_per_token": 3e-6,
        "output_cost_per_token": 15e-6,
        "cache_read_input_token_cost": 3e-7,
        "cache_creation_input_token_cost": 3.75e-6,
        "max_input_tokens": 200000,
        "mode": "chat",
    },
    "mistral/mistral-large-latest": {"input_cost_per_token": 2e-6, "output_cost_per_token": 6e-6, "mode": "chat"},
    "gpt-x-nano": {
        "input_cost_per_token": 5e-8,
        "output_cost_per_token": 4e-7,
        "max_input_tokens": 272000,
        "mode": "chat",
    },
    "text-embedding-3-small": {"input_cost_per_token": 2e-8, "output_cost_per_token": 0, "mode": "embedding"},
    "groq/llama-3.1-8b-instant": {
        "input_cost_per_token": 0.5,
        "output_cost_per_token": 0.5,
        "mode": "chat",
    },  # aberrant
}


def known(regy: ModelRegistry) -> set[str]:
    return {m.ref for m in regy.all()}


@pytest.fixture(autouse=True)
def reset_fx():
    reg.set_live_rate(None)
    yield
    reg.set_live_rate(None)


def test_validate_price():
    assert validate_price({"input_per_mtok_usd": 1, "output_per_mtok_usd": 2.5}) == {
        "input_per_mtok_usd": 1.0,
        "output_per_mtok_usd": 2.5,
    }
    for bad in (
        {},
        {"input_per_mtok_usd": 1},
        {"input_per_mtok_usd": -1, "output_per_mtok_usd": 1},
        {"input_per_mtok_usd": 5000, "output_per_mtok_usd": 1},
        {"prix": 1},
        {"input_per_mtok_usd": 1, "output_per_mtok_usd": 1, "context_window": 10},
    ):
        with pytest.raises(PricingError):
            validate_price(bad)


def test_breakdown_matches_formula_and_usage_cost():
    b = breakdown(SONNET, 10_000, 1_000, cached_tokens=8_000, cache_write_tokens=1_000)
    terms = {l["term"]: l["usd"] for l in b["lines"]}
    assert terms == pytest.approx(
        {
            "input": 1000 * 3 / 1e6,
            "cache_read": 8000 * 0.3 / 1e6,
            "cache_write": 1000 * 3.75 / 1e6,
            "output": 1000 * 15 / 1e6,
        }
    )
    usage = {
        "prompt_tokens": 10_000,
        "completion_tokens": 1_000,
        "prompt_tokens_details": {"cached_tokens": 8_000, "cache_creation_tokens": 1_000},
    }
    assert b["cost_usd"] == pytest.approx(SONNET.usage_cost(usage))
    assert b["cost_eur"] == pytest.approx(b["cost_usd"] * 0.92) and b["priced"]
    assert breakdown(infer_local("ollama", "llama3:8b"), 10**6, 10**6)["cost_eur"] == 0
    with pytest.raises(PricingError):
        breakdown(SONNET, 100, 10, cached_tokens=200)


def test_litellm_import(tmp_path):
    store, regy = DocumentStore(":memory:"), ModelRegistry()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=LITELLM)))
    engine = PricingEngine(regy, store, client)
    engine.set_price("anthropic/claude-sonnet-4-5", {"input_per_mtok_usd": 9, "output_per_mtok_usd": 9})  # manuel
    res = asyncio.run(
        engine.import_prices({"openai": ["gpt-4o", "gpt-x-nano", "text-embedding-3-small"]}, {"openai": "cloud"})
    )
    assert "openai/gpt-4o" in res["models"] and "openai/gpt-x-nano" in res["models"]
    assert "mistral/mistral-large-latest" in res["models"]
    assert "openai/text-embedding-3-small" not in res["models"] and "openai/text-embedding-3-small" not in known(regy)
    assert "anthropic/claude-sonnet-4-5" not in res["models"]  # le prix manuel prime
    assert res["skipped"] == 1  # groq aberrant ignoré
    g4 = regy.get("openai", "gpt-4o")
    assert (g4.input_per_mtok_usd, g4.output_per_mtok_usd, g4.cache_read_price) == (2.0, 8.0, 1.0)
    assert g4.quality == 0.85  # métadonnées conservées
    nano = regy.get("openai", "gpt-x-nano")
    assert nano.input_per_mtok_usd == pytest.approx(0.05) and nano.context_window == 272000
    assert regy.get("anthropic", "claude-sonnet-4-5").input_per_mtok_usd == 9
    # persistance : une nouvelle instance retrouve tout
    regy2 = ModelRegistry()
    PricingEngine(regy2, store, client)
    assert regy2.get("openai", "gpt-4o").input_per_mtok_usd == 2.0 and "openai/gpt-x-nano" in known(regy2)
    assert engine.reset_price("openai/gpt-4o") and regy.get("openai", "gpt-4o").input_per_mtok_usd == 2.5
    assert engine.reset_price("openai/gpt-x-nano") and "openai/gpt-x-nano" not in known(regy)


# ---------- API ----------


class Upstream:
    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "raw.githubusercontent.com":
            return httpx.Response(200, json=LITELLM)
        if request.url.path.endswith("/models"):
            ids = {"ollama": ["llama3.1:8b"], "openai": ["gpt-4o", "gpt-4o-mini", "gpt-9-preview"]}[host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        body = json.loads(request.content)
        cached = 600 if body["model"] == "gpt-4o-mini" else 0
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": cached},
        }
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": usage,
            },
        )


@pytest.fixture
def client():
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


def test_grid_admin_prices_and_estimate(client):
    grid = {m["ref"]: m for m in client.get("/v1/pricing").json()["data"]}
    assert grid["openai/gpt-4o"]["usd_per_mtok"]["input"] == 2.5 and grid["openai/gpt-4o"]["source"] == "builtin"
    assert grid["openai/gpt-4o"]["eur_per_mtok"]["input"] == pytest.approx(2.3)
    assert (
        client.put(
            "/v1/admin/pricing/openai/gpt-4o",
            headers={"Authorization": "Bearer compte"},
            json={"input_per_mtok_usd": 1, "output_per_mtok_usd": 1},
        ).status_code
        == 403
    )
    r = client.put(
        "/v1/admin/pricing/openai/gpt-4o", headers=MASTER, json={"input_per_mtok_usd": 2, "output_per_mtok_usd": 9}
    )
    assert r.status_code == 200 and r.json()["cost_usd"] == pytest.approx(11)  # 1M + 1M tokens
    assert client.get("/v1/pricing").json()["data"][0]["ref"]
    est = client.post(
        "/v1/pricing/estimate", json={"model": "openai/gpt-4o", "input_tokens": 1000, "output_tokens": 500}
    ).json()
    assert est["cost_usd"] == pytest.approx((1000 * 2 + 500 * 9) / 1e6)
    cmp = client.post(
        "/v1/pricing/estimate",
        json={
            "models": ["openai/gpt-4o", "ollama/llama3.1:8b", "openai/gpt-4o-mini"],
            "input_tokens": 1000,
            "output_tokens": 500,
        },
    ).json()["data"]
    assert [m["model"] for m in cmp] == ["ollama/llama3.1:8b", "openai/gpt-4o-mini", "openai/gpt-4o"]
    assert client.post("/v1/pricing/estimate", json={"model": "gpt-4o", "input_tokens": 1}).status_code == 400
    assert client.post("/v1/pricing/estimate", json={"model": "openai/gpt-4o", "input_tokens": -1}).status_code == 400
    assert client.delete("/v1/admin/pricing/openai/gpt-4o", headers=MASTER).json()["reset"]
    assert client.delete("/v1/admin/pricing/openai/gpt-4o", headers=MASTER).status_code == 400


def test_unknown_cloud_model_is_flagged_then_priced_by_import(client):
    r = client.post("/v1/chat/completions", headers=MASTER, json=chat("openai/gpt-9-preview")).json()
    assert r["x_gateway"]["pricing"] == "unknown" and r["x_gateway"]["cost_usd"] == 0
    imp = client.post("/v1/admin/pricing/import", headers=MASTER).json()
    assert imp["updated"] >= 3 and "openai/gpt-9-preview" not in imp["models"]  # absent de la source
    client.put(
        "/v1/admin/pricing/openai/gpt-9-preview",
        headers=MASTER,
        json={"input_per_mtok_usd": 1, "output_per_mtok_usd": 4},
    )
    r = client.post("/v1/chat/completions", headers=MASTER, json=chat("openai/gpt-9-preview")).json()
    assert "pricing" not in r["x_gateway"] and r["x_gateway"]["cost_usd"] == pytest.approx((1000 + 800) / 1e6)


def test_savings_breakdown(client):
    for model in ("ollama/llama3.1:8b", "ollama/llama3.1:8b", "openai/gpt-4o-mini", "openai/gpt-4o"):
        client.post("/v1/chat/completions", headers=MASTER, json=chat(model))
    for _ in range(2):  # 2e = cache de réponses
        client.post("/v1/chat/completions", headers=MASTER, json=chat("openai/gpt-4o", cache=True))
    s = client.get("/v1/savings", headers=MASTER).json()
    base = 1000 * 2.5 / 1e6 + 200 * 10 / 1e6  # gpt-4o par requête : 0.0045 $
    mini_cost = (400 * 0.15 + 600 * 0.075 + 200 * 0.6) / 1e6
    mini_cache = 600 * (0.15 - 0.075) / 1e6
    fx = 0.92
    assert s["baseline_model"] == "openai/gpt-4o" and s["totals"]["requests"] == 6
    assert s["requests_by_category"] == {"local": 2, "free": 0, "cloud": 3, "response_cache": 1}
    b = s["breakdown_eur"]
    assert b["local_models"] == pytest.approx(2 * base * fx, abs=1e-6)
    assert b["prompt_caching"] == pytest.approx(mini_cache * fx, abs=1e-6)
    assert b["smart_routing"] == pytest.approx((base - mini_cost - mini_cache) * fx, abs=1e-6)  # gpt-4o : 0
    assert b["response_cache"] == pytest.approx(base * fx, abs=1e-6)
    assert s["totals"]["saved_eur"] == pytest.approx(sum(b.values()), abs=1e-6)
    assert s["totals"]["baseline_usd"] == pytest.approx(6 * base)
    assert 0 < s["totals"]["saved_pct"] < 100
    top = s["by_model"][0]
    assert top["model"] == "ollama/llama3.1:8b" and top["saved_eur"] == pytest.approx(2 * base * fx, abs=1e-6)


def test_savings_use_rate_of_each_request(client, monkeypatch):
    client.post("/v1/chat/completions", headers=MASTER, json=chat("ollama/llama3.1:8b"))  # à 0.92
    monkeypatch.setenv("USD_EUR_RATE", "0.5")
    client.post("/v1/chat/completions", headers=MASTER, json=chat("ollama/llama3.1:8b"))  # à 0.5
    base = 1000 * 2.5 / 1e6 + 200 * 10 / 1e6
    s = client.get("/v1/savings", headers=MASTER).json()
    assert s["breakdown_eur"]["local_models"] == pytest.approx(base * 0.92 + base * 0.5, abs=1e-6)
    # périmètres : une clé ne voit que ses requêtes, l'autre compte rien
    assert client.get("/v1/savings", headers={"Authorization": "Bearer compte"}).json()["totals"]["requests"] == 0
    assert client.get("/v1/savings", headers=MASTER, params={"scope": "all"}).json()["totals"]["requests"] == 2
    monkeypatch.setenv("SAVINGS_BASELINE_MODEL", "openai/inconnu")
    assert client.get("/v1/savings", headers=MASTER).status_code == 400
