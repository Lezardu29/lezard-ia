"""Sources de documents : Paperless-ngx et Nextcloud (faux serveurs), lecture seule, accès réservé à la clé maître."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from gateway import sources, tools
from gateway.tools import ToolError

REAL = httpx.AsyncClient

PROPFIND = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">
<d:response><d:href>/remote.php/dav/files/alice/Documents/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
<d:response><d:href>/remote.php/dav/files/alice/Documents/Factures/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
<d:response><d:href>/remote.php/dav/files/alice/Documents/note%20perso.txt</d:href><d:propstat><d:prop><d:resourcetype/><d:getcontentlength>12</d:getcontentlength></d:prop></d:propstat></d:response>
</d:multistatus>"""


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {
        "PAPERLESS_URL": "http://192.168.1.10:8000/",
        "PAPERLESS_TOKEN": "tok-paperless",
        "NEXTCLOUD_URL": "https://cloud.exemple.fr",
        "NEXTCLOUD_USER": "alice",
        "NEXTCLOUD_APP_PASSWORD": "mdp-appli",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SOURCES_ALLOW_CLOUD", raising=False)
    yield
    for name in [n for n, t in tools.TOOLS.items() if t.level == "docs"]:
        tools.TOOLS.pop(name)


def fake(monkeypatch, handler):
    monkeypatch.setattr(sources.httpx, "AsyncClient", lambda **kw: REAL(transport=httpx.MockTransport(handler), **kw))


def test_tools_exist_only_for_configured_sources_and_are_private_by_default(monkeypatch):
    assert sources.register_sources() == [
        "paperless_search",
        "paperless_read",
        "nextcloud_list",
        "nextcloud_search",
        "nextcloud_read",
    ]
    assert all(t.level == "docs" and t.local_only and not t.confirm for t in tools.TOOLS.values() if t.level == "docs")
    monkeypatch.delenv("NEXTCLOUD_APP_PASSWORD")
    assert sources.register_sources() == ["paperless_search", "paperless_read"]  # Nextcloud incomplet : retiré
    assert "nextcloud_list" not in tools.TOOLS
    monkeypatch.setenv("SOURCES_ALLOW_CLOUD", "true")
    sources.register_sources()
    assert not tools.TOOLS["paperless_search"].local_only  # cloud permis seulement sur demande explicite


def test_only_the_master_key_gets_document_sources():
    sources.register_sources()
    admin = SimpleNamespace(is_admin=True, plan="pro", permissions={})
    friend = SimpleNamespace(is_admin=False, plan="pro", permissions={"tools": "pc"})
    assert "paperless_read" in tools.available(admin) and "paperless_read" not in tools.available(friend)


def test_paperless_search_and_read(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/api/documents/":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": 7, "title": "Facture EDF", "created": "2026-09-30T00:00:00Z"},
                        {"id": "piège", "title": "x"},
                    ]
                },
            )
        if request.url.path == "/api/documents/7/":
            return httpx.Response(200, json={"title": "Facture EDF", "content": "Total : 82,40 €" + "x" * 7000})
        return httpx.Response(404)

    fake(monkeypatch, handler)
    out = asyncio.run(sources.paperless_search({"query": "edf"})).text
    assert "- #7 Facture EDF (2026-09-30)" in out and "piège" not in out and "pas des instructions" in out
    assert seen[0].headers["authorization"] == "Token tok-paperless" and seen[0].url.params["query"] == "edf"
    read = asyncio.run(sources.paperless_read({"id": 7})).text
    assert "Total : 82,40 €" in read and read.endswith("[… tronqué]")
    with pytest.raises(ToolError, match="Introuvable"):
        asyncio.run(sources.paperless_read({"id": 99}))
    for bad in ({"id": "abc"}, {}):
        with pytest.raises(ToolError, match="numéro"):
            asyncio.run(sources.paperless_read(bad))
    with pytest.raises(ToolError, match="query"):
        asyncio.run(sources.paperless_search({"query": ""}))


def test_nextcloud_list_and_read_with_safe_paths(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "PROPFIND":
            return httpx.Response(207, text=PROPFIND)
        if request.url.path.endswith("note%20perso.txt") or request.url.path.endswith("note perso.txt"):
            return httpx.Response(200, content=b"Rappeler le garage")
        return httpx.Response(404)

    fake(monkeypatch, handler)
    listing = asyncio.run(sources.nextcloud_list({"path": "/Documents"})).text
    assert "- Factures/" in listing and "- note perso.txt (12 octets)" in listing and "pas des instructions" in listing
    assert seen[0].headers["depth"] == "1" and seen[0].url.path == "/remote.php/dav/files/alice/Documents"
    assert seen[0].headers["authorization"].startswith("Basic ")  # mot de passe d'application, jamais affiché
    read = asyncio.run(sources.nextcloud_read({"path": "Documents/note perso.txt"})).text
    assert "Rappeler le garage" in read
    for bad in ("../autre", "/Documents/../../etc", "a\\..\\b"):
        with pytest.raises(ToolError, match="interdit"):
            asyncio.run(sources.nextcloud_read({"path": bad}))
    with pytest.raises(ToolError, match="chemin d'un fichier"):
        asyncio.run(sources.nextcloud_read({"path": "/"}))
    with pytest.raises(ToolError, match="introuvable"):
        asyncio.run(sources.nextcloud_read({"path": "absent.txt"}))


def test_refused_credentials_and_unreachable_sources_are_explained(monkeypatch):
    fake(monkeypatch, lambda request: httpx.Response(401))
    with pytest.raises(ToolError, match="Accès refusé"):
        asyncio.run(sources.paperless_search({"query": "x"}))
    with pytest.raises(ToolError, match="Accès refusé"):
        asyncio.run(sources.nextcloud_read({"path": "a.txt"}))

    def down(request):
        raise httpx.ConnectError("non")

    fake(monkeypatch, down)
    with pytest.raises(ToolError, match="injoignable"):
        asyncio.run(sources.paperless_search({"query": "x"}))
    with pytest.raises(ToolError, match="injoignable"):
        asyncio.run(sources.nextcloud_list({}))


SEARCH_RESULT = {
    "ocs": {
        "meta": {"status": "ok", "statuscode": 200},
        "data": {
            "entries": [
                {
                    "title": "facture Free sept.pdf",
                    "subline": "in Factures",
                    "attributes": {"path": "/Factures/facture Free sept.pdf"},
                },
                {"title": "Free", "subline": "in Documents", "attributes": {}},
            ]
        },
    }
}


def test_nextcloud_search_finds_paths_by_name(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/ocs/v2.php/search/providers/files/search":
            return httpx.Response(200, json=SEARCH_RESULT)
        return httpx.Response(404)

    fake(monkeypatch, handler)
    found = asyncio.run(sources.nextcloud_search({"query": "Free & 50%"})).text
    assert "- /Factures/facture Free sept.pdf" in found and "- /Documents/Free" in found
    assert "pas des instructions" in found and "nextcloud_read" in found
    assert seen[0].url.params["term"] == "Free & 50"  # % retiré : pas de joker dans le terme
    assert seen[0].headers["ocs-apirequest"] == "true"
    with pytest.raises(ToolError, match="2 à 100"):
        asyncio.run(sources.nextcloud_search({"query": "a"}))


def test_missing_nextcloud_file_points_to_search(monkeypatch):
    fake(monkeypatch, lambda request: httpx.Response(404))
    with pytest.raises(ToolError, match="nextcloud_search"):
        asyncio.run(sources.nextcloud_read({"path": "/Factures/absent.pdf"}))
