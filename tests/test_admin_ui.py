import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import registry as reg
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer master"}


@pytest.fixture(autouse=True)
def reset_fx():
    reg.set_live_rate(None)
    yield
    reg.set_live_rate(None)


def upstream(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if request.url.path.endswith("/models"):
        ids = {"ollama": ["llama3.1:8b"], "openai": ["gpt-4o", "gpt-4o-mini"]}[host]
        return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
    if json.loads(request.content).get("stream"):
        chunks = [
            {"choices": [{"delta": {"content": "ok"}}]},
            {"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}},
        ]
        return httpx.Response(200, text="".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n")
    return httpx.Response(
        200,
        json={
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
        },
    )


@pytest.fixture
def client():
    app = create_app(
        [
            ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
            ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        plans=PlanResolver({"compte": "free"}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        yield c


def chat(model, **kw):
    return {"model": model, "messages": [{"role": "user", "content": "Bonjour"}], **kw}


def test_summary_kpis(client):
    assert client.get("/v1/metrics/summary", headers=MASTER).json()["requests"] == 0
    client.post("/v1/chat/completions", headers=MASTER, json=chat("openai/gpt-4o"))
    client.post("/v1/chat/completions", headers=MASTER, json=chat("ollama/llama3.1:8b", stream=True))
    client.post("/v1/chat/completions", headers=MASTER, json=chat("fournisseur-x/modele"))  # erreur 404
    s = client.get("/v1/metrics/summary", headers=MASTER).json()
    cost = (1000 * 2.5 + 200 * 10) / 1e6
    assert s["requests"] == 3 and s["errors"] == 1 and s["error_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert s["spend_eur"] == pytest.approx(cost * 0.92) and s["provider_cost_eur"] == pytest.approx(cost * 0.92)
    assert s["tokens"]["total"] == 2400 and s["local_requests"] == 1
    assert s["avg_latency_ms"] is not None and s["avg_ttft_ms"] is not None
    assert s["savings"]["baseline_model"] == "openai/gpt-4o" and s["savings"]["saved_eur"] > 0
    assert set(s["savings"]["breakdown_eur"]) == {
        "local_models",
        "free_cloud",
        "smart_routing",
        "prompt_caching",
        "response_cache",
    }
    import time

    assert client.get("/v1/metrics/summary", headers=MASTER, params={"since": time.time() + 10}).json()["requests"] == 0


def test_keys_with_usage_for_gauges(client):
    k = client.post(
        "/v1/keys",
        headers=MASTER,
        json={"name": "web", "limits": {"tpd": 10_000, "budget": {"amount": 1, "currency": "EUR", "period": "month"}}},
    ).json()
    client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {k['key']}"}, json=chat("openai/gpt-4o"))
    rows = client.get("/v1/keys", headers=MASTER, params={"with_usage": "true"}).json()["data"]
    u = rows[0]["usage"]
    assert u["tpd"] == {"limit": 10_000, "used": 1200, "remaining": 8800}
    assert u["budgets"][0]["spent"] > 0 and u["budgets"][0]["remaining"] < 1
    assert "usage" not in client.get("/v1/keys", headers=MASTER).json()["data"][0]  # opt-in


def test_ui_payload_shapes_accepted(client):
    """Formes exactes produites par la fenêtre de création/édition (key-dialog.js)."""
    body = {
        "name": "Bot",
        "description": None,
        "project": None,
        "plan": "free",
        "permissions": {
            "models": {"allow": ["ollama/*"]},
            "hosting": "local",
            "ip_allow": ["10.0.0.0/8"],
            "modes": ["economy"],
            "max_effort": "medium",
            "orchestration": False,
        },
        "limits": {"rpm": 30, "budget": [{"period": "week", "amount": 5, "currency": "EUR"}]},
        "metadata": {"equipe": "mobile"},
        "expires_in": "30d",
    }
    r = client.post("/v1/keys", headers=MASTER, json=body)
    assert r.status_code == 201, r.text
    k = r.json()
    assert k["permissions"]["hosting"] == "local" and k["limits"]["budget"][0]["period"] == "week"
    # édition : tout décocher = plus aucune restriction ; expiration « Jamais »
    r = client.patch(
        f"/v1/keys/{k['id']}",
        headers=MASTER,
        json={
            "name": "Bot v2",
            "description": "maj",
            "project": None,
            "plan": "free",
            "permissions": {},
            "limits": {},
            "metadata": {},
            "expires_at": None,
        },
    )
    assert r.status_code == 200 and r.json()["permissions"] == {} and r.json()["expires_at"] is None
    # date précise envoyée en ISO par le navigateur
    r = client.patch(
        f"/v1/keys/{k['id']}", headers=MASTER, json={"name": "Bot v2", "expires_at": "2099-01-01T10:00:00.000Z"}
    )
    assert r.status_code == 200 and r.json()["expires_at"] > 4e9


def test_admin_page_and_assets(client):
    r = client.get("/admin?frame=1")
    assert r.status_code == 200 and "content-security-policy" in r.headers
    html = r.text
    for marker in ('id="kpis"', 'id="key-dialog"', 'id="secret-dialog"', 'data-opt="hosting"'):
        assert marker in html
    assert html.index('src="/static/theme.js"') < html.index('type="module"')
    for a in ("admin.js", "admin.css", "key-dialog.js", "row-menu.js"):
        assert client.get(f"/static/{a}").status_code == 200
    assert 'href="/admin"' in client.get("/app?frame=1").text


def test_mobile_interface_on_every_page(client):
    """Interface mobile (13.5) : détection par les capacités de l'écran, tiroir.
    L'absence de défilement horizontal à 360 px se vérifie dans un navigateur (non lancé par la suite de tests)."""
    for page in ("/", "/app", "/admin", "/models"):
        html = client.get(page if page == "/" else page + "?frame=1").text
        assert 'src="/static/device.js"' in html and 'href="/static/mobile.css"' in html, page
        assert html.index("device.js") < html.index('type="module"'), page  # avant le rendu : pas de saut
        if page != "/":
            assert "data-drawer" in html, page
    device = client.get("/static/device.js").text
    assert "(pointer: coarse)" in device and "matchMedia" in device
    assert "known && known.matches ? coarse.matches : uaHint" in device  # User-Agent : simple indice
    mobile = client.get("/static/mobile.js").text
    assert "visualViewport" in mobile and "data-drawer" in mobile
    css = client.get("/static/mobile.css").text
    assert "min-height: 44px" in css and ':root[data-input="touch"]' in css


def test_hardware_widget_only_on_admin_pages(client):
    """Widget Matériel & IA locales (13.3) : administration seulement, jamais dans le chat (14.3)."""
    for page in ("/admin", "/models"):
        assert 'id="hw-widget"' in client.get(page + "?frame=1").text
    assert "hw-widget" not in client.get("/app?frame=1").text
    widget = client.get("/static/hardware-widget.js")
    assert widget.status_code == 200 and client.get("/static/ollama-pull.js").status_code == 200
    assert "/v1/admin/hardware" in widget.text and "document.hidden" in widget.text  # en pause onglet caché
    for script in ("app.js", "chat.js"):
        assert "hardware-widget" not in client.get(f"/static/{script}").text
    assert "if (me.is_admin) initHardwareWidget" in client.get("/static/admin.js").text


def test_script_ids_exist_in_their_page():
    """Chaque $("id") d'un script existe dans sa page : sinon le script s'arrête au premier élément manquant
    (le tableau de bord restait vide)."""
    import re
    from pathlib import Path

    web = Path(__file__).resolve().parent.parent / "gateway" / "web"
    for script, page in (("admin.js", "admin.html"), ("models.js", "models.html"), ("app.js", "app.html")):
        src = (web / "static" / script).read_text(encoding="utf-8")
        html = (web / page).read_text(encoding="utf-8")
        ids = set(re.findall(r'\$\("([^"]+)"\)', src))
        missing = sorted(i for i in ids if f'id="{i}"' not in html)
        assert not missing, (script, missing)
