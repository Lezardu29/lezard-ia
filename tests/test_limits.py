from datetime import datetime, UTC

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.counters import SlidingWindow
from gateway.limits import LimitError, period_bounds, validate_limits
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.registry import usd_eur_rate
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer master"}
T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC).timestamp()  # dimanche


# ---------- unités ----------


def test_validate_limits():
    v = validate_limits(
        {
            "rpm": 10,
            "budget": [{"amount": 5, "period": "semaine"}, {"amount": 20, "currency": "usd", "period": "mensuel"}],
        }
    )
    assert v == {
        "rpm": 10,
        "budget": [
            {"amount": 5.0, "currency": "EUR", "period": "week"},
            {"amount": 20.0, "currency": "USD", "period": "month"},
        ],
    }
    for bad in (
        {"rpm": 0},
        {"tpm": "beaucoup"},
        {"rps": 1},
        {"budget": {"amount": -1}},
        {"budget": {"amount": 1, "currency": "BTC"}},
        {"budget": {"amount": 1, "period": "an"}},
        {"budget": [{"amount": 1, "period": "day"}, {"amount": 2, "period": "jour"}]},
    ):
        with pytest.raises(LimitError):
            validate_limits(bad)


def test_period_bounds():
    pid, reset = period_bounds("day", T0)
    assert pid == "2026-10-18" and reset == datetime(2026, 10, 19, tzinfo=UTC).timestamp()
    pid, reset = period_bounds("week", T0)
    assert pid == "2026-W42" and reset == datetime(2026, 10, 19, tzinfo=UTC).timestamp()  # lundi
    pid, reset = period_bounds("month", T0)
    assert pid == "2026-10" and reset == datetime(2026, 11, 1, tzinfo=UTC).timestamp()
    assert (
        period_bounds("month", datetime(2026, 12, 31, 23, tzinfo=UTC).timestamp())[1]
        == datetime(2027, 1, 1, tzinfo=UTC).timestamp()
    )


def test_sliding_window():
    w = SlidingWindow(60)
    w.add(30, 100.0)
    w.add(50, 130.0)
    assert w.used(130.0) == 80 and w.used(161.0) == 50 and w.used(191.0) == 0
    w.add(30, 200.0)
    w.add(50, 230.0)
    assert w.retry_after(240.0, 40, 100) == pytest.approx(20)  # attendre l'expiration du 1er seau


# ---------- API ----------


class Upstream:
    def __init__(self):
        self.calls = 0
        self.usage = {"prompt_tokens": 1000, "completion_tokens": 1000}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path.endswith("/models"):
            ids = {"ollama": ["llama3.1:8b"], "openai": ["gpt-4o", "gpt-4o-mini"]}[host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        self.calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": dict(self.usage),
            },
        )


@pytest.fixture
def api():
    up, now, store = Upstream(), [T0], DocumentStore(":memory:")

    def build():
        return TestClient(
            create_app(
                [
                    ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
                    ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k"),
                ],
                httpx.AsyncClient(transport=httpx.MockTransport(up)),
                plans=PlanResolver({}, "free", master_key="master"),
                store=store,
                clock=lambda: now[0],
            )
        )

    with build() as c:

        def key(limits):
            r = c.post("/v1/keys", headers=MASTER, json={"name": "k", "limits": limits})
            assert r.status_code == 201, r.text
            return r.json()["id"], {"Authorization": f"Bearer {r.json()['key']}"}

        yield c, up, now, key, build


def chat(model="openai/gpt-4o", **kw):
    return {"model": model, "messages": [{"role": "user", "content": "Bonjour"}], **kw}


def test_rpm(api):
    c, up, now, key, _ = api
    _, h = key({"rpm": 2})
    r = c.post("/v1/chat/completions", headers=h, json=chat())
    assert r.headers["x-ratelimit-limit-requests"] == "2" and r.headers["x-ratelimit-remaining-requests"] == "1"
    assert c.post("/v1/chat/completions", headers=h, json=chat()).status_code == 200
    r = c.post("/v1/chat/completions", headers=h, json=chat())
    assert r.status_code == 429 and 1 <= int(r.headers["Retry-After"]) <= 60
    assert r.json()["error"]["provider"] == "limits:rpm" and up.calls == 2
    now[0] += 61
    assert c.post("/v1/chat/completions", headers=h, json=chat()).status_code == 200


def test_tpm_and_tpd(api):
    c, up, now, key, _ = api
    _, h = key({"tpm": 5000, "tpd": 9000})
    for _ in range(2):  # 2 × 2000 tokens
        assert c.post("/v1/chat/completions", headers=h, json=chat()).status_code == 200
    up.usage = {"prompt_tokens": 500, "completion_tokens": 600}
    assert c.post("/v1/chat/completions", headers=h, json=chat()).status_code == 200  # 5100 utilisés
    r = c.post("/v1/chat/completions", headers=h, json=chat())
    assert r.status_code == 429 and r.json()["error"]["provider"] == "limits:tpm"
    now[0] += 61
    up.usage = {"prompt_tokens": 2000, "completion_tokens": 2000}
    assert c.post("/v1/chat/completions", headers=h, json=chat()).status_code == 200  # 9100 sur 24 h
    now[0] += 61
    r = c.post("/v1/chat/completions", headers=h, json=chat())
    assert r.status_code == 429 and r.json()["error"]["provider"] == "limits:tpd"
    assert int(r.headers["Retry-After"]) > 80000  # ~24 h
    # une grosse requête est refusée d'emblée si elle dépasserait la limite
    _, h2 = key({"tpm": 1000})
    big = chat(messages=[{"role": "user", "content": "x " * 5000}])
    calls = up.calls
    assert c.post("/v1/chat/completions", headers=h2, json=big).status_code == 429 and up.calls == calls


def test_budget_persisted_and_reset(api):
    c, up, now, key, build = api
    kid, h = key({"budget": {"amount": 0.02, "currency": "EUR", "period": "month"}})
    # gpt-4o : 1000 × 2.5 $ + 1000 × 10 $ par M = 0.0125 $ par requête
    assert c.post("/v1/chat/completions", headers=h, json=chat()).status_code == 200
    r = c.post("/v1/chat/completions", headers=h, json=chat())
    assert r.status_code == 200 and "x-budget-remaining-month" in r.headers
    r = c.post("/v1/chat/completions", headers=h, json=chat())
    assert r.status_code == 429 and r.json()["error"]["provider"] == "limits:budget_month"
    assert "2026-11-01" in r.json()["error"]["message"]
    usage = c.get(f"/v1/keys/{kid}/usage", headers=MASTER).json()
    assert usage["spend"]["month"]["spent_usd"] == pytest.approx(0.025)
    assert usage["budgets"][0]["remaining"] == 0
    # le modèle local (gratuit) reste utilisable
    assert c.post("/v1/chat/completions", headers=h, json=chat("ollama/llama3.1:8b")).status_code == 200
    # persistance : une nouvelle instance de la gateway connaît la dépense
    with build() as c2:
        assert c2.post("/v1/chat/completions", headers=h, json=chat()).status_code == 429
        now[0] = datetime(2026, 11, 1, 0, 1, tzinfo=UTC).timestamp()  # nouveau mois
        assert c2.post("/v1/chat/completions", headers=h, json=chat()).status_code == 200


def test_budget_blocks_expensive_request_upfront(api):
    c, up, _, key, _ = api
    _, h = key({"budget": {"amount": 0.01, "currency": "USD", "period": "day"}})
    r = c.post("/v1/chat/completions", headers=h, json=chat(max_tokens=50_000))  # ~0.5 $ estimés
    assert r.status_code == 429 and up.calls == 0
    assert c.post("/v1/chat/completions", headers=h, json=chat("openai/gpt-4o-mini")).status_code == 200


def test_unpriced_cloud_model_cannot_bypass_budget(api):
    """8.3 : un modèle cloud absent de la grille coûterait 0 $ et contournerait le budget."""
    c, up, _, key, _ = api
    kid, h = key({"budget": {"amount": 0.01, "currency": "USD", "period": "day"}})
    r = c.post("/v1/chat/completions", headers=h, json=chat("openai/gpt-unlisted"))
    assert r.status_code == 403 and "sans tarif connu" in r.json()["error"]["message"] and up.calls == 0
    # pas non plus en modèle de secours
    r = c.post("/v1/chat/completions", headers=h, json=chat("openai/gpt-4o-mini", fallbacks=["openai/gpt-unlisted"]))
    assert r.status_code == 403
    # clé sans budget : autorisé, coût signalé comme inconnu
    _, free = key({"rpm": 100})
    r = c.post("/v1/chat/completions", headers=free, json=chat("openai/gpt-unlisted"))
    assert r.status_code == 200 and r.json()["x_gateway"]["pricing"] == "unknown"
    # une fois le prix renseigné par l'admin, la dépense est comptée
    assert (
        c.put(
            "/v1/admin/pricing/openai/gpt-unlisted",
            headers=MASTER,
            json={"input_per_mtok_usd": 2.0, "output_per_mtok_usd": 8.0},
        ).status_code
        == 200
    )
    assert c.post("/v1/chat/completions", headers=h, json=chat("openai/gpt-unlisted")).status_code == 200
    assert c.get(f"/v1/keys/{kid}/usage", headers=MASTER).json()["spend"]["day"]["spent_usd"] == pytest.approx(0.01)


def test_budget_routing_skips_unpriced_models(api):
    c, _, _, _, _ = api
    # gpt-4o-mini passe à 0 $ (prix inconnu) : en mode économique il serait choisi d'office
    c.put(
        "/v1/admin/pricing/openai/gpt-4o-mini", headers=MASTER, json={"input_per_mtok_usd": 0, "output_per_mtok_usd": 0}
    )
    r = c.post(
        "/v1/keys",
        headers=MASTER,
        json={
            "name": "cloud",
            "permissions": {"hosting": "cloud"},
            "limits": {"budget": {"amount": 5, "period": "month"}},
        },
    )
    h = {"Authorization": f"Bearer {r.json()['key']}"}
    r = c.post("/v1/chat/completions", headers=h, json=chat("auto/economy"))
    assert r.status_code == 200 and r.json()["x_gateway"]["model"] == "gpt-4o"
    assert "openai/gpt-4o-mini" not in r.json()["x_gateway"]["routing"]["alternatives"]
    # sans budget, le routage économique prend bien le modèle « gratuit »
    r = c.post("/v1/keys", headers=MASTER, json={"name": "cloud2", "permissions": {"hosting": "cloud"}})
    h2 = {"Authorization": f"Bearer {r.json()['key']}"}
    assert (
        c.post("/v1/chat/completions", headers=h2, json=chat("auto/economy")).json()["x_gateway"]["model"]
        == "gpt-4o-mini"
    )


def test_weekly_budget_in_usd_and_me_endpoint(api):
    c, _, now, key, _ = api
    kid, h = key({"budget": [{"amount": 1, "currency": "USD", "period": "week"}, {"amount": 0.5, "period": "day"}]})
    c.post("/v1/chat/completions", headers=h, json=chat())
    me = c.get("/v1/me", headers=h).json()
    assert me["limits"]["budget"][0]["period"] == "week"
    week = next(b for b in me["usage"]["budgets"] if b["period"] == "week")
    day = next(b for b in me["usage"]["budgets"] if b["period"] == "day")
    assert week["spent"] == pytest.approx(0.0125) and week["remaining"] == pytest.approx(0.9875)
    assert day["spent"] == pytest.approx(0.0125 * usd_eur_rate()) and day["currency"] == "EUR"


def test_cache_hits_are_free_and_limits_crud(api):
    c, up, _, key, _ = api
    kid, h = key({"tpm": 100000, "budget": {"amount": 1, "period": "month"}})
    for _ in range(3):
        c.post("/v1/chat/completions", headers=h, json=chat(cache=True))
    assert up.calls == 1
    st = c.get(f"/v1/keys/{kid}/usage", headers=MASTER).json()
    assert st["tpm"]["used"] == 2000 and st["spend"]["month"]["spent_usd"] == pytest.approx(0.0125)
    assert c.post("/v1/keys", headers=MASTER, json={"name": "x", "limits": {"rpm": -1}}).status_code == 400
    upd = c.patch(f"/v1/keys/{kid}", headers=MASTER, json={"limits": {"rpm": 5}}).json()
    assert upd["limits"] == {"rpm": 5}
    # comptes et anonymes ne sont pas limités par clé
    for _ in range(3):
        assert c.post("/v1/chat/completions", headers=MASTER, json=chat()).status_code == 200
