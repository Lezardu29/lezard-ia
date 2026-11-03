"""Installation en application (13.6) : manifeste, icônes, service worker limité aux fichiers statiques, CSP."""

import json
import re
import struct

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore


@pytest.fixture
def client():
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        plans=PlanResolver({"compte": "free"}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        yield c


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_manifest_and_icons(client):
    r = client.get("/manifest.webmanifest")  # public : le navigateur le lit sans clé
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    m = json.loads(r.text)
    assert m["start_url"] == "/app" and m["scope"] == "/" and m["display"] == "standalone"
    sizes = {(i["sizes"], i.get("purpose", "any")) for i in m["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes  # critères d'installation
    for icon in m["icons"] + [{"src": "/static/icons/apple-touch-icon.png", "sizes": "180x180"}]:
        img = client.get(icon["src"])
        assert img.status_code == 200
        w, h = png_size(img.content)
        assert f"{w}x{h}" == icon["sizes"], icon["src"]


def test_service_worker_caches_static_files_only(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-cache"  # une nouvelle version est prise aussitôt
    sw = r.text
    assert 'url.pathname.startsWith("/static/")' in sw and 'request.method === "GET"' in sw
    assert '!request.headers.has("authorization")' in sw
    assert sw.count("cache.put(") == 1  # un seul endroit écrit dans le cache : networkFirst
    code = "\n".join(line.split("//")[0] for line in sw.splitlines())
    assert "/v1" not in code  # aucune règle ne touche à l'API
    precache = sw.split("const PRECACHE = ")[1].split(";")[0]
    assert re.findall(r'"([^"]+)"', precache) and all(
        p.startswith("/static/") for p in re.findall(r'"([^"]+)"', precache)
    )
    assert 'const OFFLINE = "/static/offline.html"' in sw
    assert client.get("/static/offline.html").status_code == 200


def test_csp_allows_worker_and_manifest_from_self_only(client):
    csp = client.get("/app").headers["content-security-policy"]
    assert "worker-src 'self'" in csp and "manifest-src 'self'" in csp


def test_pages_link_manifest_and_install_button(client):
    for page in ("/", "/app", "/admin", "/models"):
        html = client.get(page if page == "/" else page + "?frame=1").text
        assert 'rel="manifest" href="/manifest.webmanifest"' in html and 'rel="apple-touch-icon"' in html, page
        if page != "/":
            assert 'id="install-btn"' in html and "hidden data-drawer" in html, page
    pwa = client.get("/static/pwa.js").text
    assert "window.isSecureContext" in pwa and "beforeinstallprompt" in pwa
    assert "https://" in client.get("/static/net-warning.js").text  # explication en HTTP sur le réseau local
