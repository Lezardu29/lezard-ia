"""Chef d'orchestre (serveur MCP) : une IA forte confie des étapes à des modèles moins chers ; une étape que son
modèle ne sait pas faire lui revient « bloquée »."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from gateway import mcp_orchestre as mo

TAGS = {
    "models": [
        {"name": "gros:27b", "size": 17e9, "details": {"parameter_size": "27B"}},
        {"name": "bge-m3:latest", "size": 1e9, "details": {}},
        {"name": "petit:4b", "size": 3e9, "details": {"parameter_size": "4B"}},
        {"name": "moyen:12b", "size": 8e9, "details": {"parameter_size": "12B"}},
    ]
}


@pytest.fixture
def ollama(monkeypatch):
    """Faux Ollama : `state` règle sa réponse et les modèles chargés ; `state["sent"]` garde les demandes."""
    state = {"sent": [], "answer": " Résumé. ", "loaded": [], "seen": 40, "done": "stop"}

    def call(url, body=None, timeout=0, key=""):
        if url.endswith("/api/tags"):
            return TAGS
        if url.endswith("/api/ps"):
            return {"models": [{"name": n} for n in state["loaded"]]}
        state["sent"].append(body)
        return {
            "message": {"content": state["answer"]},
            "prompt_eval_count": state["seen"],
            "eval_count": 3,
            "done_reason": state["done"],
        }

    monkeypatch.setattr(mo, "call", call)
    for name in ("ORCHESTRE_MODELE", "ORCHESTRE_FENETRE", "LEZARD_IA_URL", "LEZARD_IA_KEY"):
        monkeypatch.delenv(name, raising=False)
    return state


def tool(name, args, roots=()):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    r = mo.handle(msg, [Path(p).resolve() for p in roots])
    return r["result"]["isError"], r["result"]["content"][0]["text"]


def test_lists_text_models_lightest_first_and_prefers_the_loaded_one(ollama):
    err, text = tool("modeles", {})
    assert not err and "bge" not in text
    assert text.index("petit:4b") < text.index("moyen:12b") < text.index("gros:27b")
    assert text.endswith("Par défaut : petit:4b")
    ollama["loaded"] = ["moyen:12b"]  # déjà sur la carte graphique : pas de rechargement
    text = tool("modeles", {})[1]
    assert "moyen:12b : 8.0 Go, 12B — déjà chargé" in text and text.endswith("Par défaut : moyen:12b")


def test_step_reads_files_itself_and_tells_the_worker_how_to_say_it_is_stuck(ollama, tmp_path):
    (tmp_path / "notes.txt").write_text("Le ciel est bleu.", encoding="utf-8")
    args = {"consigne": "Résume.", "fichiers": ["notes.txt"], "role": "Bref."}
    err, text = tool("confier_etape", args, [tmp_path])
    assert not err and text.startswith("Résumé.") and "petit:4b · 40 tokens lus, 3 écrits" in text
    body = ollama["sent"][0]
    assert body["model"] == "petit:4b" and body["stream"] is False
    assert body["options"] == {"num_ctx": mo.DEFAULT_WINDOW}
    system, user = body["messages"]
    assert system["role"] == "system" and "BLOQUE:" in system["content"] and system["content"].endswith("Rôle : Bref.")
    assert "Le ciel est bleu." in user["content"] and user["content"].endswith("Résume.")


@pytest.mark.parametrize(
    "answer", ["BLOQUE: il manque le fichier config.", "**Bloqué** : il manque le fichier config.", ""]
)
def test_stuck_step_comes_back_to_the_orchestrator_and_writes_nothing(ollama, tmp_path, answer):
    ollama["answer"] = answer
    err, text = tool("confier_etape", {"consigne": "x", "ecrire_dans": "sortie.md"}, [tmp_path])
    assert not err and text.startswith("ÉTAPE BLOQUÉE : ")
    assert ("il manque le fichier config." in text) or ("réponse vide." in text)
    assert "prends un modèle plus fort" in text and not (tmp_path / "sortie.md").exists()


def test_overflowing_or_cut_answers_are_stuck_too(ollama, monkeypatch, tmp_path):
    monkeypatch.setenv("ORCHESTRE_FENETRE", "40")
    text = tool("confier_etape", {"consigne": "x"})[1]
    assert text.startswith("ÉTAPE BLOQUÉE : demande plus longue que la fenêtre (40 tokens)") and "Résumé." in text
    monkeypatch.delenv("ORCHESTRE_FENETRE")
    ollama["done"] = "length"
    text = tool("confier_etape", {"consigne": "x", "ecrire_dans": "a.txt", "max_tokens": 5}, [tmp_path])[1]
    assert text.startswith("ÉTAPE BLOQUÉE : réponse coupée") and not (tmp_path / "a.txt").exists()
    assert ollama["sent"][-1]["options"]["num_predict"] == 5


def test_long_answers_go_to_a_file_and_only_a_receipt_comes_back(ollama, tmp_path):
    ollama["answer"] = "```python\n" + "\n".join(f"ligne {i}" for i in range(40)) + "\n```"
    err, text = tool("confier_etape", {"consigne": "x", "ecrire_dans": "src/gen.py"}, [tmp_path])
    written = (tmp_path / "src" / "gen.py").read_text(encoding="utf-8")
    assert not err and written.startswith("ligne 0\n") and written.endswith("ligne 39\n") and "```" not in written
    assert "40 lignes" in text and "ligne 4" in text and "ligne 5" not in text  # le début seulement


def test_chosen_model_must_exist_and_env_sets_the_default(ollama, monkeypatch):
    assert not tool("confier_etape", {"consigne": "x", "modele": "gros:27b"})[0]
    assert ollama["sent"][-1]["model"] == "gros:27b"
    err, text = tool("confier_etape", {"consigne": "x", "modele": "absent:1b"})
    assert err and "petit:4b" in text
    monkeypatch.setenv("ORCHESTRE_MODELE", "gros:27b")
    tool("confier_etape", {"consigne": "x"})
    assert ollama["sent"][-1]["model"] == "gros:27b"


def test_files_only_inside_allowed_folders(ollama, tmp_path):
    inside, outside = tmp_path / "ok", tmp_path / "secret.txt"
    inside.mkdir()
    outside.write_text("clé", encoding="utf-8")
    assert tool("confier_etape", {"consigne": "x", "fichiers": [str(outside)]})[0]  # aucun dossier autorisé
    err, text = tool("confier_etape", {"consigne": "x", "fichiers": ["../secret.txt"]}, [inside])
    assert err and "hors des dossiers" in text
    err, text = tool("confier_etape", {"consigne": "x", "ecrire_dans": "../pirate.txt"}, [inside])
    assert err and "hors des dossiers" in text and not (tmp_path / "pirate.txt").exists()
    assert not ollama["sent"] and tool("confier_etape", {"consigne": ""})[0]


def test_steps_go_through_lezard_ia_when_its_address_and_key_are_given(monkeypatch):
    sent = []

    def call(url, body=None, timeout=0, key=""):
        sent.append((url, body, key))
        if url.endswith("/v1/models"):
            return {"data": [{"id": "ollama/petit:4b"}, {"id": "anthropic/claude-haiku"}, {"id": "ollama/bge-m3"}]}
        return {
            "choices": [{"message": {"content": "Fait."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 2},
            "x_gateway": {"model": "petit:4b"},
        }

    monkeypatch.setattr(mo, "call", call)
    monkeypatch.delenv("ORCHESTRE_MODELE", raising=False)
    monkeypatch.setenv("LEZARD_IA_URL", "http://127.0.0.1:8000/v1/")
    monkeypatch.setenv("LEZARD_IA_KEY", "sk-test")
    listed = tool("modeles", {})[1]
    assert "anthropic/claude-haiku" in listed and "bge" not in listed and listed.endswith("Par défaut : auto")
    err, text = tool("confier_etape", {"consigne": "x", "max_tokens": 9})
    url, body, key = sent[-1]
    assert not err and text.startswith("Fait.") and "12 tokens lus, 2 écrits" in text and "modèle : petit:4b" in text
    assert url == "http://127.0.0.1:8000/v1/chat/completions" and key == "sk-test"
    assert body["model"] == "auto" and body["max_tokens"] == 9


def test_skill_is_written_for_claude_code_and_removed(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert mo.install_skill() == 0
    text = (tmp_path / ".claude" / "skills" / "orchestre" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: orchestre\ndescription: Use when") and "confier_etape" in text
    assert mo.remove_skill() == 0 and not (tmp_path / ".claude" / "skills" / "orchestre").exists()
    assert mo.remove_skill() == 0 and "Déjà retiré." in capsys.readouterr().out


def test_speaks_mcp_over_stdio_as_a_plain_script():
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "inconnue"},
    ]
    out = subprocess.run(
        [sys.executable, "-I", str(Path(mo.__file__))],
        input="\n".join(json.dumps(x) for x in lines) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    ).stdout.splitlines()
    replies = [json.loads(x) for x in out]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["result"]["serverInfo"]["name"] == "orchestre"
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["modeles", "confier_etape"]
    assert replies[2]["error"]["code"] == -32601


def test_marketplace_recipe_registers_the_server_with_the_real_python():
    from gateway import installers

    add, skill = installers.RECIPES["tool:orchestre"]["steps"]
    resolved = installers.resolve(("python", *add[1:]))
    assert "{python}" not in resolved and resolved[-3].lower().endswith(("python.exe", "python"))
    assert Path(resolved[-2]).name.startswith("mcp_orchestre.") and resolved[-1] == "."
    assert installers._undo(add) == ("claude", "mcp", "remove", "--scope", "user", "orchestre")
    assert installers._undo(skill) == (installers.PYTHON, installers.ORCHESTRE, "--remove-skill")
