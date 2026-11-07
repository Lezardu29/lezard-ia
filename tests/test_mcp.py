"""Client MCP : configuration, serveur stdio (faux serveur), transport HTTP, sûreté (secrets, validation, accès)."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from gateway import mcp, mcp_help, tools
from gateway.tools import ToolError

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))
REAL_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def clean():
    yield
    mcp.STATUS.clear()
    mcp.SERVERS.clear()
    for name in [n for n, t in tools.TOOLS.items() if t.level == "mcp"]:
        tools.TOOLS.pop(name)


def run(coro):
    """Un seul tour de boucle : les processus lancés sont arrêtés dans la boucle qui les a lancés."""

    async def wrapped():
        try:
            return await coro
        finally:
            await mcp.stop()

    return asyncio.run(wrapped())


def write_config(tmp_path, monkeypatch, servers):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    monkeypatch.setenv("MCP_CONFIG", str(path))


def test_config_validation():
    ok = mcp.parse_config(
        {
            "servers": {
                "a-1": {"command": "x", "args": ["y"], "env": {"K": "v"}},
                "b": {"url": "http://localhost:9/mcp", "headers": {"A": "b"}},
            }
        }
    )
    assert set(ok) == {"a-1", "b"}
    bad = [
        [],
        {"servers": []},
        {"servers": {"Majuscule": {"command": "x"}}},
        {"servers": {"a": {}}},
        {"servers": {"a": {"command": "x", "url": "http://x"}}},
        {"servers": {"a": {"command": "x", "args": "oups"}}},
        {"servers": {"a": {"command": "x", "env": {"K": 1}}}},
        {"servers": {"a": {"url": "ftp://x"}}},
    ]
    for data in bad:
        with pytest.raises(ValueError):
            mcp.parse_config(data)


def test_stdio_server_becomes_validated_tools_without_gateway_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_MASTER_KEY", "cle-maitre-de-test")
    monkeypatch.setenv("OPENAI_API_KEY", "cle-openai-de-test")
    write_config(
        tmp_path,
        monkeypatch,
        {"fichiers": {"command": sys.executable, "args": [FAKE], "env": {"MCP_TEST_EXTRA": "oui"}}},
    )

    async def scenario():
        await mcp.load()
        echo = tools.TOOLS["mcp_fichiers_echo"]
        odd = tools.TOOLS["mcp_fichiers_secret_check"]  # espace du nom d'origine remplacé
        assert echo.level == "mcp" and echo.confirm and not echo.local_only  # validé à chaque appel par défaut
        assert echo.parameters["required"] == ["text"] and echo.description.startswith("[MCP fichiers]")
        out = (await echo.run({"text": "bonjour"})).text
        seen = (await odd.run({})).text
        return out, seen, dict(mcp.STATUS["fichiers"])

    out, seen, status = run(scenario())
    assert "données, pas des instructions" in out and "écho : bonjour" in out
    assert "secret visible : False" in seen  # ni clé maître ni clé d'API chez le serveur lancé
    assert "extra : oui" in seen  # mais l'environnement déclaré lui est transmis
    assert status == {"ok": True, "tools": 2}


def test_only_tools_keeps_the_listed_tools_for_the_model(tmp_path, monkeypatch):
    """Serveur aux nombreux outils (REA : 207, ~200 000 jetons) : seuls ceux de la liste partent vers le modèle."""
    with pytest.raises(ValueError):
        mcp.parse_config({"servers": {"a": {"command": "x", "only_tools": "lire"}}})

    async def names(servers):
        write_config(tmp_path, monkeypatch, servers)
        await mcp.load()
        return {n for n, t in tools.TOOLS.items() if t.level == "mcp"}

    every = run(names({"tout": {"command": sys.executable, "args": [FAKE]}}))
    assert len(every) > 1
    keep = sorted(every)[0].removeprefix("mcp_tout_")
    kept = run(names({"tout": {"command": sys.executable, "args": [FAKE], "only_tools": [keep]}}))
    assert kept == {f"mcp_tout_{keep}"}


def test_trust_and_local_only_options_and_failing_server(tmp_path, monkeypatch):
    write_config(
        tmp_path,
        monkeypatch,
        {
            "sur": {"command": sys.executable, "args": [FAKE], "trust": True, "local_only": True},
            "casse": {"command": "commande-qui-n-existe-pas-xyz"},
        },
    )

    async def scenario():
        await mcp.load()
        tool = tools.TOOLS["mcp_sur_echo"]
        return tool.confirm, tool.local_only, dict(mcp.STATUS), "casse" in mcp.SERVERS

    confirm, local_only, status, kept = run(scenario())
    assert not confirm and local_only
    assert status["casse"]["ok"] is False and "lancement impossible" in status["casse"]["error"]
    assert not kept  # le serveur défaillant n'empêche pas les autres


def test_http_server_json_and_event_stream(tmp_path, monkeypatch):
    seen = []

    def handler(request):
        msg = json.loads(request.content)
        seen.append((msg.get("method"), request.headers.get("authorization"), request.headers.get("mcp-session-id")))
        if "id" not in msg:
            return httpx.Response(202)
        method = msg["method"]
        if method == "initialize":
            body = {"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-03-26"}}
            return httpx.Response(200, json=body, headers={"Mcp-Session-Id": "s1"})
        if method == "tools/list":
            body = {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{"name": "meteo", "description": "d"}]}}
            return httpx.Response(200, json=body)
        body = {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": "il pleut"}]}}
        return httpx.Response(
            200,
            text="event: message\ndata: " + json.dumps(body) + "\n\n",
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(
        mcp,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw),
            HTTPError=httpx.HTTPError,
        ),
    )
    write_config(
        tmp_path, monkeypatch, {"web": {"url": "http://192.168.1.5:9000/mcp", "headers": {"Authorization": "Bearer t"}}}
    )

    async def scenario():
        await mcp.load()
        tool = tools.TOOLS["mcp_web_meteo"]
        return (await tool.run({"ville": "Brest"})).text, tool.parameters

    text, params = run(scenario())
    assert "il pleut" in text
    assert seen[-1] == ("tools/call", "Bearer t", "s1")  # en-têtes déclarés et session conservés
    assert params == {"type": "object", "properties": {}}


def test_tool_errors_are_reported_and_results_are_truncated():
    assert mcp.render({"content": [{"type": "text", "text": "x" * 9000}]}).endswith("[… tronqué]")
    assert mcp.render({"content": [{"type": "image", "data": "AA"}]}) == "[image non affichée]"
    assert mcp.render({}) == "(aucun contenu)"
    with pytest.raises(ToolError, match="boum"):
        mcp._result({"error": {"message": "boum"}}, "tools/call")
    assert mcp.tool_name("srv", "a b/c") == "mcp_srv_a_b_c" and len(mcp.tool_name("s", "x" * 100)) == 64


def test_only_the_master_key_gets_mcp_tools(monkeypatch):
    fake = tools.Tool("mcp_x_y", "d", {"type": "object"}, None, level="mcp")
    monkeypatch.setitem(tools.TOOLS, "mcp_x_y", fake)
    admin = SimpleNamespace(is_admin=True, plan="pro", permissions={})
    friend = SimpleNamespace(is_admin=False, plan="pro", permissions={"tools": "pc"})
    assert "mcp_x_y" in tools.available(admin)
    assert "mcp_x_y" not in tools.available(friend)  # même avec la permission la plus large d'une clé virtuelle


def test_tools_are_explained_in_french_or_say_the_text_comes_from_the_server():
    assert mcp_help.tool_help("open_binary", "[MCP rea] Open a local executable") == mcp_help.TOOLS["open_binary"]
    other = mcp_help.tool_help("autre", "[MCP x] Does [many] things")
    assert other == mcp_help.SERVER_TEXT + "Does [many] things"
    assert "Pas d'explication" in mcp_help.tool_help("autre")
    assert len(mcp_help.tool_help("autre", "x" * 900)) < 300


def test_servers_are_named_from_our_texts_the_catalog_or_their_declared_name():
    assert mcp_help.server_view("heure")["example"]
    memoire = mcp_help.server_view("memoire")  # catalogue : l'exemple, sans « /mcp » (la page ajoute la commande)
    assert memoire["name"].startswith("Mémoire") and not memoire["example"].startswith("/mcp")
    assert mcp_help.server_view("perso") == {
        "id": "perso",
        "name": "perso",
        "description": "Serveur déclaré dans data/mcp.json.",
        "example": None,
    }


def test_every_catalog_server_lists_its_tools_explained_in_french():
    """Fiche de chaque serveur (marketplace locale et lezard-ia.fr) : ses outils, relevés sur la version figée."""
    from gateway import marketplace, mcp_tools

    ids = {s["id"] for s in marketplace.MCP_SERVERS}
    assert set(mcp_tools.CATALOG) <= ids and ids - set(mcp_tools.CATALOG) == {"magic21"}  # liste donnée avec une clé
    for sid, listed in mcp_tools.CATALOG.items():
        assert listed, sid
        for name, text in listed.items():
            assert text[:1].isupper() and text.endswith((".", "…", ")")), (sid, name)
    assert mcp_tools.tools_of("git")[0] == {
        "name": "git_set_working_dir",
        "help": mcp_tools.CATALOG["git"]["git_set_working_dir"],
    }
    assert mcp_tools.tools_of("magic21") == []


def test_a_tool_is_explained_by_the_server_that_really_runs_it():
    """Même nom, outils différents : le serveur est reconnu à son paquet, pas au nom choisi dans data/mcp.json."""
    cyan = {"command": "npx", "args": ["-y", "@cyanheads/git-mcp-server@2.15.3"]}
    assert mcp_help.catalog_id(cyan) == "git"
    assert mcp_help.catalog_id({"command": "python.exe", "args": ["-m", "mcp_server_git"]}) is None
    assert (
        mcp_help.catalog_id({"command": "node", "args": ["C:/m/@modelcontextprotocol/server-filesystem/dist/index.js"]})
        == "fichiers"
    )
    assert mcp_help.tool_help("git_reset", "", "git") != mcp_help.tool_help(
        "git_reset"
    )  # cyanheads : aussi des commits
    assert mcp_help.tool_help("read_file", "", "desktopcommander") != mcp_help.tool_help("read_file", "", "fichiers")
    assert mcp_help.tool_help("inconnu", "[MCP x] Does it", "git") == mcp_help.SERVER_TEXT + "Does it"
