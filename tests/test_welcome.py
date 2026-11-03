"""Premier lancement guidé (32.4) : affiché au tableau de bord jusqu'à ce qu'il soit terminé, clé maître seulement."""

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer master"}


def test_welcome_is_shown_until_finished():
    store = DocumentStore(":memory:")
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))),
        plans=PlanResolver({"user-key": "free"}, "free", master_key="master"),
        store=store,
    )
    with TestClient(app) as c:
        assert c.get("/v1/admin/welcome", headers=MASTER).json() == {"done": False}
        assert c.post("/v1/admin/welcome", headers={"Authorization": "Bearer user-key"}, json={}).status_code == 403
        assert c.post("/v1/admin/welcome", headers=MASTER, json={}).json() == {"done": True}
        assert c.get("/v1/admin/welcome", headers=MASTER).json() == {"done": True}
        page = c.get("/admin?frame=1").text
        assert 'id="welcome-section"' in page and 'href="/models#ollama-section"' in page
        assert "initWelcome" in c.get("/static/welcome-ui.js").text
