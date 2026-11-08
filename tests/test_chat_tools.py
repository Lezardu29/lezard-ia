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
    "OPENCLAW_CONFIG_PATH", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")}}
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


def test_claude_code_extensions_run_only_in_lezard_ia(api):
    """Superpowers, HyperFrames… : Claude Code de ce PC, mais son IA est celle de Lezard IA (modèle choisi dans le
    chat), jamais le compte claude.ai de ce PC ni la clé d'API d'Anthropic du .env."""
    c, ask, got, _ = api
    run = ask("hyperframes", "une intro de 10 secondes", fake_as="claude", model="ollama/gemma4:e4b")
    seen = got()
    assert run["text"] == "claude : /hyperframes:hyperframes une intro de 10 secondes"
    assert run["model"] == "ollama/gemma4:e4b"
    env = seen["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8123" and env["ANTHROPIC_AUTH_TOKEN"] == "master"
    assert env["ANTHROPIC_API_KEY"] is None and env["LEZARD_API_KEY"] is None  # la clé du .env : jamais
    assert env["ANTHROPIC_MODEL"] == env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == env["CLAUDE_CODE_SUBAGENT_MODEL"]
    assert seen["args"][seen["args"].index("--model") + 1] == "ollama/gemma4:e4b"
    assert seen["args"][seen["args"].index("--permission-mode") + 1] == "acceptEdits"
    assert seen["args"][seen["args"].index("--tools") + 1] == "Read,Write,Edit,Glob,Grep,Skill"  # tient en local
    assert "--strict-mcp-config" in seen["args"]
    ask("superpowers", "faisons une liste de tâches", fake_as="claude")
    assert got()["env"]["ANTHROPIC_MODEL"] == "auto"  # sans modèle choisi : le choix automatique de Lezard IA


def test_superpowers_adapts_to_a_local_ai_or_to_claude(api, monkeypatch):
    """IA locale : outils réduits, et la méthode de Superpowers qui convient donnée d'office (un petit modèle n'irait
    pas la chercher). Clé d'API d'Anthropic enregistrée dans Lezard IA : Claude, l'extension telle qu'écrite."""
    c, ask, got, _ = api
    ask("superpowers", "Je veux construire une appli de liste de courses", fake_as="claude", model="ollama/gemma4:e4b")
    args = got()["args"]
    guide = args[args.index("--append-system-prompt") + 1]
    assert "Cadrer avant de construire" in guide and args[args.index("--tools") + 1] == chat_tools.CLAUDE_TOOLS
    ask("superpowers", "mon script plante avec une erreur", fake_as="claude", model="ollama/gemma4:e4b")
    assert "Débogage" in got()["args"][got()["args"].index("--append-system-prompt") + 1]
    ask("hyperframes", "une intro", fake_as="claude", model="ollama/gemma4:e4b")
    args = got()["args"]  # seulement la langue de la demande : pas de méthode de Superpowers
    assert args[args.index("--append-system-prompt") + 1] == chat_tools.LANGUAGE

    monkeypatch.setattr(chat_tools, "claude_model_of", lambda providers: "anthropic/claude-sonnet-5-5")
    run = ask("superpowers", "Je veux construire une appli", fake_as="claude")  # aucun modèle choisi : Claude
    seen = got()
    assert (
        run["model"] == "anthropic/claude-sonnet-5-5"
        and seen["env"]["ANTHROPIC_MODEL"] == "anthropic/claude-sonnet-5-5"
    )
    assert seen["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8123"  # toujours par Lezard IA (sa clé, ses comptes)
    assert seen["args"][seen["args"].index("--tools") + 1] == chat_tools.CLAUDE_FULL
    assert seen["args"][seen["args"].index("--append-system-prompt") + 1] == chat_tools.LANGUAGE  # pas de méthode
    ask("superpowers", "Je veux construire une appli", fake_as="claude", model="ollama/gemma4:e4b")
    assert got()["env"]["ANTHROPIC_MODEL"] == "ollama/gemma4:e4b"  # le modèle choisi dans le chat prime


def test_claude_model_only_with_an_anthropic_key():
    from gateway.config import ProviderConfig

    class P:
        def __init__(self, cfg):
            self.cfg = cfg

    assert chat_tools.claude_model_of({"ollama": P(ProviderConfig("ollama", "openai", "http://o/v1", "local"))}) is None
    keyless = ProviderConfig("anthropic", "anthropic", "https://api.anthropic.com/v1", "cloud", api_key_env="ABSENTE_X")
    assert chat_tools.claude_model_of({"anthropic": P(keyless)}) is None
    keyed = ProviderConfig("anthropic", "anthropic", "https://api.anthropic.com/v1", "cloud", api_key="sk-ant-x")
    assert chat_tools.claude_model_of({"anthropic": P(keyed)}) == "anthropic/claude-sonnet-5-5"
    assert chat_tools.is_claude("anthropic/claude-opus-5-5") and not chat_tools.is_claude("ollama/qwen3:8b")


def test_system_messages_in_the_conversation_are_accepted():
    """Claude Code récent met des messages « system » dans la conversation : ramenés aux instructions système."""
    from gateway.validation import validate_messages_body

    body = {
        "system": [{"type": "text", "text": "Tu es Claude Code."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Dis OK"}]},
            {"role": "system", "content": [{"type": "text", "text": "Rappel : réponds court.", "cache_control": {}}]},
        ],
    }
    validate_messages_body(body)
    assert [m["role"] for m in body["messages"]] == ["user"]
    assert [b["text"] for b in body["system"]] == ["Tu es Claude Code.", "Rappel : réponds court."]
    plain = {
        "system": "Instructions",
        "messages": [{"role": "system", "content": "Note"}, {"role": "user", "content": "x"}],
    }
    validate_messages_body(plain)
    assert plain["system"] == [{"type": "text", "text": "Instructions"}, {"type": "text", "text": "Note"}]


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
    monkeypatch.setattr(chat_tools.installers, "program", lambda name: None)
    assert ask("jan", "")["text"] == "Jan est ouvert sur ce PC."
    assert ask("librechat", "")["text"].startswith("Ouvert dans le navigateur de ce PC : http://localhost:3080")
    assert opened == ["Jan", "http://localhost:3080"]


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


def test_every_agent_uses_claude_when_a_key_is_saved(api, monkeypatch):
    """Aider, OpenCode, Qwen Code, OpenClaw : sans modèle choisi dans le chat, Claude si une clé d'Anthropic est
    enregistrée dans Lezard IA (toujours par Lezard IA)."""
    c, ask, got, _ = api
    monkeypatch.setattr(chat_tools, "claude_model_of", lambda providers: "anthropic/claude-sonnet-5-5")
    run = ask("aider", "écris un script")
    seen = got()
    assert run["model"] == "anthropic/claude-sonnet-5-5"
    assert seen["args"][seen["args"].index("--model") + 1] == "openai/anthropic/claude-sonnet-5-5"
    assert seen["env"]["OPENAI_API_BASE"] == "http://127.0.0.1:8123/v1"
    ask("qwencode", "explique")
    assert got()["env"]["OPENAI_MODEL"] == "anthropic/claude-sonnet-5-5"
    ask("aider", "x", model="ollama/gemma4:e4b")
    assert got()["args"][got()["args"].index("--model") + 1] == "openai/ollama/gemma4:e4b"  # le choix du chat prime


def fake_plugin(home, plugin: str, skills: dict[str, str]):
    """Extension de Claude Code installée sous `home`, avec ses méthodes (skills/<nom>/SKILL.md)."""
    root = home / ".claude" / "plugins" / "cache" / plugin.replace("@", "/") / "1.0"
    for name, desc in skills.items():
        (root / "skills" / name).mkdir(parents=True)
        (root / "skills" / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\nÉtapes de la méthode {name}.", encoding="utf-8"
        )
    index = home / ".claude" / "plugins" / "installed_plugins.json"
    data = json.loads(index.read_text("utf-8")) if index.exists() else {"plugins": {}}
    data["plugins"][plugin] = [{"scope": "user", "installPath": str(root)}]
    index.write_text(json.dumps(data), encoding="utf-8")


def test_extensions_give_a_local_ai_the_right_method(api, tmp_path, monkeypatch):
    """IA locale : la méthode de l'extension qui convient, choisie par le modèle en une courte requête, est donnée
    dans ses instructions ; jamais le mode d'emploi général (« using-… »)."""
    import asyncio

    monkeypatch.setattr(chat_tools.Path, "home", lambda: tmp_path)
    fake_plugin(
        tmp_path,
        "debugging-toolkit@claude-code-workflows",
        {"debug-hunt": "Use when a program crashes", "code-review": "Use to review code", "using-tools": "Intro"},
    )
    assert [s["name"] for s in chat_tools.plugin_skills("wshobson")] == ["code-review", "debug-hunt", "using-tools"]

    asked = []

    class Model:
        def __init__(self, reply):
            self.reply = reply

        def __call__(self, request):
            asked.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": self.reply}]})

    real = httpx.AsyncClient

    def guide(reply, message="mon programme plante"):
        transport = httpx.MockTransport(Model(reply))
        monkeypatch.setattr(chat_tools.httpx, "AsyncClient", lambda **kw: real(transport=transport))
        return asyncio.run(chat_tools.plugin_guide("wshobson", message, 8123, "master", "ollama/gemma4:e4b"))

    g = guide({"content": "debug-hunt"})
    assert "Méthode « debug-hunt »" in g and "Étapes de la méthode debug-hunt." in g and "---" not in g
    assert "using-tools" not in asked[-1]["messages"][1]["content"]  # mode d'emploi général : jamais proposé
    g = guide({"content": "", "reasoning": "code-review ? non, plutôt debug-hunt"})  # a tout dit en réfléchissant
    assert "Méthode « debug-hunt »" in g
    assert guide({"content": "aucune"}, "quelle heure est-il ?") == ""
    assert asyncio.run(chat_tools.plugin_guide("hyperframes", "x", 8123, "k", "m")) == ""  # pas installée


def test_mcp_extensions_keep_their_own_server(tmp_path, monkeypatch):
    """Avec une IA locale, Claude Code ne reçoit aucun serveur MCP… sauf celui de l'extension (agentmemory, Serena)."""
    monkeypatch.setattr(chat_tools.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("GATEWAY_DATA_DIR", str(tmp_path / "data"))
    assert chat_tools.mcp_args("watch", {}) == ["--strict-mcp-config"]
    assert chat_tools.mcp_args("serena", {"mcp": "serena"}) == []  # pas déclaré : ceux de Claude Code, l'outil marche
    servers = {"serena": {"command": "serena"}, "autre": {"command": "x"}, "mon-agentmemory": {"command": "a"}}
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    args = chat_tools.mcp_args("serena", {"mcp": "serena"})
    assert args[0] == "--mcp-config" and args[2:] == ["--strict-mcp-config", "--allowedTools", "mcp__serena"]
    assert json.loads(Path(args[1]).read_text("utf-8")) == {"mcpServers": {"serena": {"command": "serena", "env": {}}}}
    args = chat_tools.mcp_args("agentmemory", {"mcp": "agentmemory"})  # nom un peu différent : retrouvé quand même
    assert list(json.loads(Path(args[1]).read_text("utf-8"))["mcpServers"]) == ["mon-agentmemory"]
    assert marketplace.CHAT["serena"]["mcp"] == "serena" and marketplace.CHAT["agentmemory"]["mcp"] == "agentmemory"
