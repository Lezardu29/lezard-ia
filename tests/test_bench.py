"""Banc d'essai de l'agent PC (27.2) : faux bureau, tâches de référence, boucle complète avec un modèle scripté."""

import asyncio
import json

import httpx
import pytest

from gateway.tools import RuleRefusal, ToolError, bench
from gateway.tools.bench import TASKS, Desktop, Ollama, run_task, summary


def _go(coro):
    return asyncio.run(coro)


def _num(d: Desktop, name: str) -> int:
    return next(i for i, e in enumerate(d.elements, 1) if e.name == name)


def test_start_menu_search_opens_an_app():
    d = Desktop()
    _go(d.screenshot({}))
    _go(d.click({"element": _num(d, "Démarrer"), "reason": "menu"}))
    _go(d.type_text({"text": "bloc-notes"}))  # le champ de recherche a le focus
    r = _go(d.press({"keys": "enter"}))  # Entrée juste après la frappe : permise
    assert d.front.app == "notepad" and "Bloc-notes" in r.text and r.image.startswith("data:image/jpeg")
    with pytest.raises(RuleRefusal):  # après une touche, comme sur le vrai PC : cliquer d'abord dans le champ
        _go(d.type_text({"text": "bonjour"}))
    _go(d.click({"element": _num(d, "Zone de texte")}))
    _go(d.type_text({"text": "bonjour"}))
    assert TASKS[0].check(d)


def test_mouse_first_and_unchanged_screen():
    d = Desktop(("notepad",), "notepad")
    _go(d.screenshot({}))
    with pytest.raises(RuleRefusal):
        _go(d.press({"keys": "alt+f4"}))  # un élément « Fermer » existe
    with pytest.raises(RuleRefusal):
        _go(d.type_text({"text": "x"}))  # pas encore cliqué dans le champ
    r = _go(d.click({"element": _num(d, "Fermer")}))
    assert d.window("notepad") is None and not r.no_effect
    r = _go(d.click({"cell": "P1"}))  # case vide : rien ne change
    assert r.no_effect and "n'a pas changé" in r.text
    with pytest.raises(ToolError):
        _go(d.click({"element": 99}))
    with pytest.raises(ToolError):
        _go(d.press({"keys": "ctrl+inconnue"}))


def test_explorer_needs_a_double_click_and_finds_files():
    d = Desktop()
    _go(d.open_app_tool({"name": "explorateur de fichiers"}))
    _go(d.click({"element": _num(d, "Téléchargements")}))  # volet de navigation : un clic suffit
    assert TASKS[5].check(d)
    _go(d.click({"element": _num(d, "Rechercher")}))
    _go(d.type_text({"text": "rapport"}))  # résultats pendant la frappe, sans Entrée
    _go(d.click({"element": _num(d, "rapport.pdf")}))  # un clic : sélection seulement
    assert not TASKS[6].check(d)
    _go(d.click({"element": _num(d, "rapport.pdf"), "button": "double"}))
    assert TASKS[6].check(d) and d.front.app == "pdf"


def test_discord_browser_and_focus():
    d = Desktop(("discord", "notepad"), "notepad")
    _go(d.focus_window({"title": "discord"}))
    assert TASKS[3].check(d)
    _go(d.click({"element": _num(d, "Velistu")}))
    _go(d.click({"element": _num(d, "Envoyer un message à @Velistu")}))
    _go(d.type_text({"text": "Salut !"}))
    _go(d.press({"keys": "enter"}))
    assert TASKS[4].check(d)
    _go(d.open_app_tool({"name": "navigateur"}))
    _go(d.type_text({"text": "météo Brest"}))
    _go(d.press({"keys": "enter"}))
    assert TASKS[8].check(d) and not TASKS[2].check(d)
    _go(d.open_url({"url": "https://fr.wikipedia.org"}))
    assert TASKS[2].check(d)
    with pytest.raises(ToolError):
        _go(d.focus_window({"title": "inexistante"}))


def _reply(content="", call=None):
    msg = {"content": content}
    if call:
        msg["tool_calls"] = [{"id": "x", "function": {"name": call[0], "arguments": json.dumps(call[1])}}]
    return {"choices": [{"message": msg}]}


def test_full_loop_with_a_scripted_model():
    script = iter(
        [
            _reply('{"etapes": ["ouvrir le bloc-notes", "écrire bonjour"]}'),  # planificateur
            _reply(call=("pc_open", {"reason": "pas ouvert", "name": "bloc-notes"})),
            _reply(call=("pc_type", {"reason": "zone de texte", "text": "bonjour"})),
            _reply("C'est fait."),
        ]
    )

    async def call(msgs, specs):
        return next(script), "local"

    r = _go(run_task(TASKS[0], call, max_steps=8))
    assert r["success"] and r["actions"] == 2 and r["plan"] == ["ouvrir le bloc-notes", "écrire bonjour"]
    assert r["answer"] == "C'est fait." and r["error"] is None
    s = summary([r, {**r, "success": False, "actions": 9}])
    assert s["success_rate"] == 0.5 and s["mean_actions_success"] == 2


def test_claimed_success_is_not_success():
    async def call(msgs, specs):
        return _reply("J'ai ouvert la calculatrice."), "local"  # rien fait

    r = _go(run_task(TASKS[1], call, max_steps=4))
    assert not r["success"] and r["actions"] == 0
    r = _go(run_task(TASKS[9], call, max_steps=4))  # « réponds sans rien faire » : réussi
    assert r["success"]


def test_ollama_falls_back_to_tools_in_text_for_gemma3():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("tools"):
            return httpx.Response(400, json={"error": "registry.ollama.ai/library/gemma3:12b does not support tools"})
        assert body["options"]["num_ctx"] >= 16384
        content = '{"tool": "pc_screenshot", "arguments": {}}' if len(bodies) == 2 else "fini"
        return httpx.Response(200, json={"model": body["model"], "message": {"role": "assistant", "content": content},
                                         "done": True})  # fmt: skip

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            o = Ollama("gemma3:12b", "http://ollama", client)
            specs = [t.spec() for t in Desktop().tools().values()]
            first, _ = await o([{"role": "user", "content": "capture"}], specs)
            second, _ = await o([{"role": "user", "content": "et ensuite ?"}], None)
            return o, first, second

    o, first, second = _go(go())
    assert o.emulated and first["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "pc_screenshot"
    assert "Outils disponibles" in bodies[1]["messages"][0]["content"]  # outils décrits en texte
    assert all("Outils disponibles" not in str(m["content"]) for m in bodies[2]["messages"])  # tour sans outils
    assert second["choices"][0]["message"]["content"] == "fini"


def test_cli_rejects_unknown_tasks(capsys):
    with pytest.raises(SystemExit):
        bench.main(["--tasks", "inconnue"])
    assert "inconnue" in capsys.readouterr().err


def test_python_style_tool_call_from_gemma3_is_understood():
    """gemma3 écrit souvent None / True et des numéros entre guillemets : c'était lu comme une réponse finale."""
    from gateway.tools import runner

    text = '{"tool": "pc_click", "arguments": {"reason": "x", "element": "2", "zoom": None, "cell": None}}'
    r = runner.parse_emulated({"choices": [{"message": {"content": text}}]}, {"pc_click"})
    fn = r["choices"][0]["message"]["tool_calls"][0]["function"]
    assert fn["name"] == "pc_click" and json.loads(fn["arguments"]) == {"reason": "x", "element": "2"}
    d = Desktop(("notepad",), "notepad")
    _go(d.click({"element": "1"}))  # « Fermer », numéro écrit en texte
    assert d.window("notepad") is None
    bad = {"choices": [{"message": {"content": "{__import__('os')}"}}]}
    assert runner.parse_emulated(bad, {"pc_click"}) == bad  # rien n'est exécuté


def test_cli_runs_every_task_and_writes_the_report(monkeypatch, tmp_path, capsys):
    class Talker:  # répond sans agir : seule « rien_a_faire » réussit
        def __init__(self, model, *_):
            self.model = model

        async def __call__(self, msgs, specs):
            return {"choices": [{"message": {"role": "assistant", "content": "144"}, "finish_reason": "stop"}]}, "local"

    monkeypatch.setattr(bench, "Ollama", Talker)
    assert bench.main(["--models", "m1", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "rien_a_faire" in out and "Détail" in out
    report = json.loads(next(tmp_path.glob("bench-*.json")).read_text(encoding="utf-8"))
    s = report["models"]["m1"]["summary"]
    assert s["tasks"] == len(TASKS) and s["success"] == 1 and s["mean_actions_success"] == 0


def test_unreachable_model_is_an_error_not_a_success():
    async def down(msgs, specs):
        raise httpx.ConnectError("refusé")

    r = _go(run_task(next(t for t in TASKS if t.id == "rien_a_faire"), down, 4))
    assert not r["success"] and r["error"].startswith("ConnectError")
    assert summary([r])["mean_actions_success"] is None
