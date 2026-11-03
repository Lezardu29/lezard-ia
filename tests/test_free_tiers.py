"""Offres gratuites officielles (13.8) : 0 €, juste après le local en mode Économique, quotas préremplis,
bascule quand le quota est atteint, catégorie « cloud gratuit » des FinOps."""

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import registry as reg
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.registry import ModelRegistry
from gateway.store import DocumentStore

MASTER, ACCOUNT = {"Authorization": "Bearer master"}, {"Authorization": "Bearer compte"}
USAGE = {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}


@pytest.fixture(autouse=True)
def fx():
    reg.set_live_rate(None)
    yield
    reg.set_live_rate(None)


class Upstream:
    def __init__(self):
        self.calls, self.fail = [], {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path.endswith("/models"):
            ids = {
                "gemini": ["models/gemini-3.5-flash-lite", "models/gemini-3.1-pro-preview"],
                "openai": ["gpt-4o-mini"],
                "ollama": ["llama3.1:8b"],
                "openrouter": ["meta-llama/llama-3.3-70b-instruct:free", "openai/gpt-4o-mini"],
            }[host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        model = json.loads(request.content)["model"]
        self.calls.append((host, model))
        if host in self.fail:
            return httpx.Response(self.fail[host], json={"error": {"message": "quota épuisé"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": USAGE,
            },
        )


def make(up, local=False, openrouter=False, store=None):
    configs = [
        ProviderConfig("gemini", "openai", "http://gemini/v1beta/openai", "cloud", api_key="g-key"),
        ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="o-key"),
    ]
    if local:
        configs.append(ProviderConfig("ollama", "openai", "http://ollama:11434/v1", "local"))
    if openrouter:
        configs.append(ProviderConfig("openrouter", "openai", "http://openrouter/api/v1", "cloud", api_key="r-key"))
    return create_app(
        configs,
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({"compte": "free"}, "free", master_key="master"),
        store=store or DocumentStore(":memory:"),
    )


def chat(c, headers=ACCOUNT, model="auto/economy"):
    return c.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": model, "messages": [{"role": "user", "content": "Bonjour"}]},
    )


def enable(c, provider="gemini", on=True):
    r = c.put(f"/v1/admin/free-tiers/{provider}", headers=MASTER, json={"enabled": on})
    assert r.status_code == 200, r.text
    return r.json()


def test_free_tier_listed_with_training_terms_admin_only():
    with TestClient(make(Upstream())) as c:
        assert c.get("/v1/admin/free-tiers", headers=ACCOUNT).status_code == 403
        data = {t["provider"]: t for t in c.get("/v1/admin/free-tiers", headers=MASTER).json()["data"]}
        assert set(data) == {"gemini", "groq", "mistral", "openrouter"}
        assert data["mistral"]["training"] == "yes" and data["groq"]["training"] == "no"
        assert data["gemini"]["training"] == "region" and data["openrouter"]["training"] == "depends"
        assert data["gemini"]["has_key"] and not data["gemini"]["enabled"] and data["gemini"]["checked"]


def test_without_offer_gemini_is_billed_with_offer_free():
    up = Upstream()
    with TestClient(make(up)) as c:
        paid = chat(c, model="gemini/gemini-3.5-flash-lite").json()["x_gateway"]
        assert paid["cost_usd"] > 0 and "free_tier" not in paid
        t = enable(c)
        assert t["enabled"] and t["provider_rules"] == {"rpm": 10, "rpd": 250, "tpm": 250_000}
        assert t["free_models"] == ["gemini-3.5-flash-lite"]  # Pro : offre payante
        free = chat(c, model="gemini/gemini-3.5-flash-lite").json()["x_gateway"]
        assert free["cost_usd"] == 0 and free["free_tier"] is True and free["hosting"] == "cloud"
        assert ("gemini", "gemini-3.5-flash-lite") in up.calls  # préfixe « models/ » retiré
        pro = chat(c, model="gemini/gemini-3.1-pro-preview").json()["x_gateway"]
        assert pro["cost_usd"] > 0


def test_economy_mode_local_then_free_then_paid():
    up = Upstream()
    with TestClient(make(up, local=True)) as c:
        enable(c)
        r = chat(c).json()["x_gateway"]
        assert r["provider"] == "ollama"
        alts = r["routing"]["alternatives"]
        assert alts.index("gemini/gemini-3.5-flash-lite") < alts.index("openai/gpt-4o-mini")
    with TestClient(make(Upstream())) as c:  # sans local : le gratuit d'abord
        enable(c)
        r = chat(c).json()["x_gateway"]
        assert r["provider"] == "gemini" and "cloud gratuit" in r["routing"]["reason"] and r["cost_usd"] == 0


def test_daily_quota_reached_switches_to_another_ai():
    up = Upstream()
    with TestClient(make(up)) as c:
        enable(c)
        c.put("/v1/admin/providers/gemini/rules", headers=MASTER, json={"rpm": 10, "rpd": 2, "tpm": 250_000})
        assert [chat(c).json()["x_gateway"]["provider"] for _ in range(3)] == ["gemini", "gemini", "openai"]
        rules = c.get("/v1/admin/free-tiers", headers=MASTER).json()["data"][0]
        assert rules["usage"]["rpd"] == {"limit": 2, "used": 2} and "requêtes/jour" in rules["usage"]["saturated"]


def test_provider_quota_error_falls_back():
    up = Upstream()
    up.fail["gemini"] = 429  # quota réel du compte épuisé
    with TestClient(make(up)) as c:
        enable(c)
        r = chat(c).json()["x_gateway"]
        assert r["provider"] == "openai" and r["fallback"]["attempts"][0]["status"] == 429


def test_disable_removes_prefilled_rules_but_keeps_edited_ones():
    with TestClient(make(Upstream())) as c:
        enable(c)
        c.put(
            "/v1/admin/providers/gemini/rules",
            headers=MASTER,
            json={"rpm": 5, "rpd": 250, "tpm": 250_000, "plans": ["pro"]},
        )
        t = enable(c, on=False)
        assert not t["enabled"] and t["provider_rules"] == {"rpm": 5, "plans": ["pro"]}
        assert c.put("/v1/admin/providers/gemini/rules", headers=MASTER, json={"rpd": 0}).status_code == 400


def test_openrouter_free_models_free_without_offer_and_offer_restricts():
    up = Upstream()
    with TestClient(make(up, openrouter=True)) as c:
        r = chat(c, model="openrouter/meta-llama/llama-3.3-70b-instruct:free").json()["x_gateway"]
        assert r["cost_usd"] == 0 and r["free_tier"] is True
        enable(c, "openrouter")
        r = chat(c, model="openrouter/openai/gpt-4o-mini")
        assert r.status_code == 403  # offre : modèles « :free » seulement


def test_budget_key_may_use_free_models_at_zero_cost():
    with TestClient(make(Upstream())) as c:
        enable(c)
        k = c.post(
            "/v1/keys",
            headers=ACCOUNT,
            json={"name": "budget", "limits": {"budget": {"amount": 1, "currency": "EUR", "period": "month"}}},
        ).json()
        r = chat(c, {"Authorization": f"Bearer {k['key']}"})
        assert r.status_code == 200 and r.json()["x_gateway"]["provider"] == "gemini"
        usage = c.get("/v1/keys", headers=ACCOUNT, params={"with_usage": "true"}).json()["data"][0]["usage"]
        assert usage["budgets"][0]["spent"] == 0


def test_finops_free_cloud_category():
    store = DocumentStore(":memory:")
    with TestClient(make(Upstream(), store=store)) as c:
        enable(c)
        chat(c)  # gemini gratuit
        chat(c, model="openai/gpt-4o-mini")  # cloud payant
        for _ in range(50):  # la capture des métriques est asynchrone
            s = c.get("/v1/metrics/summary", headers=ACCOUNT).json()
            if s["requests"] >= 2:
                break
            time.sleep(0.05)
        b = s["savings"]["breakdown_eur"]
        assert b["free_cloud"] > 0 and set(b) == {
            "local_models",
            "free_cloud",
            "smart_routing",
            "prompt_caching",
            "response_cache",
        }
        split = {x["provider"] for x in c.get("/v1/metrics/charts", headers=ACCOUNT).json()["split"]}
        assert split == {"free", "openai"}


def test_openrouter_free_model_never_reported_as_unpriced():
    with TestClient(make(Upstream(), openrouter=True)) as c:
        for _ in range(2):
            assert chat(c, model="openrouter/meta-llama/llama-3.3-70b-instruct:free").status_code == 200
        assert c.get("/v1/admin/pricing/unknown", headers=MASTER).json()["data"] == []


def test_registry_free_flag():
    r = ModelRegistry()
    assert not r.get("gemini", "gemini-3.5-flash-lite").free
    r.free_enabled.add("gemini")
    info = r.get("gemini", "gemini-3.5-flash-lite")
    assert info.free and info.priced and info.cost_usd(10**6, 10**6) == 0 and info.context_window == 1_048_576
    assert not r.get("gemini", "gemini-3.1-pro-preview").free
    guess = r.get("openrouter", "qwen/qwen3-235b-a22b:free")
    assert guess.free and guess.hosting == "cloud" and guess.quality >= 0.7
