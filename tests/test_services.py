"""Clés des services MCP (GitHub, Brave…) : plusieurs clés par service, étiquette et accès, chiffrement, restriction
aux dépôts précis appliquée à chaque appel."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import mcp, services
from gateway.config import ProviderConfig
from gateway.crypto import Cipher
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore
from gateway.tools import ToolError

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
SECRET = "github_pat_test_0123456789abcdef"


def make():
    cipher = Cipher(b"\x07" * 32)
    store = DocumentStore(":memory:")
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=store,
        cipher=cipher,
    )
    return TestClient(app), store, cipher


def add(c, **body):
    payload = {"label": "GitHub perso", "service": "github", "secret": SECRET, "scope": {"mode": "all"}, **body}
    return c.post("/v1/admin/services/keys", headers=MASTER, json=payload)


def test_key_is_stored_encrypted_and_never_returned():
    c, store, cipher = make()
    with c:
        r = add(c)
        assert r.status_code == 200 and r.json()["data"]["id"] == "github-perso"
        raw = store.get_credential("svc:github-perso")
        assert SECRET not in raw and cipher.decrypt(raw, "svc:github-perso") == SECRET
        listing = c.get("/v1/admin/services", headers=MASTER).json()
        assert SECRET not in json.dumps(listing) and SECRET not in json.dumps(r.json())
        assert listing["keys"][0]["hint"] and listing["keys"][0]["hint"] != SECRET


def test_several_keys_for_the_same_service_get_distinct_ids():
    c, _, _ = make()
    with c:
        assert add(c, label="GitHub client").json()["data"]["id"] == "github-client"
        assert add(c, label="GitHub client").json()["data"]["id"] == "github-client-2"
        assert len(c.get("/v1/admin/services", headers=MASTER).json()["keys"]) == 2


def test_repo_scope_is_validated():
    c, _, _ = make()
    with c:
        ok = add(c, label="Client", scope={"mode": "repos", "repos": ["acme/site", "acme/api"]})
        assert ok.json()["data"]["scope"] == {"mode": "repos", "repos": ["acme/site", "acme/api"]}
        assert add(c, label="Vide", scope={"mode": "repos", "repos": []}).status_code == 400
        assert add(c, label="Mauvais", scope={"mode": "repos", "repos": ["sans-slash"]}).status_code == 400


def test_brave_has_no_repo_scope():
    c, _, _ = make()
    with c:
        r = add(
            c, label="Brave", service="brave", secret="BSA-test-0123456789", scope={"mode": "repos", "repos": ["a/b"]}
        )
        assert r.json()["data"]["scope"] == {"mode": "all"}


def test_unknown_service_and_short_secret_are_refused():
    c, _, _ = make()
    with c:
        assert add(c, service="inconnu").status_code == 400
        assert add(c, secret="court").status_code == 400


def test_delete_removes_the_key_and_its_secret():
    c, store, _ = make()
    with c:
        add(c)
        assert c.delete("/v1/admin/services/keys/github-perso", headers=MASTER).status_code == 200
        assert store.get_credential("svc:github-perso") is None
        assert c.delete("/v1/admin/services/keys/github-perso", headers=MASTER).status_code == 404


def test_admin_routes_require_the_master_key():
    c, _, _ = make()
    with c:
        assert c.get("/v1/admin/services").status_code in (401, 403)
        assert c.post("/v1/admin/services/keys", json={}).status_code in (401, 403)


def test_one_server_per_key_with_its_scope(tmp_path):
    c, store, cipher = make()
    with c:
        add(c, label="GitHub client", scope={"mode": "repos", "repos": ["acme/site"]})
        add(c, label="GitHub perso")
        configs = services.server_configs(store, cipher)
    assert set(configs) == {"github-client", "github-perso"}
    client = configs["github-client"]
    assert client["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": SECRET}
    assert client["args"] == ["stdio", "--read-only"] and client["scope"] == {"mode": "repos", "repos": ["acme/site"]}
    assert "scope" not in configs["github-perso"]  # tous les dépôts : aucune restriction ajoutée


class FakeServer:
    name = "github-client"

    def __init__(self):
        self.calls = []

    async def request(self, method, params, limit):
        self.calls.append(params["arguments"])
        return {"result": {"content": [{"type": "text", "text": "ok"}]}}


def restricted_tool(server):
    spec = {"name": "get_file_contents", "inputSchema": {"type": "object"}}
    cfg = {"label": "GitHub client", "scope": {"mode": "repos", "repos": ["Acme/Site"]}}
    return mcp.make_tool(server, cfg, spec)


def test_restricted_key_only_reaches_listed_repos():
    server = FakeServer()
    tool = restricted_tool(server)
    assert "ok" in asyncio.run(tool.run({"owner": "acme", "repo": "site", "path": "README.md"})).text  # casse ignorée
    with pytest.raises(ToolError, match="n'est pas accessible"):
        asyncio.run(tool.run({"owner": "acme", "repo": "autre"}))
    with pytest.raises(ToolError, match="dépôts précis"):
        asyncio.run(tool.run({"query": "toutes les issues"}))  # aucun dépôt désigné : refusé
    assert server.calls == [{"owner": "acme", "repo": "site", "path": "README.md"}]  # un seul appel réel


def test_unrestricted_key_is_not_checked():
    server = FakeServer()
    tool = mcp.make_tool(server, {"label": "x"}, {"name": "search", "inputSchema": {"type": "object"}})
    assert "ok" in asyncio.run(tool.run({"query": "n'importe quoi"})).text


def test_declared_servers_are_kept_and_key_servers_are_added(tmp_path, monkeypatch):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"servers": {"heure": {"command": "x"}}}), encoding="utf-8")
    monkeypatch.setenv("MCP_CONFIG", str(config))
    seen = {}

    async def fake_connect(name, cfg):
        seen[name] = cfg
        return 1

    monkeypatch.setattr(mcp, "_connect", fake_connect)
    extra = {"github-perso": {"command": "g", "args": [], "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": SECRET}}}
    asyncio.run(mcp.load(lambda: extra))
    assert set(seen) == {"heure", "github-perso"}
    assert seen["github-perso"]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == SECRET
    assert {m["name"] for m in mcp.overview()} == {"heure", "github-perso"}
    asyncio.run(mcp.stop())
