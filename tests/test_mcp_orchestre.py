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
def ollama(monkeypatch, tmp_path_factory):
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
    monkeypatch.setattr(mo, "STEPS", [])
    monkeypatch.setattr(mo, "ACTIVE", {"on": None})
    monkeypatch.setattr(mo, "OUTBOX", [])
    monkeypatch.setenv("LEZARD_IA_MODE", str(tmp_path_factory.mktemp("mode") / "mode.json"))
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
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["modeles", "bilan", "confier_etape"]
    assert replies[2]["error"]["code"] == -32601


def test_marketplace_recipe_registers_the_server_with_the_real_python():
    from gateway import installers

    add, skill = installers.RECIPES["tool:orchestre"]["steps"]
    resolved = installers.resolve(("python", *add[1:]))
    assert "{python}" not in resolved and resolved[-3].lower().endswith(("python.exe", "python"))
    assert Path(resolved[-2]).name.startswith("mcp_orchestre.") and resolved[-1] == "."
    assert installers._undo(add) == ("claude", "mcp", "remove", "--scope", "user", "orchestre")
    assert installers._undo(skill) == (installers.PYTHON, installers.ORCHESTRE, "--remove-skill")


def test_final_report_lists_every_delegated_step_and_asks_for_the_final_check(ollama, tmp_path):
    assert "rien à vérifier" in tool("bilan", {})[1]
    tool("confier_etape", {"consigne": "Résume   le fichier.", "ecrire_dans": "notes.md"}, [tmp_path])
    ollama["answer"] = "BLOQUE: fichier manquant."
    tool("confier_etape", {"consigne": "Trie les lignes.", "modele": "gros:27b"})
    ollama["answer"] = "Fait."
    tool("confier_etape", {"consigne": "x", "ecrire_dans": "parti.md"}, [tmp_path])
    (tmp_path / "parti.md").unlink()
    err, text = tool("bilan", {})
    assert not err and text.startswith("3 étape(s) confiée(s), 1 bloquée(s), 2 fichier(s) écrit(s)")
    assert "1. [faite] petit:4b — Résume le fichier." in text and "notes.md" in text and "octets)" in text
    assert "2. [bloquée] gros:27b — Trie les lignes." in text
    assert "parti.md (ABSENT" in text and "Vérification finale, à faire maintenant" in text
    skill = mo.SKILL
    assert "Vérification finale, obligatoire" in skill and "appelle `bilan`" in skill
    assert skill.index("Vérification finale, obligatoire") < skill.index("9. **Termine**")


def call_on(server, name, args, roots=()):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    r = mo.handle(msg, [Path(p).resolve() for p in roots], server)
    return r["result"]["isError"], r["result"]["content"][0]["text"]


def test_each_server_offers_only_its_own_tools():
    def names(server):
        return [t["name"] for t in mo.handle({"id": 1, "method": "tools/list"}, [], server)["result"]["tools"]]

    assert names("orchestre") == ["modeles", "bilan", "confier_etape"]
    assert names("resumes") == ["resumer_fichier", "resumer_dossier", "oublier_resumes"]
    assert names("brouillons") == ["controler", "retoucher"]
    assert call_on("resumes", "confier_etape", {"consigne": "x"})[0]  # outil d'un autre serveur : refusé


def test_summary_is_computed_once_then_comes_back_from_memory_until_the_file_changes(ollama, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRE_MEMOIRE", str(tmp_path / "memoire"))
    src = tmp_path / "projet"
    src.mkdir()
    (src / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    ollama["answer"] = "`f` (ligne 1) : rend 1."
    err, first = call_on("resumes", "resumer_fichier", {"fichier": "a.py"}, [src])
    assert not err and first.startswith("`f` (ligne 1)") and "calculé par petit:4b" in first
    assert "1\tdef f():" in ollama["sent"][0]["messages"][1]["content"]  # lignes numérotées pour le modèle
    err, again = call_on("resumes", "resumer_fichier", {"fichier": "a.py"}, [src])
    assert not err and "repris de la mémoire, 0 token" in again and len(ollama["sent"]) == 1
    call_on("resumes", "resumer_fichier", {"fichier": "a.py", "question": "Qui appelle f ?"}, [src])
    assert len(ollama["sent"]) == 2  # autre question : gardée à part
    (src / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    assert "calculé par" in call_on("resumes", "resumer_fichier", {"fichier": "a.py"}, [src])[1]
    assert len(ollama["sent"]) == 3
    assert call_on("resumes", "oublier_resumes", {})[1] == "3 résumé(s) oublié(s)."


def test_a_stuck_summary_is_an_error_and_is_not_kept(ollama, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRE_MEMOIRE", str(tmp_path / "memoire"))
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    ollama["answer"] = "BLOQUE: fichier illisible."
    err, text = call_on("resumes", "resumer_fichier", {"fichier": "a.py"}, [tmp_path])
    assert err and "bloque : fichier illisible." in text and not list((tmp_path / "memoire").glob("*.md"))


def test_a_whole_folder_in_one_call_reuses_what_is_already_known(ollama, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRE_MEMOIRE", str(tmp_path / "memoire"))
    src = tmp_path / "projet"
    (src / "pkg").mkdir(parents=True)
    for name in ("a.py", "pkg/b.py", "notes.txt"):
        (src / name).write_text(f"# {name}\n", encoding="utf-8")
    call_on("resumes", "resumer_fichier", {"fichier": "a.py"}, [src])
    err, text = call_on("resumes", "resumer_dossier", {"motif": "**/*.py"}, [src])
    assert not err and "## a.py" in text and "## pkg/b.py" in text and "notes.txt" not in text
    assert text.endswith("2 fichier(s) : 1 repris de la mémoire, 1 résumé(s) maintenant, 0 en échec.")
    assert call_on("resumes", "resumer_dossier", {"motif": "../**/*.py"}, [src])[0]
    assert call_on("resumes", "resumer_dossier", {"motif": "*.rs"}, [src])[0]
    monkeypatch.setattr(mo, "MAX_GROUP", 1)
    assert "resserrez le motif" in call_on("resumes", "resumer_dossier", {"motif": "**/*.py"}, [src])[1]


def test_draft_is_checked_against_the_sources_without_any_model(ollama, tmp_path):
    (tmp_path / "code.py").write_text(
        "import os\n\n\ndef charger(chemin):\n    return 1\n\n\nMAX = 3\n", encoding="utf-8"
    )
    good = "Dans `code.py` :\n- `charger(chemin)` (ligne 4) : lit.\n- `MAX` (8) : limite.\n"
    (tmp_path / "doc.md").write_text(good, encoding="utf-8")
    err, text = call_on("brouillons", "controler", {"brouillon": "doc.md", "sources": ["code.py"]}, [tmp_path])
    assert not err and text.startswith("3 nom(s) cité(s), 2 ligne(s) citée(s) contrôlée(s). Tout existe")
    bad = good + "- `sauver` (ligne 9) : écrit.\n- `MAX` (ligne 1) : limite.\n"
    (tmp_path / "doc.md").write_text(bad, encoding="utf-8")
    text = call_on("brouillons", "controler", {"brouillon": "doc.md", "sources": ["code.py"]}, [tmp_path])[1]
    assert "ABSENTS des sources (1) : sauver" in text and "PAS à la ligne citée (1) : MAX (ligne 1)" in text
    assert not ollama["sent"]  # aucun modèle appelé
    assert call_on("brouillons", "controler", {"brouillon": "doc.md"}, [tmp_path])[0]


def test_revision_is_done_by_the_local_model_in_place_and_a_stuck_one_leaves_the_file(ollama, tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# Titre\nfaux\n", encoding="utf-8")
    (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
    ollama["answer"] = "```markdown\n# Titre\njuste\nen plus\n```"
    args = {"fichier": "doc.md", "corrections": "Remplace faux par juste.", "sources": ["code.py"]}
    err, text = call_on("brouillons", "retoucher", args, [tmp_path])
    assert (
        not err and "2 lignes avant, 3 après" in text and doc.read_text(encoding="utf-8") == "# Titre\njuste\nen plus\n"
    )
    asked = ollama["sent"][0]["messages"][1]["content"]
    assert "<document>\n# Titre\nfaux" in asked and "Remplace faux par juste." in asked and "1\tx = 1" in asked
    ollama["answer"] = "BLOQUE: corrections contradictoires."
    text = call_on("brouillons", "retoucher", args, [tmp_path])[1]
    assert text.startswith("ÉTAPE BLOQUÉE") and "n'a pas été modifié" in text
    assert doc.read_text(encoding="utf-8") == "# Titre\njuste\nen plus\n"
    assert [x["state"] for x in mo.STEPS] == ["faite", "bloquée"]  # visibles dans le bilan de l'orchestre
    assert call_on("brouillons", "retoucher", {"fichier": "../x.md", "corrections": "y"}, [tmp_path])[0]


def test_cheaper_claude_assistants_are_written_and_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mine = tmp_path / ".claude" / "agents" / "le-mien.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("à moi", encoding="utf-8")
    assert mo.install_agents() == 0
    mid = (tmp_path / ".claude" / "agents" / "etape-moyenne.md").read_text(encoding="utf-8")
    low = (tmp_path / ".claude" / "agents" / "etape-simple.md").read_text(encoding="utf-8")
    assert mid.startswith("---\nname: etape-moyenne\ndescription: Use for") and "\nmodel: sonnet\n---\n" in mid
    assert "\nmodel: haiku\n---\n" in low and "BLOQUE:" in low
    assert mo.remove_agents() == 0
    assert [f.name for f in mine.parent.iterdir()] == ["le-mien.md"]  # les assistants de l'utilisateur restent


def test_only_the_known_user_variable_can_be_written(capsys):
    assert mo.user_env("PATH", "x") == 2 and "variable refusée" in capsys.readouterr().out


def test_marketplace_recipes_of_the_four_tools_install_and_undo():
    from gateway import installers

    for key, name in (("tool:resumes", "resumes"), ("tool:brouillons", "brouillons")):
        (add,) = installers.RECIPES[key]["steps"]
        assert add[-3:] == ("--serveur", name, ".") and name in mo.SERVERS
        assert installers._undo(add) == ("claude", "mcp", "remove", "--scope", "user", name)
    (agents,) = installers.RECIPES["tool:claude-etapes"]["steps"]
    assert installers._undo(agents) == (installers.PYTHON, installers.ORCHESTRE, "--remove-agents")
    (env,) = installers.RECIPES["tool:claude-allege"]["steps"]
    assert env[-2] in mo.ENV_ALLOWED
    assert installers._undo(env) == (
        installers.PYTHON,
        installers.ORCHESTRE,
        "--env-remove",
        "ENABLE_CLAUDEAI_MCP_SERVERS",
    )


def listed(server="lezard"):
    return [t["name"] for t in mo.handle({"id": 1, "method": "tools/list"}, [], server)["result"]["tools"]]


def test_switch_shows_the_tools_only_when_on_and_tells_the_client_the_list_changed(ollama, tmp_path):
    assert listed() == ["lezard_ia"]  # éteint par défaut : presque rien à charger
    err, text = call_on("lezard", "resumer_fichier", {"fichier": "a.py"}, [tmp_path])
    assert err and "/lezard on" in text and not ollama["sent"]
    assert "ÉTEINT" in call_on("lezard", "lezard_ia", {"action": "etat"})[1] and not mo.OUTBOX
    err, text = call_on("lezard", "lezard_ia", {"action": "activer"})
    assert not err and "ACTIF dans cette session" in text and "Nouvelles sessions : outils éteints" in text
    assert mo.OUTBOX == [{"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}]
    assert listed() == ["lezard_ia", "modeles", "bilan", "confier_etape", "resumer_fichier", "resumer_dossier",
                        "oublier_resumes", "controler", "retoucher"]  # fmt: skip
    assert not call_on("lezard", "confier_etape", {"consigne": "x"})[0] and len(ollama["sent"]) == 1
    call_on("lezard", "lezard_ia", {"action": "activer"})
    assert len(mo.OUTBOX) == 1  # déjà actif : rien à annoncer de nouveau
    assert "ÉTEINT" in call_on("lezard", "lezard_ia", {"action": "desactiver"})[1]
    assert listed() == ["lezard_ia"] and len(mo.OUTBOX) == 2
    assert call_on("lezard", "lezard_ia", {"action": "danser"})[0]
    assert listed("orchestre") == ["modeles", "bilan", "confier_etape"]  # les serveurs à part ne dépendent pas de lui


def test_switch_default_for_new_sessions_is_kept_on_disk(ollama, monkeypatch):
    text = call_on("lezard", "lezard_ia", {"action": "activer", "par_defaut": True})[1]
    assert "Nouvelles sessions : outils actifs" in text
    monkeypatch.setattr(mo, "ACTIVE", {"on": None})  # une nouvelle session
    assert mo.is_on() and "confier_etape" in listed()
    call_on("lezard", "lezard_ia", {"action": "desactiver"})  # cette session seulement
    monkeypatch.setattr(mo, "ACTIVE", {"on": None})
    assert mo.is_on()
    call_on("lezard", "lezard_ia", {"action": "desactiver", "par_defaut": True})
    monkeypatch.setattr(mo, "ACTIVE", {"on": None})
    assert not mo.is_on() and listed() == ["lezard_ia"]


def test_switch_announces_the_change_on_the_wire(tmp_path):
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "lezard_ia", "arguments": {"action": "activer"}},
        },
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    ]
    out = subprocess.run(
        [sys.executable, "-I", str(Path(mo.__file__)), "--serveur", "lezard"],
        input="\n".join(json.dumps(x) for x in lines) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
        env={**__import__("os").environ, "LEZARD_IA_MODE": str(tmp_path / "mode.json")},
    ).stdout.splitlines()
    replies = [json.loads(x) for x in out]
    assert replies[0]["result"]["capabilities"] == {"tools": {"listChanged": True}}
    assert replies[1]["id"] == 2 and replies[2] == {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
    assert len(replies[3]["result"]["tools"]) == 9


def test_lezard_skill_and_recipe(monkeypatch, tmp_path):
    from gateway import installers

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert mo.install_skill("lezard") == 0 and mo.install_skill("inconnue") == 2
    text = (tmp_path / ".claude" / "skills" / "lezard" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: lezard\ndescription: Use when the user types /lezard") and "lezard_ia" in text
    assert mo.remove_skill("lezard") == 0 and not (tmp_path / ".claude" / "skills" / "lezard").exists()
    add, skill, method = installers.RECIPES["tool:interrupteur"]["steps"]
    assert add[-3:] == ("--serveur", "lezard", ".")
    assert installers._undo(add) == ("claude", "mcp", "remove", "--scope", "user", "lezard")
    assert installers._undo(skill) == (installers.PYTHON, installers.ORCHESTRE, "--remove-skill", "lezard")
    assert installers._undo(method) == (installers.PYTHON, installers.ORCHESTRE, "--remove-skill")


def test_controle_accepte_les_noms_de_methode_avec_leur_classe():
    """Mesure du 2026-11-11 : `Runs.start` (ligne juste) était signalé absent, la source écrit « def start » dans
    « class Runs ». Une méthode inventée reste signalée."""
    from gateway import mcp_orchestre as m

    src = "class Runs:\n    x = 1\n\n    def start(self):\n        return 1\n"
    assert m._cited_in("Runs.start", src)
    assert not m._cited_in("Runs.invente", src)
    assert m._cited_in("Runs.start", "    def start(self):", near=True)
    assert not m._cited_in("Runs.start", "class Runs:", near=True)
