import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore


class Upstream:
    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            ids = {"ollama": ["llama3.1:8b"], "openai": ["gpt-4o", "gpt-4o-mini"]}[request.url.host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GATEWAY_TRUST_PROXY", "true")
    app = create_app(
        [
            ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
            ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(Upstream())),
        plans=PlanResolver({}, "free", master_key="master", require_key=True),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        yield c


def test_pages_are_served_with_security_headers(client):
    for path, marker in (("/", 'id="login-form"'), ("/app?frame=1", 'id="logout"')):
        r = client.get(path)
        assert r.status_code == 200 and marker in r.text
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'self'" in csp
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "<script>" not in r.text  # aucun script inline
    for asset in ("styles.css", "auth.js", "login.js", "app.js"):
        assert client.get(f"/static/{asset}").status_code == 200
    assert client.get("/static/../main.py").status_code == 404


def test_login_flow_errors(client):
    assert client.get("/v1/me").status_code == 401  # require_key
    r = client.get("/v1/me", headers={"Authorization": "Bearer sk-app-faux"})
    assert r.status_code == 401 and r.json()["error"]["message"] == "Clé API invalide."
    k = client.post("/v1/keys", headers={"Authorization": "Bearer master"}, json={"name": "web"}).json()
    client.post(
        f"/v1/keys/{k['id']}/suspend", headers={"Authorization": "Bearer master"}, json={"reason": "maintenance"}
    )
    r = client.get("/v1/me", headers={"Authorization": f"Bearer {k['key']}"})
    assert r.status_code == 403 and "maintenance" in r.json()["error"]["message"]
    me = client.get("/v1/me", headers={"Authorization": "Bearer master"}).json()
    assert me["type"] == "account" and "quota" not in me
