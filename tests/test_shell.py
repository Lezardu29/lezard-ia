"""Cadre principal : chaque page reste chargée dans son cadre, un lien interne est demandé au cadre au lieu de
recharger la page (une requête en cours continue). Vérifié en code ; le comportement dans le navigateur reste à
vérifier sur le PC."""

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

PAGES = ("/app", "/admin", "/models")


@pytest.fixture
def client():
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))),
        plans=PlanResolver({}, "free", master_key="gw-master-0123456789abcdefghijklmnop"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        yield c


def test_every_page_route_serves_the_frame_without_the_query(client):
    for path in PAGES:
        html = client.get(path).text
        assert 'id="stage"' in html and "/static/shell.js" in html, path
        assert "frame.js" not in html, path  # le cadre n'a pas besoin du script des pages


def test_the_page_itself_is_served_inside_its_frame(client):
    for path in PAGES:
        html = client.get(f"{path}?frame=1").text
        assert 'id="stage"' not in html and "/static/shell.js" not in html, path
        assert "/static/frame.js" in html, path
        assert html.index("/static/frame.js") < html.index('type="module"'), path  # avant les modules


def test_pages_may_be_framed_by_this_site_only(client):
    for path in ("/app", "/app?frame=1", "/admin?frame=1"):
        csp = client.get(path).headers["content-security-policy"]
        assert "frame-ancestors 'self'" in csp and "frame-ancestors 'none'" not in csp, path


def test_frame_script_hands_internal_links_to_the_frame():
    script = client_static("frame.js")
    assert '"gw-nav"' in script and "event.preventDefault()" in script
    assert "window.parent === window" in script  # page ouverte seule : retour au cadre
    shell = client_static("shell.js")
    assert '"gw-nav"' in shell and "event.origin !== location.origin" in shell
    assert "leaveIfLogin" in shell and "window.top !== window" in shell  # cadre imbriqué remis à sa place


def client_static(name: str) -> str:
    from gateway.core import WEB_DIR

    return (WEB_DIR / "static" / name).read_text(encoding="utf-8")
