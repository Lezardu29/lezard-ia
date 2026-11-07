"""Marketplace (gateway/marketplace.py, api/marketplace.py) : licences des modèles, composants, serveurs MCP."""

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import installers, marketplace, mcp, mcp_help, skills
from gateway.api import marketplace as market_api
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER, ACCOUNT = {"Authorization": "Bearer master"}, {"Authorization": "Bearer compte"}
WEB = Path(__file__).resolve().parents[1] / "gateway" / "web"
APACHE = "                                 Apache License\n                           Version 2.0, January 2004\n"
LLAMA = "LLAMA 3.1 COMMUNITY LICENSE AGREEMENT\nLlama 3.1 Version Release Date: July 23, 2024\nCopyright © Meta Platforms, Inc."


# ---------- résumé d'une licence ----------


@pytest.mark.parametrize(
    "text, name, commercial",
    [
        (LLAMA, "Llama Community License", "conditions"),
        ("Gemma Terms of Use\n\nLast modified: February 21, 2024", "Gemma Terms of Use", "conditions"),
        (APACHE, "Apache-2.0", "oui"),
        (
            "Microsoft.\nCopyright (c) Microsoft Corporation.\n\nMIT License\n\nPermission is hereby granted, free of charge",
            "MIT",
            "oui",
        ),
        (
            "Creative Commons Attribution-NonCommercial 4.0 International Public License",
            "Creative Commons BY-NC 4.0",
            "non",
        ),
        ("Creative Commons Attribution 4.0 International", "Creative Commons BY 4.0", "oui"),
        ("Qwen RESEARCH LICENSE AGREEMENT", "Qwen Research License", "non"),
        ("MISTRAL AI NON-PRODUCTION LICENSE", "Mistral AI Non-Production / Research License", "non"),
        ("GNU AFFERO GENERAL PUBLIC LICENSE Version 3", "AGPL-3.0", "conditions"),
        ("Licence maison : faites-en bon usage.", "Licence propre", "inconnu"),
    ],
)
def test_classify_recognises_common_model_licenses(text, name, commercial):
    out = marketplace.classify(text)
    assert (out["name"], out["commercial"]) == (name, commercial) and out["summary"] and out["disclaimer"]


def test_classify_extracts_the_copyright_holder_and_handles_no_license():
    assert marketplace.classify(LLAMA)["copyright"] == "Meta Platforms, Inc"
    assert (
        marketplace.classify("Copyright © Meta Platforms, Inc. All Rights Reserved.")["copyright"]
        == "Meta Platforms, Inc"
    )
    assert (
        marketplace.classify(
            "MIT License\n\nCopyright (c) 2023 DeepSeek\n\nPermission is hereby granted, free of charge"
        )["copyright"]
        == "2023 DeepSeek"
    )
    empty = marketplace.classify(None)
    assert empty["name"] == "Non précisée" and empty["commercial"] == "inconnu"


# ---------- licence d'un modèle : registre d'Ollama ----------


class Registry:
    def __init__(self, license_text=APACHE, status=200):
        self.text, self.status, self.calls = license_text, status, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if self.status != 200:
            return httpx.Response(self.status)
        if "/manifests/" in request.url.path:
            assert request.headers["accept"] == marketplace.MANIFEST_TYPE
            layers = [
                {"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:m", "size": 4_900_000_000},
                {"mediaType": "application/vnd.ollama.image.license", "digest": "sha256:l", "size": len(self.text)},
            ]
            return httpx.Response(200, json={"layers": layers})
        if request.url.path.endswith("/blobs/sha256:l"):
            return httpx.Response(200, text=self.text)
        return httpx.Response(404)


def run(coro):
    import asyncio

    return asyncio.run(coro)


def test_model_license_reads_the_registry_once_then_caches():
    reg, store = Registry(), DocumentStore(":memory:")

    async def twice():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reg)) as client:
            a = await marketplace.model_license(client, store, "qwen3", "8b")
            b = await marketplace.model_license(client, store, "qwen3", "8b")
            return a, b

    a, b = run(twice())
    assert a["name"] == "Apache-2.0" and a["download_bytes"] == 4_900_000_000 and a["text"].strip().startswith("Apache")
    assert a["source"] == "https://ollama.com/library/qwen3:8b" and b == a
    assert len(reg.calls) == 2  # manifeste + licence, une seule fois


def test_model_license_errors_and_stale_cache():
    store = DocumentStore(":memory:")

    async def fetch(reg, name="qwen3", tag="latest"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(reg)) as client:
            return await marketplace.model_license(client, store, name, tag)

    with pytest.raises(marketplace.MarketplaceError) as e:
        run(fetch(Registry(), name="../etc"))
    assert e.value.status == 400
    with pytest.raises(marketplace.MarketplaceError) as e:
        run(fetch(Registry(status=404)))
    assert e.value.status == 404
    with pytest.raises(marketplace.MarketplaceError) as e:
        run(fetch(Registry(status=503)))
    assert e.value.status == 502
    # déjà lue puis périmée : le registre injoignable, la dernière lecture sert
    run(fetch(Registry()))
    cache = store.get_setting(marketplace.LICENSE_SETTING)
    cache["qwen3:latest"]["fetched_at"] = 0
    store.set_setting(marketplace.LICENSE_SETTING, cache)
    out = run(fetch(Registry(status=503)))
    assert out["stale"] and out["name"] == "Apache-2.0"


def test_model_without_license_layer():
    class NoLicense(Registry):
        def __call__(self, request):
            return httpx.Response(
                200, json={"layers": [{"mediaType": "application/vnd.ollama.image.model", "size": 10}]}
            )

    store = DocumentStore(":memory:")

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(NoLicense())) as client:
            return await marketplace.model_license(client, store, "x", "latest")

    out = run(fetch())
    assert out["name"] == "Non précisée" and out["text"] is None


# ---------- serveurs MCP : data/mcp.json ----------


def test_add_and_remove_mcp_servers_keep_other_declarations(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {"perso": {"url": "http://localhost:9000/mcp"}}}), encoding="utf-8")
    monkeypatch.setenv("MCP_CONFIG", str(path))
    cfg = marketplace.add_mcp("memoire", "npx")
    assert cfg == {
        "command": "npx",
        "args": ["-y", f"@modelcontextprotocol/server-memory@{marketplace.MCP_VERSION}"],
        "env": [],
    }
    with pytest.raises(marketplace.MarketplaceError, match="dossier"):
        marketplace.add_mcp("fichiers", "npx", "relatif/dossier")
    marketplace.add_mcp("fichiers", "npx", str(tmp_path))
    saved = json.loads(path.read_text(encoding="utf-8"))["servers"]
    assert set(saved) == {"perso", "memoire", "fichiers"} and saved["fichiers"]["args"][-1] == str(tmp_path)
    assert marketplace.installed_mcp() == {"perso", "memoire", "fichiers"}
    assert marketplace.remove_mcp("memoire") and not marketplace.remove_mcp("memoire")
    with pytest.raises(marketplace.MarketplaceError):
        marketplace.add_mcp("inconnu", "npx")
    path.write_text("{oups", encoding="utf-8")
    with pytest.raises(marketplace.MarketplaceError) as e:
        marketplace.add_mcp("memoire", "npx")
    assert e.value.status == 409 and marketplace.installed_mcp() == set()


def test_service_servers_need_their_key_and_never_return_it(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    monkeypatch.setenv("MCP_CONFIG", str(path))
    for bad in (None, "court", "avec espace dedans"):
        with pytest.raises(marketplace.MarketplaceError, match="Jeton API Apify"):
            marketplace.add_mcp("apify", "npx", secret=bad)
    cfg = marketplace.add_mcp("apify", "npx", secret="apify_api_0123456789")
    assert cfg["env"] == ["APIFY_TOKEN"] and "apify_api_0123456789" not in json.dumps(cfg)
    saved = json.loads(path.read_text(encoding="utf-8"))["servers"]["apify"]
    assert saved["env"] == {"APIFY_TOKEN": "apify_api_0123456789"}  # sur ce PC seulement
    assert saved["args"] == ["-y", "@apify/actors-mcp-server@0.18.4"]
    pw = marketplace.add_mcp("playwright", "npx")
    assert pw["args"][-1] == "@playwright/mcp@0.0.83" and pw["env"] == []


def test_catalog_entries_credit_their_licenses():
    for c in marketplace.COMPONENTS:
        assert all(lic["name"] and lic["copyright"] and lic["url"].startswith("https://") for lic in c["licenses"])
    for a in marketplace.APPS:
        assert a["license"]["name"] and a["license"]["copyright"] and a["url"].startswith("https://")
    assert marketplace.MCP_LICENSE["url"] == "https://github.com/modelcontextprotocol/servers"
    for entry in (*marketplace.MCP_SERVERS, *marketplace.AGENT_TOOLS):
        lic = entry["license"]
        assert lic["name"] and lic["copyright"] and lic["url"].startswith("https://"), entry["id"]
    # les dix outils de la liste : présents dans un des catalogues
    ids = {e["id"] for e in (*marketplace.MCP_SERVERS, *marketplace.AGENT_TOOLS)}
    assert {"playwright", "magic21", "apify", "hyperframes", "watch", "rea", "gws", "findskills", "orca"} <= ids
    assert "naturel" in {s["id"] for s in skills.catalog()}  # Humanizer : compétence du chat
    with pytest.raises(marketplace.MarketplaceError):
        marketplace.component("inconnu")


# ---------- API ----------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "mcp.json"))
    reg = Registry(LLAMA)
    app = create_app(
        None,
        httpx.AsyncClient(transport=httpx.MockTransport(reg)),
        plans=PlanResolver({"compte": "free"}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        yield c


def test_catalog_is_for_the_master_key(client):
    assert client.get("/v1/admin/marketplace", headers=ACCOUNT).status_code == 403
    r = client.get("/v1/admin/marketplace", headers=MASTER).json()
    assert {c["id"] for c in r["components"]} == {"voix", "multi", "web", "pc", "ollama"}
    assert all("action" not in c for c in r["components"])  # la commande lancée reste côté serveur
    assert [s["id"] for s in r["mcp"]] == [
        "memoire",
        "reflexion",
        "fichiers",
        "playwright",
        "magic21",
        "apify",
        "agentmemory",
        "context7",
        "chromedevtools",
        "desktopcommander",
        "brave",
        "notion",
        "firecrawl",
        "tavily",
        "exa",
        "searxng",
        "n8n",
        "figma",
        "git",
        "airtable",
        "perplexity",
    ]
    assert not any(s["installed"] for s in r["mcp"]) and r["agent_tools"]
    assert r["skills"] and r["apps"] and r["disclaimer"]
    assert not r["installer_mode"] and all(c["manual"] for c in r["components"])  # lancé depuis le code


def test_model_license_route(client):
    r = client.get("/v1/admin/marketplace/models/llama3.1/license?tag=8b", headers=MASTER).json()
    assert r["name"] == "Llama Community License" and r["copyright"] == "Meta Platforms, Inc"
    assert client.get("/v1/admin/marketplace/models/llama3.1/license", headers=ACCOUNT).status_code == 403
    assert client.get("/v1/admin/marketplace/models/Pas%20Bon/license", headers=MASTER).status_code == 400


def test_skill_license_route(client):
    r = client.get("/v1/skills/concis/license", headers=ACCOUNT).json()
    assert r["license"] == "Apache-2.0" and "Apache License" in r["text"] and "Caveman" in r["notice"]
    assert client.get("/v1/skills/inconnue/license", headers=ACCOUNT).status_code == 404


class FakeProc:
    def __init__(self, code=0):
        self.code = code

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        pass


def fake_popen(monkeypatch, started, code=0):
    def popen(args, **kw):
        kw["stdout"].write("sortie de " + Path(args[0]).stem + "\n")
        started.append(args)
        return FakeProc(code)

    monkeypatch.setattr(market_api.subprocess, "Popen", popen)


def wait_job(client) -> dict:
    for _ in range(200):
        state = client.get("/v1/admin/marketplace/job", headers=MASTER).json()
        if state["state"] != "running":
            return state
        time.sleep(0.02)
    raise AssertionError("installation jamais terminée")


def fake_pc(tmp_path, monkeypatch, docker=(0, "open-webui\t\nlibrechat-api\tlibrechat\n"), pip_code=0):
    """Un PC de test : dossier personnel vide, npm et uv répondent, tous les programmes sont là."""
    home, npm_root, uv_dir = tmp_path / "home", tmp_path / "npm", tmp_path / "uv"
    for folder in (home, npm_root, uv_dir):
        folder.mkdir()
    monkeypatch.setattr(market_api.detection.Path, "home", lambda: home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(installers, "program", lambda name: name)

    def run(*argv):
        answers = {"npm": (0, f"{npm_root}\n"), "uv": (0, f"{uv_dir}\n"), "docker": docker}
        return answers.get(argv[0], (pip_code, ""))

    monkeypatch.setattr(market_api.detection, "_run", run)
    return home, npm_root, uv_dir


def test_detection_reads_each_kind_of_recipe(tmp_path, monkeypatch):
    home, npm_root, uv_dir = fake_pc(tmp_path, monkeypatch)
    detection = market_api.detection
    (home / ".claude" / "plugins").mkdir(parents=True)
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"claude-mem@thedotmack": [], "superpowers@superpowers-marketplace": []}}),
        encoding="utf-8",
    )
    (home / ".claude" / "skills" / "find-skills").mkdir(parents=True)
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"rea": {}, "agentmemory": {}}}), encoding="utf-8")
    (home / ".vscode" / "extensions").mkdir(parents=True)
    (home / ".vscode" / "extensions" / "extensions.json").write_text(
        json.dumps([{"identifier": {"id": "continue.continue"}}]), encoding="utf-8"
    )
    (npm_root / "ccusage").mkdir()
    (npm_root / "@googleworkspace" / "cli").mkdir(parents=True)
    (uv_dir / "markitdown").mkdir()
    (tmp_path / "local" / "Programs" / "Goose").mkdir(parents=True)
    facts = detection.Facts()
    facts.__dict__["windows_apps"] = {"jan 0.8.6", "cursor (user)", "januszek"}  # lecture du registre : Windows seul
    for key in (
        "tool:rea", "tool:claudemem", "tool:superpowers", "tool:findskills", "tool:agentmemory", "tool:ccusage",
        "tool:gws", "tool:markitdown", "app:continue", "app:goose", "app:jan", "app:cursor", "app:openwebui",
        "app:librechat", "app:python",
    ):  # fmt: skip
        assert detection.state(key, facts) is True, key
    for key in ("tool:hyperframes", "tool:repomix", "tool:speckit", "app:cline", "app:zed", "tool:orca"):
        assert detection.state(key, facts) is False, key
    assert detection.state("tool:serena", facts) is False  # ni sa commande, ni son serveur


def test_detection_covers_the_whole_catalog_and_says_when_it_cannot_know(tmp_path, monkeypatch):
    fake_pc(tmp_path, monkeypatch, docker=(1, ""), pip_code=1)
    detection = market_api.detection
    facts = detection.Facts()
    facts.__dict__["windows_apps"] = set()
    states = {k: detection.state(k, facts) for k in installers.RECIPES if k.startswith(("tool:", "app:"))}
    # Docker arrêté et extension peut-être dans Firefox : on ne sait pas ; tout le reste a une réponse
    assert {k for k, v in states.items() if v is None} == {"app:openwebui", "app:librechat", "app:pageassist"}
    assert not any(states.values())


def test_detection_ignores_data_left_behind_and_unreadable_files(tmp_path, monkeypatch):
    home, _, _ = fake_pc(tmp_path, monkeypatch)
    detection = market_api.detection
    (home / ".claude-mem").mkdir()  # sa mémoire reste après une désinstallation
    (home / "LibreChat").mkdir()
    assert detection.state("tool:claudemem", detection.Facts()) is False
    monkeypatch.setattr(detection, "_run", lambda *a: (0, ""))
    assert detection.state("app:librechat", detection.Facts()) is False
    (home / ".claude.json").write_text("{cassé", encoding="utf-8")
    assert detection.state("tool:rea", detection.Facts()) is None


def test_detection_tells_the_page_what_is_really_here(client, monkeypatch, install_log):
    """Présent sans être passé par la page : installé et désinstallable. Noté par la page mais parti : plus installé."""
    fake_popen(monkeypatch, [])
    monkeypatch.setattr(
        market_api.detection,
        "scan",
        lambda: {"tool:ccusage": True, "tool:rea": False, "tool:gws": None, "tool:repomix": None},
    )
    for tool in ("rea", "gws"):  # notés par la page
        assert client.post(f"/v1/admin/marketplace/tools/{tool}/install", headers=MASTER).status_code == 202
        assert wait_job(client)["state"] == "done"
    tools = {t["id"]: t for t in client.get("/v1/admin/marketplace", headers=MASTER).json()["agent_tools"]}
    assert tools["ccusage"]["installed"] and tools["ccusage"]["uninstall"] == ["npm uninstall --global ccusage"]
    assert not tools["rea"]["installed"]
    assert tools["gws"]["installed"] and not tools["repomix"]["installed"]  # inconnu : ce que la page a noté
    seen = client.get("/v1/marketplace/installed", headers=MASTER).json()
    assert seen["tools"] == ["ccusage", "gws"]


def test_tools_route_says_which_server_each_mcp_tool_belongs_to(client, tmp_path):
    """Ce que lit la commande /mcp : le serveur, son état, ses outils expliqués ; la clé maître seule voit l'erreur."""
    fake = str(Path(__file__).with_name("fake_mcp_server.py"))
    (tmp_path / "mcp.json").write_text(
        json.dumps(
            {"servers": {"heure": {"command": sys.executable, "args": [fake]}, "casse": {"command": "pas-la-xyz"}}}
        ),
        encoding="utf-8",
    )
    assert client.post("/v1/admin/mcp/reload", headers=MASTER).status_code == 200
    seen = client.get("/v1/tools", headers=MASTER).json()
    rows = [t for t in seen["data"] if t["level"] == "mcp"]
    assert rows and all(t["server"] == "heure" and t["tool"] and t["help"] for t in rows)
    assert all(t["name"] == mcp.tool_name("heure", t["tool"]) for t in rows)
    assert "server" not in next(t for t in seen["data"] if t["level"] != "mcp")
    servers = {s["id"]: s for s in seen["mcp_servers"]}
    assert servers["heure"]["connected"] and servers["heure"]["tools"] == len(rows)
    assert servers["heure"]["name"] == mcp_help.SERVERS["heure"]["name"]
    assert not servers["casse"]["connected"] and servers["casse"]["error"] and servers["casse"]["tools"] == 0
    other = client.get(
        "/v1/tools", headers=ACCOUNT
    ).json()  # pas d'outil MCP pour cette clé : aucun serveur, aucune erreur
    assert other["mcp_servers"] == [] and not any(t["level"] == "mcp" for t in other["data"])


def test_the_site_marketplace_also_gets_what_the_local_one_knows_about_this_pc(client):
    """Marketplace de lezard-ia.fr : les conseils de la page locale (matériel, modèles, bundles, MCP connectés)."""
    pc = client.get("/v1/marketplace/installed", headers=MASTER).json()["pc"]
    assert {"budget", "best", "uses", "models"} <= set(pc["recommendations"])
    assert isinstance(pc["fits"], dict) and isinstance(pc["mcp_connected"], list)
    assert pc["bundles"] and {"suggested", "why", "why_not", "parts"} <= set(pc["bundles"][0])
    local = client.get("/v1/admin/models/recommendations", headers=MASTER).json()
    assert pc["recommendations"]["best"] == local["best"]  # les mêmes conseils que la page locale
    for tag, (fit, memory, _speed) in pc["fits"].items():
        assert ":" in tag and fit in ("vram", "offload", "too_big") and memory > 0
    assert client.get("/v1/marketplace/installed", headers=ACCOUNT).status_code == 403


@pytest.fixture
def install_log(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_LOG", str(tmp_path / "logs" / "marketplace.log"))
    monkeypatch.setattr(
        installers, "program", lambda name: sys.executable if name == installers.PYTHON else f"C:/bin/{name}.cmd"
    )


def test_components_install_from_the_code_too(client, monkeypatch, tmp_path, install_log):
    started = []
    fake_popen(monkeypatch, started)
    monkeypatch.setattr(market_api.updates, "ROOT", tmp_path)
    assert client.post("/v1/admin/marketplace/components/inconnu/install", headers=MASTER).status_code == 404
    assert client.get("/v1/admin/marketplace/job", headers=MASTER).json() == {"state": "idle"}
    r = client.post("/v1/admin/marketplace/components/voix/install", headers=MASTER)
    assert r.status_code == 202 and r.json()["restart"]
    job = wait_job(client)
    assert job["state"] == "done" and job["restart"]
    assert started == [
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements-local.txt"]
    ]
    me = Path(sys.executable).stem  # python sous Windows et en CI, python3 sur d'autres Linux
    assert job["log"][0].startswith("> python") and f"sortie de {me}" in job["log"] and job["log"][-1] == "Terminé."
    assert str(tmp_path) not in "\n".join(job["log"])  # chemins de ce PC : jamais dans la page


def test_tool_switches_are_written_to_env_and_active_at_once(client, monkeypatch, tmp_path, install_log):
    monkeypatch.setattr(market_api.updates, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("GATEWAY_MASTER_KEY=secret\nTOOLS_WEB=false\n", encoding="utf-8")
    monkeypatch.setenv("TOOLS_WEB", "false")
    r = client.post("/v1/admin/marketplace/components/web/install", headers=MASTER)
    assert r.status_code == 202 and r.json()["state"] == "done"
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "GATEWAY_MASTER_KEY=secret\nTOOLS_WEB=true\n"
    assert market_api.tools.web_enabled()


def test_component_install_runs_the_launcher(client, monkeypatch, tmp_path, install_log):
    started = []
    fake_popen(monkeypatch, started)
    monkeypatch.setattr(market_api.updates, "installer_mode", lambda: True)
    monkeypatch.setattr(market_api.updates, "ROOT", tmp_path)
    r = client.post("/v1/admin/marketplace/components/multi/install", headers=MASTER)
    assert r.status_code == 202 and r.json()["restart"]
    assert wait_job(client)["state"] == "done"
    assert started[-1][1:] == [str(tmp_path / "lezard.py"), "pack", "install", "multi"]
    assert client.get("/v1/admin/marketplace", headers=MASTER).json()["components"][0]["installable"]


def test_agent_tools_and_apps_install_in_one_click(client, monkeypatch, install_log):
    started = []
    fake_popen(monkeypatch, started)
    visible = [{"id": "rea"}]  # commandes du chat qui marchent déjà (chat_tools.available)
    monkeypatch.setattr(market_api.chat_tools, "available", lambda store: visible)
    r = client.post("/v1/admin/marketplace/tools/rea/install", headers=MASTER)
    assert r.status_code == 202
    job = wait_job(client)
    assert job["state"] == "done" and not job["restart"]  # /rea marche déjà : rien à redémarrer
    assert job["after"].startswith("Dans le chat de Lezard IA : /rea <demande>.")
    assert started == [["C:/bin/npx.cmd", "--yes", "rea-agents@6.7.0", "setup", "--client", "claude_code", "--yes"]]
    visible.clear()  # programme pas encore visible par Lezard IA (PATH lu au démarrage) : redémarrage proposé
    client.post("/v1/admin/marketplace/tools/rea/install", headers=MASTER)
    assert wait_job(client)["restart"]
    cat = client.get("/v1/admin/marketplace", headers=MASTER).json()
    rea = next(t for t in cat["agent_tools"] if t["id"] == "rea")
    assert (
        rea["installed"]
        and rea["one_click"]
        and rea["missing"] == []
        and rea["steps"][0].startswith("npx --yes rea-agents@")
    )
    assert not next(a for a in cat["apps"] if a["id"] == "cursor")["installed"]
    rea_entry = next(t for t in cat["agent_tools"] if t["id"] == "rea")
    assert rea_entry["size"] == "29 Mo" and "Claude Code" in rea_entry["where"]
    assert client.post("/v1/admin/marketplace/tools/inconnu/install", headers=MASTER).status_code == 404
    assert client.post("/v1/admin/marketplace/autre/rea/install", headers=MASTER).status_code == 404
    assert client.post("/v1/admin/marketplace/tools/rea/install", headers=ACCOUNT).status_code == 403


def test_no_restart_for_tools_without_chat_command(client, monkeypatch, install_log):
    """Bibliothèque Python, extension du navigateur… : rien à redémarrer dans Lezard IA."""
    fake_popen(monkeypatch, [])
    monkeypatch.setattr(market_api.chat_tools, "available", lambda store: [])
    assert "python" not in market_api.marketplace.CHAT
    client.post("/v1/admin/marketplace/apps/python/install", headers=MASTER)
    job = wait_job(client)
    assert job["state"] == "done" and not job["restart"]


def test_installed_items_uninstall_with_the_reverse_steps(client, monkeypatch, install_log):
    started = []
    fake_popen(monkeypatch, started)
    monkeypatch.setattr(installers, "program", lambda name: f"C:/bin/{name}.exe")
    client.post("/v1/admin/marketplace/tools/serena/install", headers=MASTER)
    assert wait_job(client)["state"] == "done"
    cat = client.get("/v1/admin/marketplace", headers=MASTER).json()
    serena = next(t for t in cat["agent_tools"] if t["id"] == "serena")
    assert serena["installed"] and serena["uninstall"] == [
        "claude mcp remove --scope user serena",  # ordre inverse de l'installation
        "uv tool uninstall serena-agent",
    ]
    started.clear()
    assert client.post("/v1/admin/marketplace/tools/serena/uninstall", headers=ACCOUNT).status_code == 403
    assert client.post("/v1/admin/marketplace/tools/serena/uninstall", headers=MASTER).status_code == 202
    assert wait_job(client)["state"] == "done"
    assert [Path(a[0]).stem for a in started] == ["claude", "uv"]
    cat = client.get("/v1/admin/marketplace", headers=MASTER).json()
    assert not next(t for t in cat["agent_tools"] if t["id"] == "serena")["installed"]
    # installateur Windows ou paquet pip partagé avec Lezard IA : pas depuis la page, et on dit comment
    jan = client.post("/v1/admin/marketplace/apps/jan/uninstall", headers=MASTER)
    assert jan.status_code == 409 and "Paramètres > Applications" in jan.json()["error"]["message"]
    assert client.post("/v1/admin/marketplace/apps/python/uninstall", headers=MASTER).status_code == 409
    assert installers.uninstall_steps("app:openwebui") == [("docker", "rm", "--force", "open-webui")]  # volume gardé
    assert installers.uninstall_steps("tool:superpowers") == [
        ("claude", "plugin", "uninstall", "superpowers@superpowers-marketplace")
    ]


def test_failed_install_stops_and_shows_the_log(client, monkeypatch, install_log):
    started = []
    fake_popen(monkeypatch, started, code=1)
    client.post("/v1/admin/marketplace/tools/hyperframes/install", headers=MASTER)
    job = wait_job(client)
    assert job["state"] == "error" and job["code"] == 1 and len(started) == 1  # la 2e étape n'est pas lancée
    assert job["log"][-1] == "Échec (code 1)."
    cat = client.get("/v1/admin/marketplace", headers=MASTER).json()
    assert not next(t for t in cat["agent_tools"] if t["id"] == "hyperframes")["installed"]


def test_missing_programs_are_reported(client, monkeypatch, install_log):
    monkeypatch.setattr(installers, "program", lambda name: None)
    cat = client.get("/v1/admin/marketplace", headers=MASTER).json()
    assert next(a for a in cat["apps"] if a["id"] == "openwebui")["missing"] == ["docker"]
    r = client.post("/v1/admin/marketplace/apps/openwebui/install", headers=MASTER)
    assert r.status_code == 409 and "Requis sur ce PC : docker" in r.json()["error"]["message"]


def test_missing_dependencies_are_installed_first(client, monkeypatch, install_log):
    """Node.js absent : winget l'installe d'abord, puis npm (trouvé une fois le PATH relu) installe l'outil."""
    present = {"winget", installers.PYTHON}
    monkeypatch.setattr(installers.sys, "platform", "win32")
    monkeypatch.setattr(installers, "refresh_path", lambda: None)
    monkeypatch.setattr(installers, "program", lambda name: f"C:/bin/{name}.cmd" if name in present else None)
    cat = client.get("/v1/admin/marketplace", headers=MASTER).json()
    gws = next(t for t in cat["agent_tools"] if t["id"] == "gws")
    assert [d["name"] for d in gws["deps"]] == ["Node.js (LTS, 24 ou plus)"] and gws["missing"] == []
    assert gws["deps"][0]["license"] == "MIT" and gws["deps"][0]["size"]
    started = []

    def popen(args, **kw):
        started.append(args)
        if "OpenJS.NodeJS.LTS" in args:
            present.update({"node", "npm", "npx"})  # Node.js vient d'arriver
        return FakeProc(0)

    monkeypatch.setattr(market_api.subprocess, "Popen", popen)
    assert client.post("/v1/admin/marketplace/tools/gws/install", headers=MASTER).status_code == 202
    job = wait_job(client)
    assert job["state"] == "done", job["log"]
    assert started[0][:4] == ["C:/bin/winget.cmd", "install", "--id", "OpenJS.NodeJS.LTS"]
    assert started[1] == ["C:/bin/npm.cmd", "install", "--global", "@googleworkspace/cli@0.22.5"]
    assert job["log"][0].startswith("> winget install --id OpenJS.NodeJS.LTS")


def test_too_old_node_is_updated_like_a_missing_one(client, monkeypatch, install_log):
    """Node.js 22 présent : les serveurs MCP officiels 2.0.0 veulent 24, il est donc mis à jour par winget d'abord."""
    major = {"v": 22}
    monkeypatch.setattr(installers.sys, "platform", "win32")
    monkeypatch.setattr(installers, "refresh_path", lambda: None)
    monkeypatch.setattr(installers, "program", lambda name: f"C:/bin/{name}.cmd")
    monkeypatch.setattr(installers, "_major", lambda exe: major["v"])
    assert installers.too_old("npx") and installers.too_old("node") and not installers.too_old("git")
    gws = next(t for t in client.get("/v1/admin/marketplace", headers=MASTER).json()["agent_tools"] if t["id"] == "gws")
    assert [d["id"] for d in gws["deps"]] == ["node"] and gws["missing"] == []
    assert installers.plan("tool:gws")[0][:4] == ("winget", "install", "--id", "OpenJS.NodeJS.LTS")
    major["v"] = None  # version illisible : on ne bloque pas
    assert installers.plan("tool:gws")[0][0] == "npm"
    major["v"] = 24
    assert installers.dependencies("tool:gws") == []
    # sans winget : dit quelle version installer soi-même
    major["v"] = 20
    monkeypatch.setattr(installers, "program", lambda name: None if name == "winget" else f"C:/bin/{name}.cmd")
    assert installers.missing("tool:gws") == ["npm 24 ou plus"]


def test_mcp_server_with_too_old_node_updates_it_first(client, monkeypatch, tmp_path, install_log):
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "mcp.json"))
    major = {"v": 22}
    monkeypatch.setattr(installers.sys, "platform", "win32")
    monkeypatch.setattr(installers, "refresh_path", lambda: None)
    monkeypatch.setattr(installers, "program", lambda name: f"C:/bin/{name}.cmd")
    monkeypatch.setattr(installers, "_major", lambda exe: major["v"])
    monkeypatch.setattr(installers.shutil, "which", lambda name: "C:/node/npx.cmd" if name.startswith("npx") else None)
    started = []

    def popen(args, **kw):
        started.append(args)
        major["v"] = 24  # winget vient de mettre Node.js à jour
        return FakeProc(0)

    monkeypatch.setattr(market_api.subprocess, "Popen", popen)
    cat = client.get("/v1/admin/marketplace", headers=MASTER).json()
    memoire = next(s for s in cat["mcp"] if s["id"] == "memoire")
    assert [d["id"] for d in memoire["deps"]] == ["node"] and cat["node"] is False
    r = client.post("/v1/admin/marketplace/mcp/memoire", headers=MASTER, json={})
    assert r.status_code == 202
    job = wait_job(client)
    assert job["state"] == "done", job["log"]
    assert "OpenJS.NodeJS.LTS" in started[0]
    # mise à jour sans effet (ancien Node.js plus haut dans le PATH) : message clair, rien de déclaré
    major["v"] = 22
    monkeypatch.setattr(market_api.subprocess, "Popen", lambda args, **kw: FakeProc(0))
    assert (
        client.post("/v1/admin/marketplace/mcp/fichiers", headers=MASTER, json={"folder": str(tmp_path)}).status_code
        == 202
    )
    job = wait_job(client)
    assert job["state"] == "error" and any("Node.js 24 ou plus introuvable" in line for line in job["log"]), job["log"]
    assert "fichiers" not in json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))["servers"]


def test_watch_brings_ffmpeg_and_yt_dlp():
    deps = [d["id"] for d in installers.possible_dependencies("tool:watch")]
    assert deps == ["claude", "ffmpeg", "yt-dlp"]
    assert [d["id"] for d in installers.possible_dependencies(names=["npx"])] == ["node"]


def test_mcp_server_waits_for_node_then_is_declared(client, monkeypatch, tmp_path, install_log):
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "mcp.json"))
    node = {"ok": False}
    monkeypatch.setattr(installers.sys, "platform", "win32")
    monkeypatch.setattr(installers, "refresh_path", lambda: None)
    monkeypatch.setattr(
        installers,
        "program",
        lambda name: None if name in ("node", "npm", "npx") and not node["ok"] else f"C:/bin/{name}.cmd",
    )

    def popen(args, **kw):
        node["ok"] = True
        return FakeProc(0)

    monkeypatch.setattr(market_api.subprocess, "Popen", popen)
    bad = client.post("/v1/admin/marketplace/mcp/apify", headers=MASTER, json={})
    assert bad.status_code == 400  # jeton manquant : vu avant d'installer Node.js
    r = client.post("/v1/admin/marketplace/mcp/memoire", headers=MASTER, json={})
    assert r.status_code == 202 and r.json()["state"] == "running"
    job = wait_job(client)
    assert job["state"] == "done", job["log"]
    saved = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))["servers"]["memoire"]
    assert saved["command"] == "C:/bin/npx.cmd"  # npx trouvé une fois Node.js installé (PATH relu)


def test_one_install_at_a_time(client, monkeypatch, install_log):
    gate = threading.Event()

    class Slow(FakeProc):
        def wait(self, timeout=None):
            gate.wait(5)
            return 0

    monkeypatch.setattr(market_api.subprocess, "Popen", lambda args, **kw: Slow())
    assert client.post("/v1/admin/marketplace/apps/python/install", headers=MASTER).status_code == 202
    busy = client.post("/v1/admin/marketplace/apps/langchain/install", headers=MASTER)
    assert busy.status_code == 409 and "déjà en cours" in busy.json()["error"]["message"]
    gate.set()
    assert wait_job(client)["state"] == "done"


def test_recipes_are_fixed_and_pinned():
    every = {f"tool:{t['id']}" for t in marketplace.AGENT_TOOLS} | {f"app:{a['id']}" for a in marketplace.APPS}
    assert every <= set(installers.RECIPES)  # tout outil et toute application s'installe depuis la page
    for key, r in installers.RECIPES.items():
        for argv in r.get("steps", ()):
            assert all(isinstance(a, str) and not any(c in a for c in "&|;`$%") for a in argv), key  # sans shell
            if argv[0] in ("npm", "npx"):  # paquet npm : version figée, jamais @latest
                assert [a for a in argv if re.search(r"[a-z]@\d+\.\d+\.\d+$", a)], key
        assert "latest" not in " ".join(" ".join(argv) for argv in r.get("steps", ())), key


def test_every_installed_item_can_be_removed_or_says_how():
    """Rien ne reste sans issue : désinstallation depuis la page, ou explication de ce qu'il faut faire."""
    for key in (k for k in installers.RECIPES if k.startswith(("tool:", "app:"))):
        assert installers.uninstall_steps(key) or installers.uninstall_hint(key), key
    assert installers.uninstall_steps("tool:rea") == [
        ("npx", "--yes", "rea-agents@6.7.0", "uninstall")
    ]  # sans --purge-data
    assert installers.uninstall_steps("tool:claudemem") == [("npx", "--yes", "claude-mem@15.0.1", "uninstall")]
    assert installers.uninstall_steps("tool:codegraph") == [
        ("codegraph", "uninstall", "--keep-cli", "--yes"),  # d'abord les agents, tant que la commande existe
        ("npm", "uninstall", "--global", "@colbymchenry/codegraph"),
    ]
    assert "find-skills" in installers.uninstall_steps("tool:findskills")[0]
    assert "pip uninstall openai." in installers.uninstall_hint("app:python")
    assert "extensions" in installers.uninstall_hint("app:pageassist")


def test_claude_mem_recipe_does_not_need_a_running_observer():
    """« --provider host » finit en échec (code 1) tant qu'aucun service n'écoute sur 37777 : constaté en vrai."""
    assert installers.recipe("tool:claudemem")["steps"][0][-2:] == ("--provider", "claude")
    assert "--provider host" not in next(t for t in marketplace.AGENT_TOOLS if t["id"] == "claudemem")["install"]


def test_mcp_routes_declare_and_reload(client, monkeypatch):
    reloads = []

    async def fake_load(extra=None):
        reloads.append("load")
        mcp.STATUS["memoire"] = {"ok": True, "tools": 3}

    async def fake_stop():
        mcp.STATUS.clear()

    monkeypatch.setattr(market_api.mcp, "load", fake_load)
    monkeypatch.setattr(market_api.mcp, "stop", fake_stop)
    monkeypatch.setattr(installers.shutil, "which", lambda name: None)
    assert client.post("/v1/admin/marketplace/mcp/memoire", headers=MASTER, json={}).status_code == 409  # pas de Node
    monkeypatch.setattr(installers.shutil, "which", lambda name: "C:/node/npx.cmd")
    r = client.post("/v1/admin/marketplace/mcp/memoire", headers=MASTER, json={}).json()
    assert r["connected"] and r["declared"]["command"] == "C:/node/npx.cmd"
    bad = client.post("/v1/admin/marketplace/mcp/magic21", headers=MASTER, json={})
    assert bad.status_code == 400 and "21st.dev" in bad.json()["error"]["message"]
    listed = {s["id"]: s for s in client.get("/v1/admin/marketplace", headers=MASTER).json()["mcp"]}
    assert listed["memoire"]["installed"] and listed["memoire"]["connected"]
    assert client.post("/v1/admin/marketplace/mcp/fichiers", headers=MASTER, json={"folder": ""}).status_code == 400
    assert client.delete("/v1/admin/marketplace/mcp/memoire", headers=MASTER).json()["removed"]
    assert client.delete("/v1/admin/marketplace/mcp/inconnu", headers=MASTER).status_code == 404
    assert client.post("/v1/admin/marketplace/mcp/memoire", headers=ACCOUNT, json={}).status_code == 403
    assert reloads == ["load", "load"]
    mcp.STATUS.clear()


def test_marketplace_is_a_page_of_its_own(client):
    page = client.get("/marketplace").text
    assert "marketplace.js" in page and "shell.js" not in page and "frame.js" not in page  # hors du cadre du chat
    assert 'href="/?next=/marketplace"' in page  # visite libre, connexion pour installer
    login = (WEB / "static" / "login.js").read_text(encoding="utf-8")
    assert (
        'get("next") === "/marketplace" ? "/marketplace" : "/app"' in login
    )  # liste fermée : jamais une adresse libre
    for name in ("app.html", "admin.html", "models.html"):
        html = (WEB / name).read_text(encoding="utf-8")
        link = html[html.index('href="/marketplace"') :].split(">", 1)[0]
        assert "data-market" in link and "hidden" not in link, name  # pour tout compte, ouvert par frame.js
    frame = (WEB / "static" / "frame.js").read_text(encoding="utf-8")
    assert 'window.open(link.href, "_blank")' in frame  # garde le lien avec l'onglet : connexion reprise
    market = (WEB / "static" / "marketplace.js").read_text(encoding="utf-8")
    assert "opener.location.origin !== location.origin" in market  # même site seulement


# ---------- visiteurs (sans clé) ----------


def test_visitors_see_the_catalog_without_anything_from_this_pc(client):
    r = client.get("/v1/marketplace")
    assert r.status_code == 200
    data = r.json()
    assert data["skills"] and data["agent_tools"] and data["apps"] and data["mcp"]
    assert all("installed" not in c and "action" not in c for c in data["components"])
    assert all("installed" not in s for s in data["mcp"])
    assert "node" not in data and "installer_mode" not in data
    assert data["library"] is None  # bibliothèque d'Ollama injoignable dans ce test : le reste du catalogue s'affiche


def test_visitors_read_skills_and_licenses(client):
    sk = client.get("/v1/marketplace/skills/naturel").json()
    assert (
        sk["content"].startswith("# Écriture naturelle")
        and "Siqi Chen" in sk["license_text"]
        and "Humanizer" in sk["notice"]
    )
    assert client.get("/v1/marketplace/skills/inconnue").status_code == 404
    lic = client.get("/v1/marketplace/models/llama3.1/license?tag=8b").json()
    assert lic["name"] == "Llama Community License"


def test_new_license_lookups_by_visitors_are_limited(client, monkeypatch):
    monkeypatch.setattr(market_api, "PUBLIC_FETCHES", 2)
    assert client.get("/v1/marketplace/models/a/license").status_code == 200
    assert client.get("/v1/marketplace/models/b/license").status_code == 200
    assert client.get("/v1/marketplace/models/c/license").status_code == 429
    assert client.get("/v1/marketplace/models/a/license").status_code == 200  # déjà lue : sans limite
    assert client.get("/v1/admin/marketplace/models/d/license", headers=MASTER).status_code == 200  # clé maître


# ---------- copie publique sur lezard-ia.fr (site/marketplace) ----------
SITE = Path(__file__).resolve().parents[1] / "site"
needs_site = pytest.mark.skipif(not SITE.is_dir(), reason="site/ : seulement dans le dépôt privé")


def test_static_catalog_lists_every_item_with_its_install_steps():
    from gateway import marketplace_export

    cat = marketplace_export.static_catalog()
    tools = {t["id"]: t for t in cat["agent_tools"]}
    assert tools["rea"]["steps"] == installers.display("tool:rea") and tools["orca"]["size"] == "228 Mo"
    assert all(a["steps"] for a in cat["apps"])
    assert all(s["content"] and s["license_text"] for s in cat["skills"])
    assert all("action" not in c for c in cat["components"])


@needs_site
def test_site_marketplace_is_static_and_hands_the_key_only_to_the_chosen_pc():
    page = (SITE / "marketplace" / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in page and " style=" not in page  # CSP du site : ni script ni style en ligne
    assert '<script src="/assets/marketplace.js?v=' in page
    data = json.loads((SITE / "marketplace" / "catalog.json").read_text(encoding="utf-8"))
    assert {t["id"] for t in data["agent_tools"]} == {t["id"] for t in marketplace.AGENT_TOOLS}
    assert len(data["library"]["models"]) > 100 and json.loads(
        (SITE / "marketplace" / "licenses.json").read_text(encoding="utf-8")
    )
    js = (SITE / "assets" / "marketplace.js").read_text(encoding="utf-8")
    assert 'win.postMessage({ type: "lezard-market-key", key: key }, o)' in js  # seulement à l'adresse saisie
    assert "ev.source !== win || ev.origin !== o" in js
    assert 'window.open(target, "_blank")' in js
    # la clé ne part que vers le Lezard IA indiqué (état d'installation), jamais vers lezard-ia.fr
    assert js.count("fetch(o") == 1 and 'fetch(o + "/v1/marketplace/installed"' in js
    gw = (WEB / "static" / "marketplace.js").read_text(encoding="utf-8")
    assert 'const SITE_ORIGINS = ["https://lezard-ia.fr", "https://www.lezard-ia.fr"];' in gw
    assert "!SITE_ORIGINS.includes(e.origin) || e.source !== opener" in gw
    assert not (SITE / "_redirects").exists()


def test_orca_installs_from_its_pinned_and_verified_installer(monkeypatch):
    monkeypatch.setattr(installers.sys, "platform", "win32")  # installateur Windows : vérifié sur tout système
    monkeypatch.setattr(installers, "program", lambda name: sys.executable if name == installers.PYTHON else None)
    [argv] = installers.commands("tool:orca")
    assert argv[1].endswith("setup_download.py") and argv[2].startswith("https://github.com/stablyai/orca/releases/")
    assert argv[-1] == "/S" and len(argv[3]) == 64  # empreinte SHA-256 figée, installation silencieuse


def test_setup_download_refuses_other_sites_and_wrong_files(monkeypatch, tmp_path):
    from gateway import setup_download

    ran = []
    monkeypatch.setattr(setup_download.subprocess, "run", lambda *a, **k: ran.append(a))
    assert setup_download.main("https://exemple.com/x.exe", "0" * 64, "3") == 2

    class Resp:
        def __init__(self):
            self.chunks = [b"abc"]

        def read(self, n):
            return self.chunks.pop() if self.chunks else b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(setup_download.urllib.request, "urlopen", lambda *a, **k: Resp())
    assert setup_download.main("https://github.com/o/r/releases/download/v1/x.exe", "0" * 64, "3") == 3
    assert ran == []  # empreinte fausse : jamais lancé
    good = __import__("hashlib").sha256(b"abc").hexdigest()
    monkeypatch.setattr(
        setup_download.subprocess, "run", lambda argv, **k: ran.append(argv) or type("R", (), {"returncode": 0})()
    )
    assert setup_download.main("https://github.com/o/r/releases/download/v1/x.exe", good, "3", "/S") == 0
    assert ran[0][0].endswith("x.exe") and ran[0][1:] == ["/S"]


def test_star_refresh_sends_the_token_to_github_only_and_paces_its_reads(monkeypatch, tmp_path):
    import asyncio

    from gateway import marketplace_export

    seen, running, peak = [], 0, 0

    async def handler(request):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.path.endswith("/upstash/context7"):
            return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})
        return httpx.Response(200, json={"stargazers_count": 1234})

    monkeypatch.setattr(marketplace, "STARS_FILE", tmp_path / "stars.json")
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await marketplace_export.refresh_stars(client)

    snap = asyncio.run(go())
    assert seen and all(host == "api.github.com" and auth == "Bearer github_pat_test" for host, auth in seen)
    assert peak <= marketplace_export.GITHUB_PARALLEL  # pas de rafale : GitHub refuserait des lectures
    assert snap["repos"]["cline/cline"] == 1234 and "upstash/context7" not in snap["repos"]  # pas d'ancien relevé ici
    monkeypatch.delenv("GITHUB_TOKEN")
    assert marketplace_export.github_headers() == {}


def test_setup_download_unzips_into_programs_and_nowhere_else(monkeypatch, tmp_path):
    import zipfile

    from gateway import setup_download

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    good = tmp_path / "app.zip"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("dist-windows/Goose.exe", b"exe")
    assert setup_download.unzip(good, "Goose") == 0
    assert (tmp_path / "Programs" / "Goose" / "dist-windows" / "Goose.exe").read_bytes() == b"exe"
    assert setup_download.unzip(good, "..") == 2 and setup_download.unzip(good) == 2  # nom de dossier simple
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../../hors.txt", b"x")
    assert setup_download.unzip(bad, "Bad") == 3 and not (tmp_path / "hors.txt").exists()


def test_items_added_on_2026_11_02_install_pinned(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, "config_path", lambda: tmp_path / "mcp.json")
    chrome = marketplace.add_mcp("chromedevtools", "npx")
    assert chrome["args"] == ["-y", "chrome-devtools-mcp@1.11.0", "--no-usage-statistics"]  # rien envoyé à Google
    with pytest.raises(marketplace.MarketplaceError):
        marketplace.add_mcp("brave", "npx")  # clé Brave requise
    assert marketplace.add_mcp("brave", "npx", secret="BSA0123456789")["env"] == ["BRAVE_API_KEY"]
    goose = installers.RECIPES["app:goose"]["steps"][0]
    assert goose[2].startswith("https://github.com/aaif-goose/goose/releases/download/") and goose[-1] == "Goose"
    for key in ("app:anythingllm", "app:jan", "app:meetily"):
        argv = installers.RECIPES[key]["steps"][0]
        assert argv[2].startswith("https://github.com/") and len(argv[3]) == 64 and argv[-1] == "/S", key
    assert installers.PROVIDER["uvx"] == "uv" and installers.PROVIDER["git"] == "git"  # installés par winget
    assert [d["id"] for d in installers.possible_dependencies("tool:serena")] == ["uv", "claude"]
    assert {d["id"] for d in installers.possible_dependencies("app:librechat")} == {"git", "docker"}
    assert "claude-lezard" in {b["id"] for b in marketplace.BUNDLES}


def test_every_install_shows_size_and_place():
    for key, r in installers.RECIPES.items():
        assert r.get("where"), key
        if key.startswith(("tool:", "app:")):
            assert r.get("size"), key
    assert all(s.get("size") for s in marketplace.MCP_SERVERS)
    js = (WEB / "static" / "marketplace.js").read_text(encoding="utf-8")
    assert "Confirmer l'installation" in js and "confirm(`Install" not in js  # deux clics dans la page


def test_codegraph_agentmemory_and_openhuman_install_in_one_click(monkeypatch):
    # PC avec Node.js 24 : rien ne manque (la version de Node.js de la machine des tests ne compte pas)
    monkeypatch.setattr(installers, "program", lambda name: f"C:/bin/{name}.cmd")
    monkeypatch.setattr(installers, "_major", lambda exe: 24)
    assert installers.missing("tool:codegraph") == []  # codegraph : étape 1
    assert installers.RECIPES["tool:codegraph"]["steps"][1] == ("codegraph", "install", "--yes")
    am = installers.RECIPES["tool:agentmemory"]["steps"][0]
    assert am[-1] == "@agentmemory/mcp@0.9.30" and "node" in installers.RECIPES["tool:agentmemory"]["requires"]
    oh = installers.RECIPES["app:openhuman"]["steps"][0]
    assert oh[2].startswith("https://github.com/tinyhumansai/openhuman/releases/download/v0.64.15/") and oh[-1] == "/S"
    lic = {a["id"]: a["license"]["name"] for a in marketplace.APPS}
    assert lic["openhuman"] == "GPL-3.0"
    assert next(s for s in marketplace.MCP_SERVERS if s["id"] == "agentmemory")["version"] == "0.9.30"


def test_every_item_with_a_github_repo_shows_its_stars():
    snap = marketplace.stars_snapshot()
    assert snap["date"] and snap["repos"]
    entries = [*marketplace.MCP_SERVERS, *marketplace.APPS, *marketplace.AGENT_TOOLS, *skills.catalog()]
    for e in entries:
        s = marketplace.with_stars(e, snap)
        assert s["github"] and s["stars"] and s["stars_date"] == snap["date"], e["id"]
    assert marketplace.fmt_stars(76045) == "76 k" and marketplace.fmt_stars(413783) == "413,8 k"
    assert marketplace.fmt_stars(950) == "950"
    voix = marketplace.with_stars(marketplace.component("voix"), snap)
    assert voix["github"] == "SYSTRAN/faster-whisper"
    assert marketplace.with_stars(marketplace.component("web"), snap)["stars"] is None  # fonction de Lezard IA


@needs_site
def test_stars_reach_visitors_and_the_site(client):
    data = client.get("/v1/marketplace").json()
    assert all(t["stars"] for t in data["agent_tools"]) and all(s["stars"] for s in data["skills"])
    site = json.loads((SITE / "marketplace" / "catalog.json").read_text(encoding="utf-8"))
    assert all(a["stars"] and a["github"] for a in site["apps"]) and site["stars_date"]


@needs_site
def test_both_marketplaces_sort_by_github_stars():
    for page in (WEB / "marketplace.html", SITE / "marketplace" / "index.html"):
        html = page.read_text(encoding="utf-8")
        assert 'value="stars-desc"' in html and 'value="stars-asc"' in html and 'value="name-desc"' in html, page
    for js in (WEB / "static" / "marketplace.js", SITE / "assets" / "marketplace.js"):
        assert 'how === "stars-desc"' in js.read_text(encoding="utf-8"), js


@needs_site
def test_site_sort_menu_is_drawn_by_the_site():
    js = (SITE / "assets" / "marketplace.js").read_text(encoding="utf-8")
    assert 'customSelect($("sort"))' in js and 'setAttribute("role", "listbox")' in js
    css = (SITE / "assets" / "marketplace.css").read_text(encoding="utf-8")
    assert ".mk-select-list" in css and "var(--surface)" in css  # couleurs du thème, pas la liste du système


def test_site_reads_what_is_installed_with_cors_for_lezard_ia_only(client):
    site = {"Origin": "https://lezard-ia.fr"}
    pre = client.options(
        "/v1/marketplace/installed",
        headers={**site, "Access-Control-Request-Method": "GET", "Access-Control-Request-Private-Network": "true"},
    )
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "https://lezard-ia.fr"
    assert pre.headers["access-control-allow-headers"] == "authorization"
    assert pre.headers["access-control-allow-private-network"] == "true"  # site public -> ce PC
    other = client.options("/v1/marketplace/installed", headers={"Origin": "https://ailleurs.example"})
    assert "access-control-allow-origin" not in other.headers
    r = client.get("/v1/marketplace/installed", headers={**site, **MASTER})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "https://lezard-ia.fr"
    assert set(r.json()) == {"models", "skills", "components", "mcp", "tools", "apps", "pc"}
    bad = client.get("/v1/marketplace/installed", headers={**site, "Authorization": "Bearer faux"})
    assert (
        bad.status_code in (401, 403) and bad.headers["access-control-allow-origin"] == "https://lezard-ia.fr"
    )  # lisible
    assert client.get("/v1/marketplace/installed", headers={**site, **ACCOUNT}).status_code == 403


@needs_site
def test_site_theme_paints_the_phone_bar_and_hides_grey_scrollbars():
    theme = (SITE / "assets" / "theme.js").read_text(encoding="utf-8")
    assert 'meta[name="theme-color"]' in theme and "paintBar(next)" in theme
    css = (SITE / "assets" / "marketplace.css").read_text(encoding="utf-8")
    assert ".mk-tabs::-webkit-scrollbar { display: none; }" in css
    headers = (SITE / "_headers").read_text(encoding="utf-8")
    assert "connect-src 'self' https: http://localhost:* http://127.0.0.1:*" in headers


@needs_site
def test_site_updates_reach_phones_and_the_light_theme_stays_light():
    headers = (SITE / "_headers").read_text(encoding="utf-8")
    assert (
        "/assets/*\n  Cache-Control: public, max-age=0, must-revalidate" in headers
    )  # plus d'ancienne version d'une heure
    for page in [*SITE.glob("*.html"), SITE / "marketplace" / "index.html"]:
        html = page.read_text(encoding="utf-8")
        assert '<meta name="color-scheme" content="light dark">' in html and "/assets/site.css?v=" in html, page
    css = (SITE / "assets" / "site.css").read_text(encoding="utf-8")
    assert "color-scheme: only light;" in css  # le navigateur n'assombrit pas la page claire de lui-même
    assert 'mine.href = "/web.html"' in (SITE / "assets" / "site.js").read_text(encoding="utf-8")


@needs_site
def test_site_offers_to_use_lezard_ia_on_the_web():
    for name in ("index.html", "aide.html", "confidentialite.html", "mentions-legales.html", "404.html", "web.html"):
        assert 'class="btn btn-primary web-btn" href="/web.html"' in (SITE / name).read_text(encoding="utf-8"), name
    js = (SITE / "assets" / "web.js").read_text(encoding="utf-8")
    assert '"#key=" + encodeURIComponent(t.secret) : "#manuel"' in js  # fragment : jamais envoyé à un serveur
    # la clé ne part pas vers lezard-ia.fr : seul appel, « PC allumé ? », vers la machine, sans clé ni cookie
    assert js.count("fetch(") == 1 and 'fetch(t.origin + "/health", { mode: "no-cors", credentials: "omit"' in js
    login = (WEB / "static" / "login.js").read_text(encoding="utf-8")
    assert "/^#key=([^&]+)/.exec(location.hash)" in login and "history.replaceState" in login


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js absent")
def test_pc_login_page_sends_internet_visitors_to_the_account_page():
    """Ouverte depuis Internet sans clé, la page de connexion du PC renvoie vers lezard-ia.fr (connexion par
    compte) ; sur le PC, en Wi-Fi, par Tailscale, dans l'application ou avec « #manuel », le formulaire reste."""
    js = (WEB / "static" / "login.js").read_text(encoding="utf-8")
    assert 'ACCOUNT_PAGE = "https://lezard-ia.fr/web.html"' in js
    assert 'location.hash === "#manuel" || window.LezardApp' in js
    fn = re.search(r"(?s)export function publicAddress.*?\n}\n", js).group(0).replace("export ", "")
    hosts = {
        "ia.lezard-ia.fr": True,
        "abc.trycloudflare.com": True,
        "localhost": False,
        "127.0.0.1": False,
        "192.168.1.20": False,
        "10.0.0.5": False,
        "172.20.1.1": False,
        "100.101.1.2": False,
        "pc.tail1234.ts.net": False,
        "mon-pc": False,
        "mon-pc.local": False,
        "[::1]": False,
    }
    out = subprocess.run(
        ["node", "-e", fn + f"console.log(JSON.stringify({list(hosts)}.map(publicAddress)))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert json.loads(out) == list(hosts.values())


def test_pins_finds_every_pinned_version_and_waits_14_days():
    import asyncio

    from gateway import pins

    found = {(p.source, p.name): p for p in pins.collect()}
    assert found[("npm", "@upstash/context7-mcp")].version == "4.4.4"  # serveur MCP
    assert found[("pypi", "serena-agent")].version == "2.1.0"  # uv tool install pkg==version
    assert found[("pypi", "openai")].version  # plafond pip pkg<=version
    jan = found[("github", "janhq/jan")]
    assert jan.version == "v0.8.6" and jan.asset == "Jan_0.8.6_x64-setup.exe"
    assert ("github", "LibreChat-AI/LibreChat") in found and ("github", "open-webui/open-webui") in found
    assert len({p.source for p in found.values()}) == 3

    def handler(request):
        if request.url.host == "registry.npmjs.org":
            times = {"created": "", "modified": "", "4.4.4": "2026-10-01", "4.5.0": "2026-10-10"}
            times |= {"4.6.0": "2026-10-30", "5.0.0-beta.1": "2026-10-05"}  # trop récente ; préversion
            return httpx.Response(200, json={"time": times, "versions": {}})
        if request.url.host == "pypi.org":
            return httpx.Response(200, json={"releases": {"2.1.0": [{"upload_time": "2026-10-01"}]}})
        assets = [{"name": "Jan_0.8.7_x64-setup.exe", "browser_download_url": "https://github.com/x", "size": 5}]
        assets[0]["digest"] = "sha256:" + "a" * 64
        rel = [{"tag_name": "v0.8.7", "published_at": "2026-10-12T00:00:00Z", "assets": assets}]
        return httpx.Response(
            200, json=rel + [{"tag_name": "v0.9.0", "published_at": "2026-11-01", "prerelease": True}]
        )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await pins.check(client, today="2026-11-02")

    rows = {(r["pin"].source, r["pin"].name): r for r in asyncio.run(go())}
    ctx = rows[("npm", "@upstash/context7-mcp")]
    assert ctx["update"] and ctx["latest"] == "4.5.0"  # 4.6.0 : moins de 14 jours ; 5.0.0-beta : préversion
    assert not rows[("pypi", "serena-agent")]["update"]
    j = rows[("github", "janhq/jan")]
    assert j["latest"] == "v0.8.7" and j["asset"] == {"url": "https://github.com/x", "sha256": "a" * 64, "size": 5}


def test_web_searches_are_compared_in_their_details(client):
    rows = client.get("/v1/marketplace").json()["web_search"]
    keys = {r["key"] for r in rows}
    assert {
        "component:web",
        "mcp:brave",
        "mcp:tavily",
        "mcp:exa",
        "mcp:firecrawl",
        "mcp:searxng",
        "mcp:perplexity",
    } == keys
    assert all(r["name"] and r["cost"] and r["sends"] and r["best"] for r in rows)
    assert "DuckDuckGo" in next(r for r in rows if r["key"] == "component:web")["sends"]  # la recherche intégrée
    for js in (WEB / "static" / "marketplace.js", SITE / "assets" / "marketplace.js"):
        if js.exists():  # site/ : seulement dans le dépôt privé
            assert js.read_text(encoding="utf-8").count("webSearchCompare(it, body);") == 2, js  # outil intégré et MCP


def test_librechat_installs_pinned_with_its_own_secrets(tmp_path, monkeypatch):
    from gateway import librechat_setup

    steps = installers.RECIPES["app:librechat"]["steps"]
    assert steps[0][1].endswith("librechat_setup.py") and steps[0][2] == installers.LIBRECHAT_TAG
    assert steps[1] == ("docker", "compose", "--project-directory", "LibreChat", "up", "--detach")
    assert installers.uninstall_steps("app:librechat") == [
        ("docker", "compose", "--project-directory", "LibreChat", "down")
    ]  # dossier et données gardés
    example = "PORT=3080\nCREDS_KEY=\nCREDS_IV=\nJWT_SECRET=\nJWT_REFRESH_SECRET=\nMEILI_MASTER_KEY=\n# UID=1000\n"
    root = tmp_path / "LibreChat"
    root.mkdir()
    (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")  # déjà téléchargé : pas de git
    (root / ".env.example").write_text(example, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(librechat_setup.subprocess, "run", lambda *a, **k: pytest.fail("git relancé"))
    assert librechat_setup.main("v0.8.8", "LibreChat") == 0
    env = (root / ".env").read_text(encoding="utf-8")
    assert re.search(r"^CREDS_KEY=[0-9a-f]{64}$", env, re.M) and re.search(r"^CREDS_IV=[0-9a-f]{32}$", env, re.M)
    assert re.search(r"^ADMIN_PANEL_SESSION_SECRET=[0-9a-f]{64}$", env, re.M) and "\nUID=1000\n" in env
    override = (root / "docker-compose.override.yml").read_text(encoding="utf-8")
    assert "librechat:v0.8.8" in override and "latest" not in override and 'profiles: ["admin"]' in override
    assert librechat_setup.main("v0.8.8", "LibreChat") == 0
    assert (root / ".env").read_text(encoding="utf-8") == env  # réinstaller garde les secrets (données chiffrées)
    assert librechat_setup.main("main", "LibreChat") == 2 and librechat_setup.main("v0.8.8", "../x") == 2


def test_link_check_covers_every_pinned_download():
    from gateway import pins

    found = pins.downloads()
    kinds = {d["kind"] for d in found}
    assert kinds == {"npm", "pypi", "tag", "asset", "image", "vscode", "winget", "page"}
    assets = {d["name"]: d for d in found if d["kind"] == "asset"}
    assert set(assets) == {
        "tool:orca",
        "app:openhuman",
        "app:anythingllm",
        "app:jan",
        "app:meetily",
        "app:goose",
        "app:cherrystudio",
    }
    assert all(len(d["sha256"]) == 64 and d["size"] > 0 for d in assets.values())  # empreinte et taille comparées
    images = {d["name"] for d in found if d["kind"] == "image"}
    assert f"ghcr.io/librechat-ai/librechat:{installers.LIBRECHAT_TAG}" in images and all(
        "latest" not in i for i in images
    )
    assert {"npm", "@modelcontextprotocol/server-memory"} <= {v for d in found for v in (d["kind"], d["name"])}
    assert marketplace.MCP_VERSION == "2.0.0"  # versions datées (2026.8.31) dépréciées sur npm


def test_python_sdks_install_for_the_users_own_python():
    """Le Python de Lezard IA installé (installateur, zip) est embarqué, sans pip : le SDK va au Python des scripts."""
    for key in ("app:python", "app:langchain"):
        argv = installers.RECIPES[key]["steps"][0]
        assert argv[: len(installers.USER_PYTHON)] == installers.USER_PYTHON and "--user" in argv, key
        assert (
            installers.PYTHON not in argv and installers.uninstall_steps(key) is None
        )  # paquets partagés : pas retirés
    if sys.platform == "win32":
        assert [d["id"] for d in installers.possible_dependencies("app:python")] == ["py"]  # Python installé par winget


def test_mcp_servers_get_fixed_settings_and_a_folder_by_variable(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, "config_path", lambda: tmp_path / "mcp.json")
    n8n = marketplace.add_mcp("n8n", "npx")
    assert "N8N_MCP_TELEMETRY_DISABLED" in n8n["env"]  # télémétrie (active par défaut) coupée
    saved = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))["servers"]
    assert saved["n8n"]["env"]["N8N_MCP_TELEMETRY_DISABLED"] == "true"
    git = marketplace.add_mcp("git", "npx", folder=str(tmp_path))
    assert git["args"] == ["-y", "@cyanheads/git-mcp-server@2.15.3"]  # dossier par GIT_BASE_DIR, pas en argument
    assert json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))["servers"]["git"]["env"] == {
        "MCP_TRANSPORT_TYPE": "stdio",
        "GIT_BASE_DIR": str(tmp_path),
    }
    with pytest.raises(marketplace.MarketplaceError):
        marketplace.add_mcp("git", "npx")  # dossier requis
    figma = marketplace.add_mcp("figma", "npx", secret="figd_0123456789")
    assert figma["args"][-1] == "--stdio" and figma["env"] == ["FIGMA_API_KEY"]
    assert installers.uninstall_steps("tool:markitdown") == [("uv", "tool", "uninstall", "markitdown")]  # sans [all]
