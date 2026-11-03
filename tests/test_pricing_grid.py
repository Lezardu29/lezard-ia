"""Grille tarifaire (10.7) : grille intégrée à jour, tarifs publiés par les fournisseurs, modèles utilisés sans
tarif signalés à l'administrateur et import tenté dès leur premier usage."""

import asyncio
import time
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.pricing import PricingEngine, item_prices
from gateway.registry import BUILTIN_PRICES_DATE, CLOUD_MODELS, ModelRegistry
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
ACCOUNT = {"Authorization": "Bearer ct-compte-0123456789abcdefghijklmno"}


def test_builtin_grid_is_recent_and_consistent():
    assert (date.today() - date.fromisoformat(BUILTIN_PRICES_DATE)).days < 400
    refs = [m.ref for m in CLOUD_MODELS]
    assert len(refs) == len(set(refs))
    for m in CLOUD_MODELS:
        assert m.priced and m.input_per_mtok_usd > 0 and m.output_per_mtok_usd > 0, m.ref
        assert m.cache_read_price <= m.input_per_mtok_usd <= m.cache_write_price * 1.0001, m.ref
        assert m.context_window >= 32_000, m.ref
    by = {m.ref: m for m in CLOUD_MODELS}
    # quelques valeurs relevées le 2026-10-19 (base LiteLLM)
    assert (
        by["mistral/mistral-large-latest"].input_per_mtok_usd,
        by["mistral/mistral-large-latest"].output_per_mtok_usd,
    ) == (0.5, 1.5)
    assert by["anthropic/claude-sonnet-5-5"].cache_read_price == pytest.approx(0.1)
    assert by["anthropic/claude-haiku-5-5"].cache_write_price == pytest.approx(0.125)
    assert by["openai/gpt-5-mini"].cache_read_price == pytest.approx(0.025)
    assert by["groq/openai/gpt-oss-120b"].cache_read_price == pytest.approx(0.075)
    assert by["groq/llama-3.1-8b-instant"].cache_read_price == by["groq/llama-3.1-8b-instant"].input_per_mtok_usd


def test_prices_published_in_models_listing():
    assert item_prices(
        {"pricing": {"prompt": "0.0000003", "completion": "0.0000025", "input_cache_read": "0.00000003"}}
    ) == {"input_per_mtok_usd": 0.3, "output_per_mtok_usd": 2.5, "cache_read_per_mtok_usd": 0.03}
    assert item_prices({"pricing": {"input": 0.88, "output": 0.88}}) == {
        "input_per_mtok_usd": 0.88,
        "output_per_mtok_usd": 0.88,
    }  # Together : $/M
    for bad in (
        {},
        {"pricing": "gratuit"},
        {"pricing": {"prompt": "-1", "completion": "-1"}},  # prix variable
        {"pricing": {"prompt": "0", "completion": "0"}},  # variante gratuite
        {"pricing": {"prompt": "0.01", "completion": "0.01"}},  # 10 000 $/M : aberrant
        {"pricing": {"prompt": "abc"}},
    ):
        assert item_prices(bad) is None, bad


ROUTER_MODELS = {
    "data": [
        {"id": "acme/brainy-1", "context_length": 64000, "pricing": {"prompt": "0.000001", "completion": "0.000004"}},
        {"id": "acme/brainy-1:beta", "pricing": {"prompt": "-1", "completion": "-1"}},  # prix variable : inconnu
        {"id": "openai/gpt-4o-mini", "pricing": {"prompt": "0.00000099", "completion": "0.00000099"}},
    ]
}


class Upstream:
    def __init__(self, litellm=None):
        self.litellm = litellm or {}
        self.litellm_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "prices.test":
            self.litellm_calls += 1
            return httpx.Response(200, json=self.litellm)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=ROUTER_MODELS)
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 1000},
            },
        )


def make(up, store=None):
    return create_app(
        [ProviderConfig("openrouter", "openai", "https://router.test/v1", "cloud", api_key="k")],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({"ct-compte-0123456789abcdefghijklmno": "free"}, "free", master_key=MASTER_KEY),
        store=store or DocumentStore(":memory:"),
    )


def ask(c, model, headers=MASTER):
    return c.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": model, "messages": [{"role": "user", "content": "Bonjour"}]},
    )


def test_provider_published_prices_are_used_but_never_override_admin():
    with TestClient(make(Upstream())) as c:
        grid = {g["ref"]: g for g in c.get("/v1/pricing").json()["data"]}
        brainy = grid["openrouter/acme/brainy-1"]
        assert brainy["source"] == "provider" and brainy["usd_per_mtok"]["input"] == 1.0
        assert brainy["context_window"] == 64000
        assert "openrouter/acme/brainy-1:beta" not in grid  # prix variable : « sans tarif »
        # modèle de la grille intégrée : le prix publié par le fournisseur ne remplace pas un tarif connu
        assert grid["openrouter/openai/gpt-4o-mini"]["usd_per_mtok"]["input"] == 0.15
        meta = ask(c, "openrouter/acme/brainy-1").json()["x_gateway"]
        assert meta["cost_usd"] == pytest.approx(0.005) and "pricing" not in meta  # 1000 × 1 $ + 1000 × 4 $ / M
        assert (
            c.put(
                "/v1/admin/pricing/openrouter/acme/brainy-1",
                headers=MASTER,
                json={"input_per_mtok_usd": 2, "output_per_mtok_usd": 2},
            ).status_code
            == 200
        )
        assert c.get("/v1/admin/providers", headers=MASTER).status_code == 200  # nouvelle détection
        assert ask(c, "openrouter/acme/brainy-1").json()["x_gateway"]["cost_usd"] == pytest.approx(0.004)


def test_import_does_not_replace_provider_published_prices():
    store, regy = DocumentStore(":memory:"), ModelRegistry()
    regy.announce("openrouter", "acme/brainy-1", {"input_per_mtok_usd": 1.0, "output_per_mtok_usd": 4.0})
    data = {"openrouter/acme/brainy-1": {"input_cost_per_token": 9e-6, "output_cost_per_token": 9e-6, "mode": "chat"}}
    engine = PricingEngine(
        regy, store, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data)))
    )
    res = asyncio.run(engine.import_prices({"openrouter": ["acme/brainy-1"]}, {"openrouter": "cloud"}))
    assert "openrouter/acme/brainy-1" not in res["models"]
    assert regy.get("openrouter", "acme/brainy-1").input_per_mtok_usd == 1.0


def test_unknown_priced_models_are_reported_then_imported(monkeypatch):
    monkeypatch.setenv("PRICING_AUTO_REFRESH", "true")
    monkeypatch.setenv("PRICING_REFRESH_HOURS", "1000")
    monkeypatch.setenv("PRICING_SOURCE_URL", "https://prices.test/grid.json")
    up = Upstream()
    with TestClient(make(up)) as c:
        assert up.litellm_calls == 1  # import du démarrage
        assert c.get("/v1/admin/pricing/unknown", headers=ACCOUNT).status_code == 403
        empty = c.get("/v1/admin/pricing/unknown", headers=MASTER).json()
        assert empty["data"] == [] and empty["builtin_date"] == BUILTIN_PRICES_DATE and empty["auto_refresh"]
        # 1er usage d'un modèle sans tarif : compté à 0 $, signalé, et un import est tenté aussitôt
        up.litellm = {
            "openrouter/acme/brainy-1:beta": {
                "input_cost_per_token": 1e-7,
                "output_cost_per_token": 2e-7,
                "mode": "chat",
            }
        }
        meta = ask(c, "openrouter/acme/brainy-1:beta").json()["x_gateway"]
        assert meta["pricing"] == "unknown" and meta["cost_usd"] == 0
        for _ in range(50):
            if up.litellm_calls == 2:
                break
            time.sleep(0.02)
        assert up.litellm_calls == 2
        ask(c, "openrouter/acme/brainy-1:beta")
        assert up.litellm_calls == 2  # pas un import par requête
        r = c.get("/v1/admin/pricing/unknown", headers=MASTER).json()
        assert r["data"] == []  # importé : plus inconnu
        assert ask(c, "openrouter/acme/brainy-1:beta").json()["x_gateway"]["cost_usd"] > 0


def test_unknown_list_counts_real_usage(monkeypatch):
    up = Upstream()
    with TestClient(make(up)) as c:
        for _ in range(3):
            assert ask(c, "openrouter/acme/brainy-1:beta").status_code == 200
        ask(c, "openrouter/acme/brainy-1")  # tarifé : non listé
        data = c.get("/v1/admin/pricing/unknown", headers=MASTER).json()["data"]
        assert [(d["ref"], d["requests"], d["tokens"]) for d in data] == [("openrouter/acme/brainy-1:beta", 3, 6000)]
        assert data[0]["last_used"] > time.time() - 60
        assert c.get("/v1/admin/pricing/unknown?days=0", headers=MASTER).status_code == 400
        assert (
            c.put(
                "/v1/admin/pricing/openrouter/acme/brainy-1:beta",
                headers=MASTER,
                json={"input_per_mtok_usd": 0.1, "output_per_mtok_usd": 0.1},
            ).status_code
            == 200
        )
        assert c.get("/v1/admin/pricing/unknown", headers=MASTER).json()["data"] == []
