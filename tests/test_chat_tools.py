"""Outils de la marketplace dans le chat : « /<commande> » pour chaque outil installé (/openclaw, /aider, /gws,
/jan…). Clé maître seulement ; programmes lancés sans interpréteur de commandes ; clé jamais écrite sur le disque ;
outils branchés sur Lezard IA sans réglage ; un dossier de travail par conversation ; arrêt possible."""

import json
import re
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import chat_tools, marketplace
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

from .test_permissions import MASTER, Upstream

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "gateway" / "web" / "static"
SESSION = "abc123def456"

# faux outil : note ce qu'il a reçu (arguments, variables, dossier), répond comme le vrai
FAKE = r"""
import json, os, sys, time
args = sys.argv[1:]
if "dors" in args:
    time.sleep(60)
seen = {"args": args, "cwd": os.getcwd(), "env": {k: os.environ.get(k) for k in (
    "LEZARD_API_KEY", "OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENCODE_CONFIG",
    "OPENCLAW_CONFIG_PATH", "ANTHROPIC_API_KEY")}}
json.dump(seen, open(os.path.join(os.environ["FAKE_LOG"], "recu.json"), "w"))
tool = os.environ["FAKE_TOOL"]
if tool == "openclaw":
    msg = open(args[args.index("--message-file") + 1], encoding="utf-8").read()
    print(json.dumps({"payloads": [{"text": "OpenClaw : " + msg}], "meta": {"executionTrace": {"winnerModel": "m"}}}))
elif tool == "aider":
    msg = open(args[args.index("--message-file") + 1], encoding="utf-8").read()
    print("Aider v0.90\nModel: openai/auto\n\x1b[32mAider : " + msg + "\x1b[0m")
elif tool == "cli":
    print("sortie de " + " ".join(args))
    print("attention", file=sys.stderr)
    sys.exit(2)
else:
    print(tool + " : " + (args[args.index("-p") + 1] if tool == "claude" else args[-1].removeprefix("--prompt=")))
"""


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GATEWAY_PORT", "8123")
    monkeypatch.setenv("GATEWAY_TRUST_PROXY", "true")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cle-api-de-lezard")  # jamais donnée à Claude Code
    fake = tmp_path / "faux_outil.py"
    fake.write_text(FAKE, encoding="utf-8")
    monkeypatch.setattr(chat_tools, "program", lambda name: [sys.executable, str(fake)])
    monkeypatch.setattr(chat_tools.detection, "scan", lambda: {k: True for k in chat_tools.installers.RECIPES})
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(Upstream())),
        plans=PlanResolver({}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as c:

        def ask(tool, message, fake_as=None, headers=MASTER, **extra):
            monkeypatch.setenv("FAKE_TOOL", fake_as or tool)
            r = c.post(f"/v1/apps/{tool}/runs", headers=headers, json={"message": message, "session": SESSION, **extra})
            if r.status_code != 200:
                return r
            run = r.json()
            for _ in range(300):
                if run["status"] != "running":
                    break
                time.sleep(0.05)
                run = c.get(f"/v1/apps/runs/{run['id']}", headers=headers).json()
            return run

        def got():
            return json.loads((tmp_path / "recu.json").read_text(encoding="utf-8"))

        yield c, ask, got, tmp_path / "data"


def test_every_installed_tool_is_in_the_chat_menu(api):
    c, *_ = api
    data = c.get("/v1/apps", headers=MASTER).json()["data"]
    assert {a["id"] for a in data} == set(marketplace.CHAT)
    aider = next(a for a in data if a["id"] == "aider")
    assert aider["command"] == "aider" and aider["use"] == "/aider <demande>" and aider["kind"] == "agent"
    assert next(a for a in data if a["id"] == "hyperframes-cli")["command"] == "hyperframescli"  # lettres seulement


def test_agents_are_wired_to_lezard_ia(api):
    c, ask, got, data = api
    run = ask("openclaw", "range mon bureau & del C:\\* | %PATH%")
    assert run["status"] == "done" and run["text"] == "OpenClaw : range mon bureau & del C:\\* | %PATH%"
    seen = got()
    assert seen["env"]["LEZARD_API_KEY"] == "master" and "--message-file" in seen["args"]
    assert "master" not in (data / "openclaw" / "openclaw.json").read_text(encoding="utf-8")  # jamais sur le disque
    run = ask("aider", "écris un script", model="ollama/gemma4:e4b")
    assert run["text"] == "Aider : écris un script" and run["model"] == "ollama/gemma4:e4b"  # en-têtes et couleurs ôtés
    seen = got()
    assert seen["env"]["OPENAI_API_BASE"] == "http://127.0.0.1:8123/v1" and seen["env"]["OPENAI_API_KEY"] == "master"
    assert seen["args"][seen["args"].index("--model") + 1] == "openai/ollama/gemma4:e4b" and "--no-git" in seen["args"]
    assert Path(seen["cwd"]).name == SESSION  # un dossier par conversation
    run = ask("opencode", "crée une page")
    seen = got()
    assert seen["args"][-2:] == ["--", "crée une page"] and "--continue" not in seen["args"]
    config = json.loads(Path(seen["env"]["OPENCODE_CONFIG"]).read_text(encoding="utf-8"))
    assert config["provider"]["lezard"]["options"] == {
        "baseURL": "http://127.0.0.1:8123/v1",
        "apiKey": "{env:LEZARD_API_KEY}",
    }
    ask("opencode", "et ensuite ?")
    assert "--continue" in got()["args"]  # la suite de la même conversation
    run = ask("qwencode", "explique")
    seen = got()
    assert run["text"] == "qwencode : explique" and seen["env"]["OPENAI_BASE_URL"] == "http://127.0.0.1:8123/v1"
    assert "master" not in seen["args"]  # la clé passe par une variable, jamais par la ligne de commande


def test_claude_code_extensions_use_the_pc_account(api):
    c, ask, got, _ = api
    run = ask("hyperframes", "une intro de 10 secondes", fake_as="claude")
    seen = got()
    assert run["text"] == "claude : /hyperframes:hyperframes une intro de 10 secondes" and run["model"] is None
    assert seen["env"]["ANTHROPIC_API_KEY"] is None and seen["env"]["LEZARD_API_KEY"] is None
    assert seen["args"][seen["args"].index("--permission-mode") + 1] == "acceptEdits"


def test_command_line_tools_show_their_output(api):
    c, ask, got, _ = api
    run = ask("gws", 'calendar +agenda --today "mon agenda"', fake_as="cli")
    assert got()["args"] == ["calendar", "+agenda", "--today", "mon agenda"]  # mots, guillemets ôtés
    assert run["status"] == "done" and run["text"].startswith("```text\nsortie de calendar")
    assert "attention" in run["text"] and run["text"].endswith("Code de sortie : 2.")
    ask("findskills", "find pdf", fake_as="cli")
    assert got()["args"] == ["--yes", "skills", "find", "pdf"]
    run = ask("gws", 'non "fermé', fake_as="cli")
    assert run["status"] == "error" and "Guillemet" in run["error"]


def test_apps_open_on_this_pc(api, monkeypatch):
    c, ask, _, _ = api
    opened = []
    monkeypatch.setattr(chat_tools, "_windows_open", lambda spec: opened.append(spec["start"]) or spec["start"])
    monkeypatch.setattr(chat_tools.webbrowser, "open", opened.append)
    monkeypatch.setattr(chat_tools.installers, "program", lambda name: None)  # pas de Docker
    assert ask("jan", "")["text"] == "Jan est ouvert sur ce PC."
    assert "http://localhost:3000" in ask("openwebui", "")["text"]
    assert opened == ["Jan", "http://localhost:3000"]


def test_a_request_can_be_stopped_and_checks(api, monkeypatch):
    c, ask, _, _ = api
    monkeypatch.setenv("FAKE_TOOL", "cli")
    run = c.post("/v1/apps/gws/runs", headers=MASTER, json={"message": "dors", "session": SESSION}).json()
    time.sleep(0.5)
    assert c.delete(f"/v1/apps/runs/{run['id']}", headers=MASTER).status_code == 200
    for _ in range(100):
        run = c.get(f"/v1/apps/runs/{run['id']}", headers=MASTER).json()
        if run["status"] != "running":
            break
        time.sleep(0.05)
    assert run["status"] == "stopped"
    assert ask("aider", "").status_code == 400  # un agent attend une demande
    assert ask("aider", "x", session="../..").status_code == 400
    assert ask("inconnu", "x").status_code == 404


def test_master_key_only(api):
    c, ask, _, _ = api
    k = c.post("/v1/keys", headers=MASTER, json={"name": "appli"}).json()["key"]
    other = {"Authorization": f"Bearer {k}"}
    assert c.get("/v1/apps", headers=other).json() == {"data": []}  # rien dans son menu
    assert ask("aider", "x", headers=other).status_code == 403  # ces outils agissent sur le PC
    run = ask("aider", "x")
    assert c.get(f"/v1/apps/runs/{run['id']}", headers=other).status_code == 403


def test_not_installed(api, monkeypatch):
    c, ask, _, _ = api
    monkeypatch.setattr(chat_tools, "program", lambda name: None)
    monkeypatch.setattr(chat_tools.detection, "scan", lambda: {})
    assert c.get("/v1/apps", headers=MASTER).json() == {"data": []}
    r = ask("aider", "x")
    assert r.status_code == 409 and "marketplace" in r.json()["error"]["message"]


@pytest.mark.skipif(sys.platform != "win32", reason="lanceurs .cmd de npm sous Windows")
def test_npm_launchers_run_node_directly(tmp_path, monkeypatch):
    """Jamais par cmd.exe (il interpréterait des caractères de la demande) : Node avec le script du lanceur."""
    (tmp_path / "node.exe").write_bytes(b"")
    (tmp_path / "node_modules" / "openclaw").mkdir(parents=True)
    (tmp_path / "node_modules" / "openclaw" / "openclaw.mjs").write_text("", encoding="utf-8")
    (tmp_path / "openclaw.cmd").write_text(
        '@ECHO off\r\nendLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
        '"%dp0%\\node_modules\\openclaw\\openclaw.mjs" %*\r\n',
        encoding="utf-8",
    )
    (tmp_path / "node_modules" / "npm" / "bin").mkdir(parents=True)
    (tmp_path / "node_modules" / "npm" / "bin" / "npx-cli.js").write_text("", encoding="utf-8")
    (tmp_path / "npx.cmd").write_text(
        '@ECHO OFF\r\nSET "NODE_EXE=%~dp0\\node.exe"\r\nSET "NPX_CLI_JS=%~dp0\\node_modules\\npm\\bin\\npx-cli.js"\r\n'
        'IF EXIST "%X%" (\r\n  SET "NPX_CLI_JS=%NPM_PREFIX_NPX_CLI_JS%"\r\n)\r\n'  # posée deux fois, comme le vrai
        '"%NODE_EXE%" "%NPX_CLI_JS%" %*\r\n',
        encoding="utf-8",
    )
    (tmp_path / "bizarre.cmd").write_text("@echo off\r\nfoo.exe %*\r\n", encoding="utf-8")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "opencode.exe").write_bytes(b"")
    (tmp_path / "opencode.cmd").write_text('CALL :find_dp0\r\n"%dp0%\\bin\\opencode.exe"   %*\r\n', encoding="utf-8")
    monkeypatch.setattr(chat_tools.installers, "program", lambda name: str(tmp_path / f"{name}.cmd"))
    node = str(tmp_path / "node.exe")
    assert chat_tools.program("openclaw") == [node, str(tmp_path / "node_modules" / "openclaw" / "openclaw.mjs")]
    assert chat_tools.program("npx") == [node, str(tmp_path / "node_modules" / "npm" / "bin" / "npx-cli.js")]
    assert chat_tools.program("opencode") == [str(tmp_path / "bin" / "opencode.exe")]  # programme lancé tel quel
    assert chat_tools.program("bizarre") is None  # jamais lancé par cmd.exe


def test_every_command_is_a_chat_command_on_the_fiches():
    for eid in marketplace.CHAT:
        assert re.fullmatch(r"[a-z]+", marketplace.chat_command(eid)), eid
        entry = next(
            e for e in (*marketplace.APPS, *marketplace.AGENT_TOOLS, *marketplace.COMPONENTS) if e["id"] == eid
        )
        first = entry["commands"][0]
        assert first["where"] == marketplace.WHERE["chat"] and first["use"].startswith(
            f"/{marketplace.chat_command(eid)}"
        )
    builtin = set(re.findall(r'\{ name: "(\w+)"', (STATIC / "slash-commands.js").read_text(encoding="utf-8")))
    assert not builtin & {marketplace.chat_command(e) for e in marketplace.CHAT}  # aucune ne cache une commande


def test_chat_page_uses_the_tools():
    js = (STATIC / "slash-commands.js").read_text(encoding="utf-8")
    assert 'rawApi("/v1/apps")' in js and "export function appRequest" in js
    chat = (STATIC / "chat.js").read_text(encoding="utf-8")
    assert "appRequest(textOf(asked?.content))" in chat and "/v1/apps/runs/" in chat and 'method: "DELETE"' in chat
