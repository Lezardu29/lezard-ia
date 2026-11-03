import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.preferences import PreferenceError, validate_chat
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer master"}


def test_validate_chat():
    assert validate_chat({}) == {
        "mode": "balanced",
        "effort": "auto",
        "orchestration": "auto",
        "badge": {"display": "model", "provider_colors": True, "colors": {}},
    }
    v = validate_chat(
        {
            "mode": "economy",
            "effort": "high",
            "orchestration": "single",
            "badge": {"display": "both", "provider_colors": False, "colors": {"openai": "#0F0"}},
        }
    )
    assert v["badge"] == {"display": "both", "provider_colors": False, "colors": {"openai": "#00ff00"}}
    for bad in (
        {"mode": "turbo"},
        {"effort": "max"},
        {"orchestration": "parfois"},
        {"badge": {"display": "logo"}},
        {"badge": {"colors": {"<b>": "#fff"}}},
        {"badge": {"colors": {"openai": "rouge"}}},
        {"x": 1},
    ):
        with pytest.raises(PreferenceError):
            validate_chat(bad)


@pytest.fixture
def client():
    def upstream(r):
        if r.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        plans=PlanResolver({}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        yield c


def test_chat_preferences_roundtrip(client):
    r = client.put("/v1/preferences/chat", headers=MASTER, json={"mode": "speed", "effort": "low"}).json()
    assert r["chat"]["mode"] == "speed" and r["updated_at"]
    assert client.get("/v1/preferences/chat", headers=MASTER).json()["chat"]["effort"] == "low"
    assert client.get("/v1/preferences/theme", headers=MASTER).json()["theme"] is None  # indépendants
    assert client.get("/v1/preferences/inconnu", headers=MASTER).status_code == 400
    assert client.put("/v1/preferences/chat", headers=MASTER, json={"mode": "x"}).status_code == 400


def test_me_exposes_routing_rights_for_ui(client):
    k = client.post(
        "/v1/keys",
        headers=MASTER,
        json={
            "name": "eco",
            "permissions": {"modes": ["economy"], "max_effort": "medium", "orchestration": False, "hosting": "local"},
        },
    ).json()
    me = client.get("/v1/me", headers={"Authorization": f"Bearer {k['key']}"}).json()
    assert me["routing_rights"] == {
        "modes": ["economy"],
        "max_effort": "medium",
        "orchestration": False,
        "hosting": "local",
    }
    k2 = client.post("/v1/keys", headers=MASTER, json={"name": "libre"}).json()
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {k2['key']}"}).json()["routing_rights"] == {}


def test_ui_request_shape_is_accepted(client):
    # forme exacte envoyée par la barre de réglages : model auto/<mode>, effort, orchestration
    r = client.post(
        "/v1/chat/completions",
        headers=MASTER,
        json={
            "model": "auto/economy",
            "effort": "auto",
            "orchestration": "auto",
            "messages": [{"role": "user", "content": "Bonjour"}],
        },
    ).json()
    g = r["x_gateway"]
    assert g["routing"]["mode"] == "economy" and g["provider"] == "ollama" and g["hosting"] == "local"


def test_stream_final_chunk_has_everything_the_metrics_line_needs(client):
    import json as _json

    r = client.post(
        "/v1/chat/completions",
        headers=MASTER,
        json={"model": "auto/economy", "stream": True, "messages": [{"role": "user", "content": "Bonjour"}]},
    )
    final = [_json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: {") and "x_gateway" in l][-1]
    g = final["x_gateway"]
    # ce faux provider n'envoie pas d'usage en streaming : la gateway l'estime et le signale
    assert g["usage_estimated"] is True and final["usage"]["prompt_tokens"] > 0
    for field in ("provider", "model", "hosting", "cost_eur", "cost_usd", "latency_ms", "plan", "effort", "routing"):
        assert field in g, field
    assert g["hosting"] == "local" and g["cost_eur"] == 0
    assert client.get("/static/metrics.js").status_code == 200


def test_estimated_usage_is_billed_and_counted():
    """Sans usage du provider en streaming, coût et limites doivent quand même être comptés."""
    import json as _json

    def upstream(r):
        if r.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})
        chunk = {"choices": [{"delta": {"content": "x" * 400}}]}  # ~100 tokens, sans usage
        return httpx.Response(200, text=f"data: {_json.dumps(chunk)}\n\ndata: [DONE]\n\n")

    app = create_app(
        [ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k")],
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        plans=PlanResolver({}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        k = c.post("/v1/keys", headers=MASTER, json={"name": "k", "limits": {"tpm": 100000}}).json()
        h = {"Authorization": f"Bearer {k['key']}"}
        r = c.post(
            "/v1/chat/completions",
            headers=h,
            json={"model": "openai/gpt-4o", "stream": True, "messages": [{"role": "user", "content": "Bonjour"}]},
        )
        final = [_json.loads(l[6:]) for l in r.text.splitlines() if "x_gateway" in l][-1]
        assert final["usage"]["completion_tokens"] == 100 and final["x_gateway"]["cost_usd"] > 0
        usage = c.get(f"/v1/keys/{k['id']}/usage", headers=MASTER).json()
        assert usage["tpm"]["used"] == final["usage"]["total_tokens"]
        assert usage["spend"]["day"]["spent_usd"] == pytest.approx(final["x_gateway"]["cost_usd"], abs=1e-6)


def test_settings_assets_served(client):
    html = client.get("/app?frame=1").text
    assert 'id="toolbar"' in html and "/static/settings.css" in html
    for a in ("settings-bar.js", "settings.css"):
        assert client.get(f"/static/{a}").status_code == 200
