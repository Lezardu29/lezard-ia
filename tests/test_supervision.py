"""Supervision (18.5) : /metrics au format Prometheus et alertes d'exploitation (serveur local tombé, disque)."""

import re
from collections import namedtuple

import httpx
from fastapi.testclient import TestClient

from gateway import supervision
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore
from gateway.supervision import GB, Prometheus

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
ACCOUNT_KEY = "ct-compte-0123456789abcdefghij"
LINE = re.compile(r'^[a-z_]+(\{([a-z_]+="([^"\\]|\\.)*",?)*\})? -?[0-9.e+]+$|^# (HELP|TYPE) ')


class Upstream:
    def __init__(self):
        self.down = False
        self.fail_chat = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refusé")
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
        if self.fail_chat:
            return httpx.Response(500, json={"error": {"message": "panne"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            },
        )


def make(up, store=None, client=None):
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama:11434/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({ACCOUNT_KEY: "pro"}, "free", master_key=MASTER_KEY),
        store=store or DocumentStore(":memory:"),
    )
    return TestClient(app, client=client) if client else TestClient(app)


def chat(c):
    return c.post(
        "/v1/chat/completions",
        headers=MASTER,
        json={"model": "ollama/llama3.1:8b", "messages": [{"role": "user", "content": "Bonjour"}]},
    )


def metric(text: str, name: str, **labels) -> float | None:
    for line in text.splitlines():
        if line.startswith(name + "{") or line.startswith(name + " "):
            got = dict(re.findall(r'([a-z_]+)="((?:[^"\\]|\\.)*)"', line.split(" ")[0]))
            if all(got.get(k) == v for k, v in labels.items()):
                return float(line.rsplit(" ", 1)[1])
    return None


def test_metrics_in_prometheus_format_after_requests():
    up = Upstream()
    with make(up) as c:
        assert chat(c).status_code == 200
        r = c.get("/metrics", headers=MASTER)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain; version=0.0.4")
        text = r.text
        assert all(LINE.match(line) for line in text.strip().splitlines()), [
            line for line in text.splitlines() if not LINE.match(line)
        ]
        assert metric(text, "gateway_requests_total", provider="ollama", hosting="local", status="ok") == 1
        assert metric(text, "gateway_tokens_total", provider="ollama", type="prompt") == 12
        assert metric(text, "gateway_request_latency_seconds_count", provider="ollama") == 1
        assert metric(text, "gateway_request_latency_seconds_bucket", provider="ollama", le="+Inf") == 1
        assert metric(text, "gateway_up") == 1 and metric(text, "gateway_disk_free_bytes") > 0  # dossier data
        assert metric(text, "gateway_tunnel_up", state="stopped") == 0
        assert metric(text, "gateway_local_queue_requests", server="ollama", state="active") == 0
        assert metric(text, "gateway_local_busy") == 0
        up.fail_chat = True
        chat(c)
        text = c.get("/metrics", headers=MASTER).text
        assert metric(text, "gateway_provider_failures_total", provider="ollama", status="500") == 1


def test_metrics_access(monkeypatch):
    with make(Upstream()) as c:
        assert c.get("/metrics").status_code in (401, 403)
        assert c.get("/metrics", headers={"Authorization": f"Bearer {ACCOUNT_KEY}"}).status_code == 403
        monkeypatch.setenv("GATEWAY_METRICS_TOKEN", "court")  # trop court : ignoré
        assert c.get("/metrics", headers={"Authorization": "Bearer court"}).status_code in (401, 403)
        token = "prom-0123456789abcdefghijklmnop"
        monkeypatch.setenv("GATEWAY_METRICS_TOKEN", token)
        assert c.get("/metrics", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        assert c.get("/v1/admin/supervision", headers={"Authorization": f"Bearer {token}"}).status_code in (401, 403)
    with make(Upstream(), client=("127.0.0.1", 50000)) as local:  # ce PC, sans tunnel
        assert local.get("/metrics").status_code == 200


def test_label_escaping():
    p = Prometheus()
    p.inc("gateway_requests_total", {"provider": 'a"b\\c\nd'})
    assert 'provider="a\\"b\\\\c\\nd"' in p.render([])


def notifications(c):
    return [n["title"] for n in c.get("/v1/notifications", headers=MASTER).json()["data"]]


def check(c):
    return c.post("/v1/admin/supervision/check", headers=MASTER, json={}).json()


def test_local_server_down_then_back_notifies_once_each():
    up = Upstream()
    with make(up) as c:
        assert check(c)["servers"][0]["up"] is True and check(c)["fired"] == 0
        up.down = True
        assert check(c)["fired"] == 0  # un échec : pas encore tombé
        st = check(c)
        assert st["fired"] == 1 and st["servers"][0]["up"] is False and st["servers"][0]["error"]
        assert check(c)["fired"] == 0  # pas de répétition
        up.down = False
        assert check(c)["fired"] == 1
        titles = notifications(c)
        assert "Serveur local « ollama » injoignable" in titles and "Serveur local « ollama » rétabli" in titles
        text = c.get("/metrics", headers=MASTER).text
        assert metric(text, "gateway_local_server_up", server="ollama") == 1


def test_server_never_seen_up_does_not_nag():
    up = Upstream()
    up.down = True
    with make(up) as c:
        assert [check(c)["fired"] for _ in range(3)] == [0, 0, 0]
        assert check(c)["servers"][0]["up"] is False


def test_disk_threshold(monkeypatch, tmp_path):
    usage = namedtuple("usage", "total used free")
    free = [10 * GB]
    monkeypatch.setattr(supervision.shutil, "disk_usage", lambda p: usage(100 * GB, 0, free[0]))
    store = DocumentStore(str(tmp_path / "g.db"))
    with make(Upstream(), store) as c:
        assert c.put("/v1/admin/supervision", headers=MASTER, json={"disk_min_gb": 5}).status_code == 200
        assert check(c)["fired"] == 0
        free[0] = 3 * GB
        assert check(c)["fired"] == 1 and check(c)["fired"] == 0 and check(c)["disk_low"]
        free[0] = 5.5 * GB  # au-dessus du seuil, sous la marge
        assert check(c)["fired"] == 0
        free[0] = 7 * GB
        assert check(c)["fired"] == 1 and not check(c)["disk_low"]
        assert notifications(c)[:2] == ["Espace disque rétabli", "Disque presque plein"]
        assert metric(c.get("/metrics", headers=MASTER).text, "gateway_disk_free_bytes") == 7 * GB
        c.put("/v1/admin/supervision", headers=MASTER, json={"enabled": False})
        free[0] = 1 * GB
        assert check(c)["fired"] == 0  # alertes coupées
    store.close()


def test_webhook_settings_and_test_alert(monkeypatch):
    sent = []

    async def fake_send(self, url, secret, event):
        sent.append((url, secret, event))
        return True

    monkeypatch.setattr("gateway.alerts.AlertEngine.send", fake_send)
    with make(Upstream()) as c:
        for bad in ({"webhook_url": "ftp://x"}, {"disk_min_gb": -1}, {"enabled": "oui"}, {"bruit": 1}):
            assert c.put("/v1/admin/supervision", headers=MASTER, json=bad).status_code == 400, bad
        monkeypatch.setattr("gateway.alerts.resolve_webhook", lambda url: (None, ["203.0.113.9"]))  # DNS hors ligne
        st = c.put("/v1/admin/supervision", headers=MASTER, json={"webhook_url": "https://hooks.slack.com/x"}).json()
        assert st["webhook_url"] == "https://hooks.slack.com/x" and st["webhook_secret"].startswith("whsec_")
        c.post("/v1/admin/supervision/check", headers=MASTER, json={"test": True})
        assert sent and sent[0][0] == "https://hooks.slack.com/x" and sent[0][2]["event"] == "test"
        assert "Alerte d'essai" in notifications(c)
        assert c.get("/v1/admin/supervision", headers={"Authorization": f"Bearer {ACCOUNT_KEY}"}).status_code == 403
