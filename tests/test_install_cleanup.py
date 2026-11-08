"""Banc d'essai de la marketplace (2026-11-08) : ce qu'il a fait corriger, vérifié sans rien installer.

Relais MCP pour les IA locales (mcp_filter.py), installations rejouables et désinstallations sans restes
(installers.py, setup_download.py, user_path.py, claude_files.py), dossiers nommés dans une demande (chat_tools.py),
claude-mem branché sur Lezard IA (app_connect.py)."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from gateway import app_connect, chat_tools, claude_files, installers, marketplace, setup_download, user_path
from gateway.store import DocumentStore

FILTER = Path(installers.__file__).with_name("mcp_filter.py")


# ---------- relais MCP (mcp_filter.py) ----------
FAKE_SERVER = textwrap.dedent(
    """
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("method") == "tools/list":
            tools = [{"name": n} for n in ("open_binary", "procedure_pseudo_code", "autre")]
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools}}
        elif msg.get("method") == "tools/call":
            args = msg["params"]["arguments"]
            words = args.get("query", "").split()
            found = [] if len(words) > 1 or args.get("query") == "rien" else ["souvenir"]
            body = {"args": args, "results": found}
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": json.dumps(body)}]}}
        else:
            continue
        print(json.dumps(out), flush=True)
    """
)


def relay(tmp_path, options, messages):
    server = tmp_path / "serveur.py"
    server.write_text(FAKE_SERVER, encoding="utf-8")
    argv = [sys.executable, str(FILTER), *options, "--", sys.executable, str(server)]
    lines = "".join(json.dumps(m) + "\n" for m in messages)
    out = subprocess.run(argv, input=lines, capture_output=True, text=True, encoding="utf-8", timeout=60).stdout
    return {m["id"]: m for m in map(json.loads, out.splitlines())}


def test_relay_shows_only_some_tools_and_refuses_the_others(tmp_path):
    got = relay(
        tmp_path,
        ["--only", "open_binary,procedure_pseudo_code", "--drop", "document"],
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "autre", "arguments": {}}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "open_binary", "arguments": {"document": r"C:\a.exe", "path": "x"}},
            },
        ],
    )
    assert [t["name"] for t in got[1]["result"]["tools"]] == ["open_binary", "procedure_pseudo_code"]
    assert got[2]["error"]["message"] == "Outil non proposé."  # jamais transmis au serveur
    assert json.loads(got[3]["result"]["content"][0]["text"])["args"] == {"path": "x"}  # « document » retiré


def test_relay_retries_an_empty_search_word_by_word(tmp_path):
    ask = {"name": "memory_recall", "arguments": {"query": "couleur page accueil"}}
    got = relay(
        tmp_path,
        ["--any-word", "memory_recall"],
        [
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": ask},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "memory_recall", "arguments": {"query": "rien"}}},
        ],
    )  # fmt: skip
    first = json.loads(got[7]["result"]["content"][0]["text"])
    assert first == {"args": {"query": "couleur"}, "results": ["souvenir"]}  # un seul mot : trouvé
    assert json.loads(got[8]["result"]["content"][0]["text"])["results"] == []  # un mot : rien à refaire


def test_local_ai_gets_the_relay_claude_does_not(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_tools.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("GATEWAY_DATA_DIR", str(tmp_path / "data"))
    rea = {"command": "cmd.exe", "args": ["/d", "/c", "npx", "-y", "rea-agents@6.7.0", "mcp"]}
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"rea": rea}}), encoding="utf-8")
    spec = marketplace.CHAT["rea"]
    args = chat_tools.mcp_args("rea", spec, local=True)
    cfg = json.loads(Path(args[1]).read_text("utf-8"))["mcpServers"]["rea"]
    assert cfg["command"] == sys.executable and cfg["args"][0].endswith(("mcp_filter.py", "mcp_filter.pyc"))
    assert cfg["args"][cfg["args"].index("--") + 1 :] == [rea["command"], *rea["args"]]
    assert cfg["env"]["REA_MCP_INPUT_SCHEMA_PROFILE"] == "compact"
    assert args[-2:] == ["--allowedTools", "mcp__rea"]  # ses outils sans demande d'autorisation
    claude = json.loads(Path(chat_tools.mcp_args("rea", spec, local=False)[1]).read_text("utf-8"))["mcpServers"]
    assert claude["rea"]["command"] == "cmd.exe"  # Claude : tous ses outils (fenêtre assez grande)
    # serveur fourni par l'extension elle-même (claude-mem) : pas dans ~/.claude.json, ses outils autorisés
    assert chat_tools.mcp_args("claudemem", marketplace.CHAT["claudemem"]) == [
        "--allowedTools",
        "mcp__plugin_claude-mem_mcp-search",
    ]


# ---------- demandes : dossiers et vidéos nommés ----------
def test_named_folders_and_videos(tmp_path):
    folder = tmp_path / "projet"
    (folder / "src").mkdir(parents=True)
    (folder / "src" / "a.py").write_text("x", encoding="utf-8")
    video = tmp_path / "film.mp4"
    video.write_bytes(b"0")
    message = f"Corrige {folder / 'src' / 'a.py'}, regarde {folder}. Et {tmp_path / 'absent' / 'b.py'}"
    assert chat_tools.named_folders(message) == [str(folder / "src"), str(folder)]
    assert chat_tools.video_source(f"{video} résume") == str(video)
    assert chat_tools.video_source("https://www.youtube.com/watch?v=abc). résume") == (
        "https://www.youtube.com/watch?v=abc"
    )
    work = tmp_path / "travail"
    work.mkdir()
    assert chat_tools.project_folder(work, f"spec dans {folder}") == folder
    assert chat_tools.project_folder(work, "et maintenant le plan") == folder  # gardé pour la conversation
    config = chat_tools._opencode_config("http://x/v1", None, [str(folder)])
    assert config["permission"]["external_directory"] == {str(folder / "*"): "allow"}


def test_tools_never_inherit_electron_run_as_node(monkeypatch):
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")  # Lezard IA lancé depuis un terminal de VS Code
    env = chat_tools.tool_env()
    assert "ELECTRON_RUN_AS_NODE" not in env and env["DO_NOT_TRACK"] == "1"


def test_bundled_programs_are_given_only_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert chat_tools.bundled_env(marketplace.CHAT["rea"]) == {}
    (tmp_path / "Programs" / "Ghidra" / "ghidra_12.1.4_PUBLIC").mkdir(parents=True)
    assert set(chat_tools.bundled_env(marketplace.CHAT["rea"])) == {"GHIDRA_INSTALL_DIR"}


# ---------- installations rejouables, désinstallations sans restes ----------
def test_reinstall_replaces_a_declared_mcp_server(tmp_path, monkeypatch):
    monkeypatch.setattr(installers.Path, "home", lambda: tmp_path)
    serena = installers.RECIPES["tool:serena"]["steps"][1]
    assert "--enable-web-dashboard" in serena  # sinon une fenêtre reste ouverte après chaque tâche
    assert installers.replaced_first(serena) is None
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"serena": {}}}), encoding="utf-8")
    assert installers.replaced_first(serena) == ("claude", "mcp", "remove", "--scope", "user", "serena")
    gone = ("claude", "mcp", "remove", "--scope", "user", "agentmemory")
    assert installers.already_undone(gone) and not installers.already_undone(serena)


def test_uninstall_steps_cover_what_the_tools_leave_behind():
    assert installers.uninstall_steps("tool:superclaude") == [
        (installers.PYTHON, installers.CLAUDE_FILES, "--remove", "commands/sc"),
        ("uv", "tool", "uninstall", "superclaude"),
    ]
    assert installers.uninstall_steps("tool:orca") == [(installers.PYTHON, installers.SETUP, "--uninstall", "Orca")]
    assert installers.uninstall_steps("app:goose") == [(installers.PYTHON, installers.SETUP, "--remove", "Goose")]
    for key, r in installers.RECIPES.items():  # uv : des restes d'une désinstallation ratée sont écrasés
        for argv in r.get("steps", ()):
            if argv[:3] == ("uv", "tool", "install"):
                assert "--force" in argv, key


def test_tool_folders_and_empty_npm_scopes(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    step = ("npm", "uninstall", "--global", "@musistudio/claude-code-router")
    folder = Path(installers._tool_folder(step))
    assert folder == tmp_path / "npm" / "node_modules" / "@musistudio" / "claude-code-router"
    folder.parent.mkdir(parents=True)
    installers.tidy_after(step)
    assert not folder.parent.exists()  # dossier @org vide retiré
    assert installers.stop_running(step) == []  # rien à arrêter : le dossier n'existe pas
    assert installers._tool_folder(("uv", "tool", "uninstall", "x")).endswith(str(Path("uv", "tools", "x")))


def test_unzipped_apps_are_removed_only_in_their_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    (tmp_path / "Programs" / "Goose" / "bin").mkdir(parents=True)
    assert setup_download.remove("Goose") == 0 and not (tmp_path / "Programs" / "Goose").exists()
    assert setup_download.remove("Goose") == 0  # déjà absent
    assert setup_download.remove("..") == 2  # nom refusé


def test_windows_setups_are_uninstalled_by_their_own_uninstaller(monkeypatch):
    calls, entries = [], [[{"DisplayName": "Jan", "QuietUninstallString": '"C:\\Jan\\uninstall.exe" /S'}], []]
    monkeypatch.setattr(setup_download.sys, "platform", "win32")
    monkeypatch.setattr(setup_download, "_registered", lambda name: entries.pop(0) if entries else [])
    monkeypatch.setattr(setup_download.subprocess, "run", lambda cmd, check: calls.append(cmd))
    assert setup_download.uninstall("Jan") == 0 and calls == ['"C:\\Jan\\uninstall.exe" /S']
    assert setup_download.uninstall("Jan") == 0 and len(calls) == 1  # déjà retiré : rien lancé


def test_claude_files_stay_inside_the_claude_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_files.Path, "home", lambda: tmp_path)
    (tmp_path / ".claude" / "commands" / "sc").mkdir(parents=True)
    assert claude_files.remove("commands/sc") == 0 and not (tmp_path / ".claude" / "commands" / "sc").exists()
    assert claude_files.remove("../Documents") == 2 and claude_files.remove(".") == 2


def test_user_path_entries_compare_with_variables(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert user_path.same(str(tmp_path / "claude-mem" / "bin"), installers.CLAUDE_MEM_BIN)
    assert not user_path.same("", installers.CLAUDE_MEM_BIN)


# ---------- claude-mem branché sur Lezard IA ----------
def test_claude_mem_summaries_go_through_lezard_ia(tmp_path, monkeypatch):
    monkeypatch.setattr(app_connect.Path, "home", lambda: tmp_path)
    settings = tmp_path / ".claude-mem" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"CLAUDE_MEM_PROVIDER": "claude", "CLAUDE_MEM_MODE": "code"}), encoding="utf-8")
    store = DocumentStore(":memory:")
    msg = app_connect.connect("claudemem", store, "compte-maitre", "free", 8123)
    data = json.loads(settings.read_text("utf-8"))
    assert msg.startswith("claude-mem fait ses résumés de mémoire avec Lezard IA")
    assert data["CLAUDE_MEM_PROVIDER"] == data["CLAUDE_MEM_QUOTA_FALLBACK_PROVIDER"] == "openai-compatible"
    assert data["CLAUDE_MEM_OPENAI_COMPAT_BASE_URL"] == "http://127.0.0.1:8123/v1"
    assert data["CLAUDE_MEM_TELEMETRY"] == "false" and data["CLAUDE_MEM_MODE"] == "code"  # le reste gardé
    assert [k["name"] for k in store.list_keys("compte-maitre")] == ["Application : claude-mem"]


@pytest.mark.parametrize("tool", ["hyperframes", "speckit", "bmad"])
def test_projects_are_prepared_by_the_tool_itself(tool):
    prep = marketplace.CHAT[tool]["prepare"]
    assert prep["program"] and prep["unless"] and "{folder}" in prep["note"]
