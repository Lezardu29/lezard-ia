"""Base SQLite hors de la boucle d'événements (10.2) — sur une vraie base sur disque."""

import sqlite3
import time

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
OK = {
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 1000},
}


def upstream(request):
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]})
    return httpx.Response(200, json=OK)


def test_file_database_uses_wal_and_separate_reader(tmp_path):
    store = DocumentStore(str(tmp_path / "g.db"))
    assert store._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store._db.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert store._read is not store._db and store._read_lock is not store._lock
    # le lecteur ne peut pas écrire…
    try:
        store._read.execute("DELETE FROM api_keys")
        raise AssertionError("écriture acceptée sur la connexion de lecture")
    except sqlite3.OperationalError:
        pass
    # … et voit ce que l'autre connexion vient de valider
    store.insert_metrics(
        [
            {
                "ts": time.time(),
                "owner": "acct:a",
                "status": "ok",
                "provider": "openai",
                "cost_usd": 0.5,
                "billed_usd": 0.5,
                "billed_eur": 0.45,
                "fx_rate": 0.9,
            }
        ]
    )
    assert store.metrics_summary("acct:a")["requests"] == 1
    # base en mémoire : une seule connexion (une seconde verrait une autre base vide)
    mem = DocumentStore(":memory:")
    assert mem._read is mem._db


def test_last_used_written_at_most_once_a_minute(tmp_path, monkeypatch):
    store = DocumentStore(str(tmp_path / "g.db"))
    key = store.add_key("acct:a", {"name": "k", "plan": "free", "key_prefix": "sk-app-x"}, "h1")
    now = [1000.0]
    monkeypatch.setattr("gateway.store.time.time", lambda: now[0])
    writes = []
    store._db.set_trace_callback(lambda sql: writes.append(sql) if sql.startswith("UPDATE api_keys") else None)
    for _ in range(50):
        _, k = store.key_by_hash("h1")
    assert len(writes) == 1 and k["last_used_at"] == 1000.0
    now[0] += 61
    store.key_by_hash("h1")
    assert len(writes) == 2 and store.get_key("acct:a", key["id"])["last_used_at"] == 1061.0


def test_dashboard_on_file_database(tmp_path):
    """Chemin complet sur disque : chat avec clé + budget, puis tableau de bord, exports, alertes."""
    store = DocumentStore(str(tmp_path / "g.db"))
    app = create_app(
        [ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k")],
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=store,
    )
    with TestClient(app) as c:
        k = c.post(
            "/v1/keys", headers=MASTER, json={"name": "k", "limits": {"budget": {"amount": 100, "period": "month"}}}
        ).json()
        h = {"Authorization": f"Bearer {k['key']}"}
        body = {"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "Bonjour"}]}
        for _ in range(5):
            assert c.post("/v1/chat/completions", headers=h, json=body).status_code == 200
        c.post("/v1/alerts", headers=MASTER, json={"name": "a", "amount": 1})
        s = c.get("/v1/metrics/summary?since=0", headers=MASTER).json()
        assert s["requests"] == 5 and s["spend_eur"] > 0
        ch = c.get("/v1/metrics/charts?since=0", headers=MASTER).json()
        assert sum(p["requests"] for p in ch["timeseries"]["points"]) == 5
        assert c.get("/v1/metrics/requests", headers=MASTER).json()["total"] == 5
        assert c.get("/v1/savings", headers=MASTER).status_code == 200
        assert c.get("/v1/alerts", headers=MASTER).json()["data"][0]["current"]["spent"] > 0
        assert c.get("/v1/metrics/export?format=pdf", headers=MASTER).content.startswith(b"%PDF")
        assert c.get("/v1/metrics/export?format=csv", headers=MASTER).text.count("\n") == 6
        assert c.get(f"/v1/keys/{k['id']}/usage", headers=MASTER).json()["spend"]["month"]["spent_usd"] > 0
