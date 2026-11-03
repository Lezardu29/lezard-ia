import csv
import hashlib
import hmac
import io
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import registry as reg
from gateway.alerts import AlertError, check_webhook_url, validate_rule, webhook_payload
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore
from datetime import UTC

MASTER = {"Authorization": "Bearer master"}
COST = (1000 * 2.5 + 200 * 10) / 1e6  # gpt-4o : 0,0045 $ par requête


@pytest.fixture(autouse=True)
def reset_fx():
    reg.set_live_rate(None)
    yield
    reg.set_live_rate(None)


class Upstream:
    def __init__(self):
        self.hooks: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("hooks."):
            self.hooks.append(request)
            return httpx.Response(200 if "ok" in request.url.path else 500)
        if request.url.path.endswith("/models"):
            ids = {"ollama": ["llama3.1:8b"], "openai": ["gpt-4o"]}[request.url.host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
            },
        )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ALERT_ALLOW_PRIVATE_WEBHOOKS", "true")  # hôtes de test non résolvables
    monkeypatch.setenv("METRICS_FLUSH_SECONDS", "0.02")
    up = Upstream()
    app = create_app(
        [
            ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
            ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({"compte": "free"}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        yield c, up


def chat(model):
    return {"model": model, "messages": [{"role": "user", "content": "Bonjour"}]}


def wait_for(fn, timeout=3.0):
    t = time.time()
    while time.time() - t < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.03)
    return fn()


# ---------- règles & sécurité ----------


def test_validate_rule():
    r = validate_rule({"name": " Mensuel ", "amount": 50, "thresholds": [100, 50, 80, 80]})
    assert r == {"name": "Mensuel", "period": "month", "amount": 50.0, "currency": "EUR", "thresholds": [50, 80, 100]}
    for bad in (
        {"amount": 1},
        {"name": "x", "amount": 0},
        {"name": "x", "amount": 5, "period": "an"},
        {"name": "x", "amount": 5, "thresholds": []},
        {"name": "x", "amount": 5, "thresholds": [500]},
        {"name": "x", "amount": 5, "currency": "BTC"},
        {"name": "x", "amount": 5, "inconnu": 1},
    ):
        with pytest.raises(AlertError):
            validate_rule(bad)


def test_ssrf_guard(monkeypatch):
    monkeypatch.delenv("ALERT_ALLOW_PRIVATE_WEBHOOKS", raising=False)
    for url in (
        "http://example.com/hook",
        "https://127.0.0.1/x",
        "https://localhost/x",
        "https://10.0.0.5/x",
        "https://169.254.169.254/latest/meta-data",
        "ftp://example.com",
        "pas une url",
    ):
        with pytest.raises(AlertError):
            check_webhook_url(url)
    monkeypatch.setenv("ALERT_ALLOW_PRIVATE_WEBHOOKS", "true")
    assert check_webhook_url("http://192.168.1.10/hook") == "http://192.168.1.10/hook"


def test_webhook_formats():
    ev = {"title": "T", "message": "M", "event": "budget.threshold"}
    assert webhook_payload("https://hooks.slack.com/services/x", ev) == {"text": "T — M"}
    assert webhook_payload("https://discord.com/api/webhooks/1/x", ev) == {"content": "T — M"}
    assert webhook_payload("https://exemple.fr/hook", ev) == ev


# ---------- alertes de bout en bout ----------


def test_threshold_alerts_fire_once_with_signed_webhook(env):
    c, up = env
    r = c.post(
        "/v1/alerts",
        headers=MASTER,
        json={
            "name": "Budget test",
            "period": "day",
            "amount": 0.01,
            "currency": "EUR",
            "thresholds": [50, 100],
            "webhook_url": "https://hooks.example/ok",
        },
    )
    assert r.status_code == 201, r.text
    rule = r.json()
    secret = rule["webhook_secret"]
    assert secret.startswith("whsec_")
    c.post("/v1/chat/completions", headers=MASTER, json=chat("openai/gpt-4o"))  # 0,0041 € : 41 %
    time.sleep(0.2)
    assert c.get("/v1/notifications", headers=MASTER).json()["unread"] == 0
    c.post("/v1/chat/completions", headers=MASTER, json=chat("openai/gpt-4o"))  # 83 % -> palier 50
    notes = wait_for(lambda: c.get("/v1/notifications", headers=MASTER).json()["data"])
    assert len(notes) == 1 and notes[0]["level"] == "warning" and "83 %" in notes[0]["title"]
    for _ in range(2):  # 124 %, 166 % -> palier 100 une fois
        c.post("/v1/chat/completions", headers=MASTER, json=chat("openai/gpt-4o"))
    notes = wait_for(
        lambda: (lambda d: d if len(d) >= 2 else None)(c.get("/v1/notifications", headers=MASTER).json()["data"])
    )
    time.sleep(0.2)
    data = c.get("/v1/notifications", headers=MASTER).json()
    assert len(data["data"]) == 2 and data["data"][0]["level"] == "critical" and data["unread"] == 2
    assert len(up.hooks) == 2  # 1 webhook par palier
    h = up.hooks[0]
    sig = "sha256=" + hmac.new(secret.encode(), h.content, hashlib.sha256).hexdigest()
    assert h.headers["x-gateway-signature"] == sig
    body = json.loads(h.content)
    assert body["event"] == "budget.threshold" and body["threshold"] == 50 and body["currency"] == "EUR"
    listed = c.get("/v1/alerts", headers=MASTER).json()["data"][0]
    assert listed["current"]["pct"] > 100
    assert c.post("/v1/notifications/read", headers=MASTER, json={}).json()["updated"] == 2
    assert c.get("/v1/notifications", headers=MASTER).json()["unread"] == 0


def test_key_scope_rules_local_traffic_and_key_budget_notifications(env):
    c, up = env
    k = c.post(
        "/v1/keys",
        headers=MASTER,
        json={"name": "mobile", "limits": {"budget": {"amount": 0.008, "currency": "USD", "period": "month"}}},
    ).json()
    hk = {"Authorization": f"Bearer {k['key']}"}
    c.post(
        "/v1/alerts",
        headers=MASTER,
        json={
            "name": "Clé mobile",
            "key_id": k["id"],
            "period": "month",
            "amount": 0.005,
            "currency": "USD",
            "thresholds": [80],
        },
    )
    c.post("/v1/chat/completions", headers=MASTER, json=chat("openai/gpt-4o"))  # hors clé : ignoré
    time.sleep(0.2)
    assert c.get("/v1/notifications", headers=MASTER).json()["data"] == []
    for _ in range(3):
        c.post("/v1/chat/completions", headers=hk, json=chat("ollama/llama3.1:8b"))  # gratuit : rien
    c.post("/v1/chat/completions", headers=hk, json=chat("openai/gpt-4o"))  # 0,0045 $ : 90 % règle, 56 % budget
    notes = wait_for(lambda: c.get("/v1/notifications", headers=MASTER).json()["data"])
    assert [n["title"] for n in notes] == ["Budget « Clé mobile » : 90 % atteint"]
    # estimation basse (max_tokens 10) acceptée par le plafond 4.3, coût réel plus élevé : budget clé à 112 %
    r = c.post("/v1/chat/completions", headers=hk, json={**chat("openai/gpt-4o"), "max_tokens": 10})
    assert r.status_code == 200
    notes = wait_for(
        lambda: (lambda d: d if len(d) >= 3 else None)(c.get("/v1/notifications", headers=MASTER).json()["data"])
    )
    titles = [n["title"] for n in notes]
    assert "Clé « mobile » : budget épuisé, requêtes payantes bloquées" in titles
    assert any(t.startswith("Clé « mobile » : 1") for t in titles)  # palier 80 %
    assert not up.hooks


def test_alert_crud_and_webhook_test(env):
    c, up = env
    assert (
        c.post("/v1/alerts", headers=MASTER, json={"name": "x", "amount": 1, "key_id": "key_inconnue"}).status_code
        == 404
    )
    rule = c.post("/v1/alerts", headers=MASTER, json={"name": "x", "amount": 1}).json()
    assert rule["webhook_url"] is None and rule["webhook_secret"] is None
    assert c.post(f"/v1/alerts/{rule['id']}/test", headers=MASTER).status_code == 400
    upd = c.patch(
        f"/v1/alerts/{rule['id']}", headers=MASTER, json={"webhook_url": "https://hooks.example/fail", "enabled": False}
    ).json()
    assert upd["webhook_secret"] and upd["enabled"] is False
    assert c.post(f"/v1/alerts/{rule['id']}/test", headers=MASTER).json() == {"delivered": False}  # 500 x2
    c.patch(f"/v1/alerts/{rule['id']}", headers=MASTER, json={"webhook_url": "https://hooks.example/ok"})
    assert c.post(f"/v1/alerts/{rule['id']}/test", headers=MASTER).json() == {"delivered": True}
    k = c.post("/v1/keys", headers=MASTER, json={"name": "v"}).json()
    assert c.get("/v1/alerts", headers={"Authorization": f"Bearer {k['key']}"}).status_code == 403
    assert c.get("/v1/alerts", headers={"Authorization": "Bearer compte"}).json()["data"] == []
    assert c.delete(f"/v1/alerts/{rule['id']}", headers=MASTER).json()["deleted"]


# ---------- exports ----------


def test_csv_export_french_excel(env):
    c, _ = env
    k = c.post("/v1/keys", headers=MASTER, json={"name": "App ; « spéciale »"}).json()
    c.post("/v1/chat/completions", headers={"Authorization": f"Bearer {k['key']}"}, json=chat("openai/gpt-4o"))
    c.post("/v1/chat/completions", headers=MASTER, json=chat("ollama/llama3.1:8b"))
    r = c.get("/v1/metrics/export", headers=MASTER, params={"format": "csv"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="consommation-' in r.headers["content-disposition"]
    text = r.content.decode("utf-8")
    assert text.startswith("﻿")
    rows = list(csv.DictReader(io.StringIO(text[1:]), delimiter=";"))
    assert len(rows) == 2
    cloud = next(x for x in rows if x["fournisseur"] == "openai")
    assert cloud["cle"] == "App ; « spéciale »" and cloud["tokens_entree"] == "1000"
    assert cloud["cout_reel_usd"] == str(COST).replace(".", ",") and cloud["cache_reponse"] == "non"
    assert cloud["date_utc"].endswith("Z")
    en = c.get("/v1/metrics/export", headers=MASTER, params={"format": "csv", "locale": "en"}).content.decode()
    assert list(csv.DictReader(io.StringIO(en[1:])))[0]["cout_reel_usd"] in ("0", str(COST))
    # une clé n'exporte que ses propres requêtes
    mine = c.get("/v1/metrics/export", headers={"Authorization": f"Bearer {k['key']}"}).content.decode()
    assert len(mine.strip().splitlines()) == 2
    assert c.get("/v1/metrics/export", headers=MASTER, params={"format": "xlsx"}).status_code == 400


def test_json_and_pdf_exports(env):
    c, _ = env
    for m in ("openai/gpt-4o", "ollama/llama3.1:8b", "ollama/llama3.1:8b"):
        c.post("/v1/chat/completions", headers=MASTER, json=chat(m))
    j = c.get("/v1/metrics/export", headers=MASTER, params={"format": "json"}).json()
    assert j["summary"]["requests"] == 3 and len(j["rows"]) == 3
    assert j["savings"]["baseline_model"] == "openai/gpt-4o" and j["rows"][0]["date_utc"].endswith("Z")
    assert j["fx"]["usd_eur"] == 0.92
    r = c.get("/v1/metrics/export", headers=MASTER, params={"format": "pdf", "since": time.time() - 86400})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and len(r.content) > 2000
    from pypdf import PdfReader

    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
    assert "Rapport de consommation" in text and "€" in text and "Modèles locaux" in text
    assert "openai/gpt-4o" in text and "Détail par jour" in text


def test_pdf_day_labels_use_local_date(env):
    """Régression : à Paris (UTC+2), une requête à 23:30 UTC appartient au lendemain local."""
    from datetime import datetime
    from pypdf import PdfReader
    from gateway.plans import account_identity

    store = DocumentStore(":memory:")
    t = datetime(2026, 10, 13, 23, 30, tzinfo=UTC).timestamp()  # mer. 14/10 01:30 à Paris
    store.insert_metrics(
        [
            {
                "ts": t,
                "owner": account_identity("master"),
                "status": "ok",
                "provider": "ollama",
                "hosting": "local",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost_usd": 0,
                "fx_rate": 0.92,
                "latency_ms": 5,
            }
        ]
    )
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(Upstream())),
        plans=PlanResolver({}, "free", master_key="master"),
        store=store,
    )
    with TestClient(app) as c:
        r = c.get(
            "/v1/metrics/export",
            headers=MASTER,
            params={"format": "pdf", "since": t - 86400, "until": t + 86400, "tz_offset": -120},
        )
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
    detail = text.split("Détail par jour")[1]
    assert "14/10/2026" in detail and "13/10/2026" not in detail
    assert "du 13/10/2026 au 15/10/2026" in text  # période en heure locale


def test_csv_export_neutralizes_formulas():
    """Modèle demandé, nom de clé… : saisis par un client, jamais exécutés comme formule à l'ouverture dans Excel."""
    from gateway.exports import _cell

    assert _cell("model", '=HYPERLINK("http://x","clic")', ",") == '\'=HYPERLINK("http://x","clic")'
    assert _cell("key_name", "@SUM(1)", ",") == "'@SUM(1)" and _cell("key_name", "App", ",") == "App"
    assert _cell("prompt_tokens", -3, ",") == "-3" and _cell("cost_usd", -0.5, ",") == "-0,5"
