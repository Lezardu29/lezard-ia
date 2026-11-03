"""Outils exécutés par la gateway (phases 21 à 26) : boucle modèle ↔ outils, droits par compte, validation humaine,
arrêt d'urgence, protection SSRF, calculatrice sûre."""

import asyncio
import json
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import tools
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore
from gateway.tools import StopRun, Tool, ToolError, ToolResult, basic, runner, state
from gateway.tools import web_tools as web

KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {KEY}"}
ASK = {"model": "ollama/llama3.1:8b", "messages": [{"role": "user", "content": "Combien font 2+2 ?"}]}


class Scripted:
    """Ollama : appelle la calculatrice tant qu'aucun résultat d'outil n'est revenu, puis répond."""

    def __init__(self):
        self.bodies = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools"], "model_info": {}})
        body = json.loads(request.content)
        self.bodies.append(body)
        last = body["messages"][-1]
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if last["role"] == "tool":
            msg = {"role": "assistant", "content": f"Résultat : {last['content']}"}
        else:
            msg = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "calculator", "arguments": '{"expression": "2+2"}'},
                    }
                ],
            }
        return httpx.Response(
            200, json={"choices": [{"index": 0, "finish_reason": "stop", "message": msg}], "usage": usage}
        )


def make_client(fake):
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        plans=PlanResolver({}, "free", master_key=KEY),
        store=DocumentStore(":memory:"),
    )
    return TestClient(app)


def test_tool_loop_non_stream():
    fake = Scripted()
    with make_client(fake) as c:
        r = c.post("/v1/chat/completions", headers=MASTER, json={**ASK, "gateway_tools": True})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["choices"][0]["message"]["content"] == "Résultat : 2+2 = 4"
    assert data["x_gateway"]["tools"] == [{"name": "calculator", "ok": True}]
    assert data["usage"]["total_tokens"] == 30
    assert {t["function"]["name"] for t in fake.bodies[0]["tools"]} >= {"calculator", "web_fetch"}
    assert "gateway_tools" not in fake.bodies[0]


def test_tool_loop_stream_events():
    with make_client(Scripted()) as c:
        r = c.post(
            "/v1/chat/completions", headers=MASTER, json={**ASK, "gateway_tools": ["calculator"], "stream": True}
        )
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: {")]
    kinds = [e["x_gateway_tool"]["type"] for e in events if "x_gateway_tool" in e]
    assert kinds == ["call", "result"]
    text = "".join((e["choices"][0]["delta"] or {}).get("content") or "" for e in events if e.get("choices"))
    assert text == "Résultat : 2+2 = 4"


def test_rights_and_errors(monkeypatch):
    monkeypatch.setenv("TOOLS_PC", "false")
    with make_client(Scripted()) as c:
        names = {t["name"] for t in c.get("/v1/tools", headers=MASTER).json()["data"]}
        assert {"calculator", "web_search", "web_fetch"} <= names and not any(n.startswith("pc_") for n in names)
        both = {**ASK, "gateway_tools": True, "tools": [{"type": "function", "function": {"name": "x"}}]}
        assert c.post("/v1/chat/completions", headers=MASTER, json=both).status_code == 400
        assert (
            c.post("/v1/chat/completions", headers=MASTER, json={**ASK, "gateway_tools": ["inconnu"]}).status_code
            == 403
        )
        assert c.post("/v1/tools/approvals/abc", headers=MASTER, json={"approve": True}).status_code == 404
        assert c.post("/v1/tools/approvals/abc", json={"approve": True}).status_code == 403  # pas la clé maître


def test_levels():
    class Ctx:
        plan, is_admin = "free", False

    assert tools.available(Ctx()) == {}  # clés virtuelles et comptes sans permission « tools » : aucun outil
    Ctx.is_admin = True
    assert {"calculator", "web_fetch"} <= set(tools.available(Ctx()))


# ---------- boucle : validation, modèle cloud, arrêt d'urgence, captures ----------
def _calls(*names):
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"id": f"c{i}", "function": {"name": n, "arguments": "{}"}} for i, n in enumerate(names)
                    ],
                }
            }
        ]
    }


FINAL = {"choices": [{"message": {"content": "fini"}}]}


async def _drive(tool, *, hosting="local", decide=None):
    seen, audits = [], []
    replies = iter([_calls(tool.name), FINAL])

    async def call(msgs, specs):
        seen.append(msgs)
        return next(replies), hosting

    events = []
    async for chunk in runner.run(
        call=call,
        tools={tool.name: tool},
        messages=[{"role": "user", "content": "go"}],
        approvals=runner.APPROVALS,
        identity="me",
        audit=lambda *a: audits.append(a),
        max_steps=4,
    ):
        ev = chunk.get("x_gateway_tool")
        events.append(chunk)
        if ev and ev["type"] == "approval" and decide is not None:
            assert not runner.APPROVALS.decide(ev["approval"], "quelqu'un d'autre", True)  # lié au compte
            assert runner.APPROVALS.decide(ev["approval"], "me", decide)
    return events, seen, audits


def _tool(run, **kw):
    return Tool("act", "test", {"type": "object", "properties": {}}, run, level="pc", **kw)


def test_approval_required():
    ran = []

    async def act(args):
        ran.append(args)
        return ToolResult("ok", image="data:image/jpeg;base64,AAAA")

    events, seen, audits = asyncio.run(_drive(_tool(act, confirm=True), decide=False))
    assert not ran and audits[0][2] == "refusée"
    assert "refusée" in seen[1][-1]["content"]
    events, seen, audits = asyncio.run(_drive(_tool(act, confirm=True), decide=True))
    assert ran and audits[0][2] == "autorisée"
    assert seen[1][-1]["content"][1]["image_url"]["url"].startswith("data:image")  # capture jointe au modèle
    assert all("x_capture" not in m for m in seen[1])
    assert not runner.PC_LOCK.locked()


def test_local_only_and_stop():
    async def act(args):
        raise StopRun("Arrêt d'urgence")

    _, seen, _ = asyncio.run(_drive(_tool(act, local_only=True), hosting="cloud"))
    assert "modèle local" in seen[1][-1]["content"]
    events, seen, _ = asyncio.run(_drive(_tool(act)))
    assert len(seen) == 1  # le modèle n'est plus rappelé
    assert any("⛔" in ((e.get("choices") or [{}])[0].get("delta") or {}).get("content", "") for e in events)


def test_calculator_is_safe():
    calc = lambda e: asyncio.run(basic.calculate({"expression": e})).text  # noqa: E731
    assert calc("sqrt(16) + 2^3") == "sqrt(16) + 2^3 = 12.0"
    for bad in ("9**9**9", "__import__('os')", "().__class__", "factorial(5000)"):
        with pytest.raises(ToolError):
            calc(bad)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/v1/models",
        "http://localhost/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "file:///C:/Windows/win.ini",
    ],
)
def test_web_fetch_refuses_private(url):
    with pytest.raises(ToolError):
        asyncio.run(web.web_fetch({"url": url}))


def test_page_text():
    raw = "<html><head><title>T &amp; co</title><script>x()</script></head><body><p>Un</p><p>Deux</p></body></html>"
    assert web.page_text(raw) == "# T & co\nUn\nDeux"


@pytest.mark.skipif(sys.platform != "win32", reason="contrôle du PC : Windows")
def test_pc_grid_target():
    from PIL import Image

    from gateway.tools import pc

    pc._shot.update(image=Image.new("RGB", (1280, 720)), scale=0.5, rows=9, cell=80, cell_h=80)
    assert pc._target({"cell": "a1"}) == (40, 40)
    assert pc._target({"x": 100, "y": 50}) == (100, 50)
    for bad in ({"cell": "Z9"}, {"cell": "A10"}, {"x": 5000, "y": 1}, {}):
        with pytest.raises(ToolError):
            pc._target(bad)
    assert pc._preview_point({"cell": "B2"}).startswith("data:image/jpeg")
    with pytest.raises(ToolError):
        asyncio.run(pc.press({"keys": "ctrl+inconnue"}))


def test_chat_preference():
    from gateway.preferences import PreferenceError, validate_chat

    assert validate_chat({"tools": True})["tools"] is True
    assert "tools" not in validate_chat({"tools": False})
    with pytest.raises(PreferenceError):
        validate_chat({"tools": "oui"})


# ---------- web : lecture et recherche (réseau simulé) ----------
SEARCH_HTML = (
    '<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fex.fr%2Fa&amp;rut=x">Titre <b>A</b></a>'
    '<a class="result__snippet" href="#">Extrait &amp; suite</a>'
)


def _fake_web(monkeypatch, handler):
    async def public(host):
        if host == "interne.lan":
            raise ToolError("Adresse refusée")
        return "93.184.216.34"

    real = httpx.AsyncClient
    monkeypatch.setattr(web, "_public_ip", public)
    monkeypatch.setattr(web.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_web_fetch_and_search(monkeypatch):
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers["host"]))
        path = request.url.path
        if path == "/redir":
            return httpx.Response(302, headers={"location": "http://interne.lan/admin"})
        if path == "/image":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"x")
        if path == "/absent":
            return httpx.Response(404)
        if path == "/html/":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=SEARCH_HTML)
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text="<p>Bonjour</p>" * 2000)

    _fake_web(monkeypatch, handler)
    out = asyncio.run(web.web_fetch({"url": "https://exemple.fr/page"})).text
    assert out.startswith("Contenu de https://exemple.fr/page") and out.endswith("[… tronqué]")
    assert seen[0] == ("https://93.184.216.34/page", "exemple.fr")  # connexion vers l'IP vérifiée
    for path in ("/redir", "/image", "/absent"):
        with pytest.raises(ToolError):
            asyncio.run(web.web_fetch({"url": f"https://exemple.fr{path}"}))
    res = asyncio.run(web.web_search({"query": "lézard"})).text
    assert "Titre A — https://ex.fr/a" in res and "Extrait & suite" in res
    with pytest.raises(ToolError):
        asyncio.run(web.web_search({"query": ""}))


def test_datetime():
    assert "/20" in asyncio.run(basic.now({})).text


@pytest.mark.skipif(sys.platform != "win32", reason="contrôle du PC : Windows")
def test_pc_actions_without_touching_the_pc(monkeypatch):
    from PIL import Image

    from gateway.tools import pc

    sent, moves = [], []
    monkeypatch.setattr(pc, "_send", lambda *i: sent.append(len(i)))
    monkeypatch.setattr(pc._user32, "SetCursorPos", lambda x, y: moves.append((x, y)), raising=False)
    monkeypatch.setattr(pc, "_check_failsafe", lambda: None)
    monkeypatch.setattr(pc, "_show_ring", lambda x, y: moves.append(("cercle", x, y)))
    monkeypatch.setattr(pc, "_elements", lambda: [])  # pas d'appel réel à UI Automation
    real_sleep = asyncio.sleep
    monkeypatch.setattr(pc.asyncio, "sleep", lambda s: real_sleep(0))

    async def after():
        return ToolResult("ok")

    monkeypatch.setattr(pc, "_after", after)
    monkeypatch.setattr("PIL.ImageGrab.grab", lambda: Image.new("RGB", (1920, 1080)))
    shot = asyncio.run(pc.screenshot({}))
    assert shot.image.startswith("data:image/jpeg") and "A1 à P9" in shot.text
    asyncio.run(pc.click({"cell": "A1", "button": "double"}))
    assert moves[0] == ("cercle", 60, 60) and moves[-1] == (60, 60) and sent == [2, 2]  # cercle, glissé, clic
    asyncio.run(pc.type_text({"text": "é😀"}))  # 😀 = deux unités UTF-16
    asyncio.run(pc.press({"keys": "ctrl+s"}))
    asyncio.run(pc.scroll({"direction": "down", "amount": 3}))
    assert sent[2:] == [2, 2, 2, 4, 1]
    for fn, args in (
        (pc.click, {"cell": "A1", "button": "milieu"}),
        (pc.type_text, {"text": ""}),
        (pc.scroll, {"direction": "gauche"}),
    ):
        with pytest.raises(ToolError):
            asyncio.run(fn(args))


def test_runner_errors_and_step_limit(monkeypatch):
    monkeypatch.setattr(runner, "HEARTBEAT", 0.01)
    monkeypatch.setattr(runner, "APPROVAL_TIMEOUT", 0.02)

    async def act(args):
        return ToolResult("ok")

    bad = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {"id": "a", "function": {"name": "inconnu", "arguments": "{}"}},
                        {"id": "b", "function": {"name": "act", "arguments": "pas du json"}},
                        {"id": "c", "function": {"name": "act", "arguments": "[1]"}},
                        {"function": {"name": "act", "arguments": {}}},
                    ]
                }
            }
        ]
    }

    async def drive():
        seen = []

        async def call(msgs, specs):
            seen.append(specs)
            return bad, "local"

        tool = _tool(act, confirm=True)
        out = [
            c
            async for c in runner.run(
                call=call,
                tools={"act": tool},
                messages=[],
                approvals=runner.APPROVALS,
                identity="me",
                audit=lambda *a: None,
                max_steps=1,
            )
        ]
        return seen, out

    seen, out = asyncio.run(drive())
    assert seen[-1] is None  # dernier tour : plus d'outils proposés
    kinds = [c["x_gateway_tool"]["type"] for c in out if "x_gateway_tool" in c]
    assert "waiting" in kinds and kinds.count("approval") == 1  # sans réponse : refus après le délai
    assert out[-2]["choices"][0]["delta"]["content"] == "(limite d'étapes atteinte)"
    texts = [c["x_gateway_tool"]["text"] for c in out if c.get("x_gateway_tool", {}).get("type") == "result"]
    assert any("inconnu" in t for t in texts) and any("illisibles" in t for t in texts)
    assert any("objet JSON" in t for t in texts)


def test_pc_lock_busy():
    async def act(args):
        return ToolResult("ok")

    async def drive():
        await runner.PC_LOCK.acquire()
        try:
            return await _drive(_tool(act))
        finally:
            runner.PC_LOCK.release()

    _, seen, _ = asyncio.run(drive())
    assert "Un autre agent" in seen[1][-1]["content"]


def test_one_action_per_turn_with_a_card_each_time():
    ran, audits, cards = [], [], []

    async def act(args):
        ran.append(args)
        return ToolResult("ok")

    tool = _tool(act, confirm=True)
    replies = iter([_calls("act", "act"), _calls("act"), FINAL])  # 2 actions d'un coup, puis 1

    async def drive():
        async def call(msgs, specs):
            return next(replies), "local"

        async for chunk in runner.run(
            call=call,
            tools={"act": tool},
            messages=[],
            approvals=runner.APPROVALS,
            identity="me",
            audit=lambda *a: audits.append(a[2]),
            max_steps=5,
        ):
            ev = chunk.get("x_gateway_tool") or {}
            if ev.get("type") == "approval":
                cards.append(ev["id"])
                assert runner.APPROVALS.decide(ev["approval"], "me", True)
            if ev.get("type") == "result" and not ev["ok"]:
                assert "une seule action" in ev["text"]

    asyncio.run(drive())
    assert len(cards) == 2 and len(ran) == 2  # 2e action du 1er tour ignorée ; tour suivant : nouvelle carte
    assert audits == ["autorisée", "autorisée"]


def test_each_command_asks_again():
    ran, audits = [], []

    async def act(args):
        ran.append(1)
        return ToolResult("ok")

    tool = _tool(act, confirm=True)
    approvals = runner.Approvals()

    async def one(decision):
        replies = iter([_calls("act"), FINAL])
        cards = 0

        async def call(msgs, specs):
            return next(replies), "local"

        async for chunk in runner.run(
            call=call,
            tools={"act": tool},
            messages=[],
            approvals=approvals,
            identity="me",
            audit=lambda *a: audits.append(a[2]),
            max_steps=3,
        ):
            ev = chunk.get("x_gateway_tool") or {}
            if ev.get("type") == "approval":
                cards += 1
                approvals.decide(ev["approval"], "me", decision)
        return cards

    assert asyncio.run(one(True)) == 1 and len(ran) == 1
    assert asyncio.run(one(True)) == 1 and len(ran) == 2  # commande suivante : nouvelle carte
    assert asyncio.run(one(False)) == 1 and len(ran) == 2
    assert audits == ["autorisée", "autorisée", "refusée"]


def test_approval_answers_are_yes_or_no():
    with make_client(Scripted()) as c:
        assert "pc_session_seconds" not in c.get("/v1/tools", headers=MASTER).json()
        assert c.post("/v1/tools/session", headers=MASTER).status_code == 404  # plus d'autorisation de session
        assert c.post("/v1/tools/approvals/x", headers=MASTER, json={"approve": "tout"}).status_code == 400
        assert c.post("/v1/tools/approvals/x", headers=MASTER, json={"approve": "all"}).status_code == 400


class NoToolsModel(Scripted):
    """gemma3 : pas d'appel d'outils natif ; répond par le JSON décrit dans le prompt."""

    def __call__(self, request):
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemma3:12b"}]})
        if path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "vision"], "model_info": {}})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        body = json.loads(request.content)
        self.bodies.append(body)
        last = body["messages"][-1]
        text = last["content"] if isinstance(last["content"], str) else last["content"][0]["text"]
        reply = (
            f"Voici : {text}"
            if text.startswith("Résultat")
            else 'Je calcule.\n```json\n{"tool": "calculator", "arguments": {"expression": "2+2"}}\n```'
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": reply}}]
            },
        )


def test_emulated_tools_for_models_without_function_calling():
    fake = NoToolsModel()
    with make_client(fake) as c:
        r = c.post(
            "/v1/chat/completions",
            headers=MASTER,
            json={**ASK, "model": "ollama/gemma3:12b", "gateway_tools": ["calculator"]},
        )
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "Voici : Résultat de l'outil : 2+2 = 4"
    second = fake.bodies[-1]
    assert "tools" not in second and second["messages"][0]["content"].startswith("Outils disponibles")
    assert [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "user"]
    assert json.loads(second["messages"][2]["content"])["tool"] == "calculator"


def test_emulated_parsing():
    resp = lambda text: {"choices": [{"message": {"content": text}}]}  # noqa: E731
    assert runner.parse_emulated(resp("pas d'outil"), {"calculator"}) == resp("pas d'outil")
    assert runner.parse_emulated(resp('{"tool": "inconnu"}'), {"calculator"}) == resp('{"tool": "inconnu"}')
    assert runner.parse_emulated(resp("{cassé"), {"calculator"})["choices"][0]["message"]["content"] == "{cassé"
    msgs = runner.emulated(
        [{"role": "user", "content": "a"}, {"role": "user", "content": [{"type": "text", "text": "b"}]}], []
    )
    assert msgs[0]["content"] == [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]  # sans outils : pas de guide


def test_repeated_action_refused():
    async def act(args):
        return ToolResult("ok")

    tool = _tool(act, confirm=True)
    approvals = runner.Approvals()
    replies = iter([_calls("act"), _calls("act"), _calls("act"), FINAL])
    texts = []

    async def drive():
        async def call(msgs, specs):
            return next(replies), "local"

        async for chunk in runner.run(
            call=call,
            tools={"act": tool},
            messages=[],
            approvals=approvals,
            identity="me",
            audit=lambda *a: None,
            max_steps=5,
        ):
            ev = chunk.get("x_gateway_tool") or {}
            if ev.get("type") == "approval":
                approvals.decide(ev["approval"], "me", True)
            if ev.get("type") == "result":
                texts.append(ev["text"])

    asyncio.run(drive())
    assert texts[:2] == ["ok", "ok"] and "deux fois de suite" in texts[2]


@pytest.mark.skipif(sys.platform != "win32", reason="contrôle du PC : Windows")
def test_pc_context_and_fresh_capture(monkeypatch):
    from PIL import Image

    from gateway.tools import pc

    monkeypatch.setattr(pc, "_windows", lambda: [(1, "Sans titre - Bloc-notes"), (2, "Lezard IA - Edge")])
    monkeypatch.setattr(pc, "_title", lambda h: "Lezard IA - Edge")
    monkeypatch.setattr(pc, "_elements", lambda: [])
    monkeypatch.setattr("PIL.ImageGrab.grab", lambda: Image.new("RGB", (1920, 1080)))
    pc._shot.clear()
    first = asyncio.run(pc.screenshot({}))
    assert "premier plan : « Lezard IA - Edge »" in first.text and "« Sans titre - Bloc-notes »" in first.text
    assert "toujours à jour" in pc.still_fresh()
    pc._capture()
    assert "n'a pas changé" in pc._context()
    monkeypatch.setattr(pc, "_check_failsafe", lambda: None)
    monkeypatch.setattr(pc, "_mouse_first", lambda kind: None)
    with pytest.raises(ToolError, match="Aucune fenêtre"):
        asyncio.run(pc.focus({"title": "calculatrice"}))


def test_fresh_capture_not_retaken():
    async def shot(args):
        return ToolResult("capture", image="data:image/jpeg;base64,AAAA")

    tool = Tool("look", "test", {"type": "object", "properties": {}}, shot, fresh=lambda: "toujours à jour")
    replies = iter([_calls("look"), _calls("look"), FINAL])
    texts = []

    async def drive():
        async def call(msgs, specs):
            return next(replies), "local"

        async for chunk in runner.run(
            call=call,
            tools={"look": tool},
            messages=[],
            approvals=runner.APPROVALS,
            identity="me",
            audit=lambda *a: None,
            max_steps=4,
        ):
            ev = chunk.get("x_gateway_tool") or {}
            if ev.get("type") == "result":
                texts.append(ev["text"])

    asyncio.run(drive())
    assert texts == ["capture", "Erreur : toujours à jour"]  # 1re capture de la réponse : toujours permise


@pytest.mark.skipif(sys.platform != "win32", reason="contrôle du PC : Windows")
def test_pc_zoom_targets(monkeypatch):
    from PIL import Image

    from gateway.tools import pc

    monkeypatch.setattr(pc, "_windows", lambda: [])
    monkeypatch.setattr(pc, "_title", lambda h: "")
    monkeypatch.setattr(pc, "_elements", lambda: [])
    monkeypatch.setattr("PIL.ImageGrab.grab", lambda: Image.new("RGB", (1920, 1080)))
    pc._shot.clear()
    pc._capture()
    with pytest.raises(ToolError, match="pc_zoom d'abord"):
        pc._target({"zoom": 1})
    res = asyncio.run(pc.zoom({"cell": "A1"}))  # coin : la vue reste dans l'écran
    assert res.image.startswith("data:image/jpeg") and "1 à 36" in res.text
    assert pc._shot["zoom"] == (0, 0, 240)
    assert pc._target({"zoom": 1}) == (20, 20) and pc._target({"zoom": 36}) == (220, 220)
    with pytest.raises(ToolError):
        pc._target({"zoom": 37})
    assert pc._capture() and pc._shot["zoom"] is None  # nouvelle capture : l'ancien zoom n'est plus valable


@pytest.mark.skipif(sys.platform != "win32", reason="contrôle du PC : Windows")
def test_pc_elements_set_of_mark(monkeypatch):
    from PIL import Image

    from gateway.tools import pc

    out = "﻿Button\tDémarrer\t0\t1032\t56\t48\nEdit\t\t100\t100\t400\t30\nligne illisible\n".encode()

    class Done:
        stdout = out

    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **kw: Done())
    monkeypatch.setattr("PIL.ImageGrab.grab", lambda: Image.new("RGB", (1920, 1080)))
    pc._shot.clear()
    pc._capture()
    assert [e["kind"] for e in pc._shot["elements"]] == ["bouton", "champ"]
    assert "[1] bouton « Démarrer »" in pc.elements_text() and "[2] champ « sans nom »" in pc.elements_text()
    assert pc._target({"element": 1}) == pytest.approx((28 * 2 / 3, 1056 * 2 / 3))
    for bad in (0, 3, "x"):
        with pytest.raises(ToolError):
            pc._target({"element": bad})

    def boom(*a, **kw):
        raise pc.subprocess.TimeoutExpired("powershell", 6)

    monkeypatch.setattr(pc.subprocess, "run", boom)
    pc._capture()
    assert pc._shot["elements"] == [] and "grille" in pc.elements_text()
    with pytest.raises(ToolError, match="cell"):
        pc._target({"element": 1})


def test_false_claim_flagged():
    async def act(args):
        return ToolResult("ok")

    async def drive(tools_):
        async def call(msgs, specs):
            return {"choices": [{"message": {"content": "J'ai ouvert Gmail dans votre navigateur."}}]}, "local"

        return [
            c
            async for c in runner.run(
                call=call,
                tools=tools_,
                messages=[],
                approvals=runner.APPROVALS,
                identity="me",
                audit=lambda *a: None,
                max_steps=2,
            )
        ]

    out = asyncio.run(drive({"act": _tool(act, confirm=True)}))
    assert "Aucune action n'a été faite sur le PC" in out[0]["choices"][0]["delta"]["content"]
    plain = Tool("calc", "t", {"type": "object", "properties": {}}, act)  # sans contrôle du PC : pas d'alerte
    assert "Aucune action" not in asyncio.run(drive({"calc": plain}))[0]["choices"][0]["delta"]["content"]


@pytest.mark.skipif(sys.platform != "win32", reason="contrôle du PC : Windows")
def test_pc_open_url(monkeypatch):
    from gateway.tools import pc

    opened = []
    monkeypatch.setattr(pc.os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(pc, "_check_failsafe", lambda: None)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(pc.asyncio, "sleep", lambda s: real_sleep(0))

    async def after():
        return ToolResult("ok")

    monkeypatch.setattr(pc, "_after", after)
    asyncio.run(pc.open_url({"url": "https://mail.google.com"}))
    assert opened == ["https://mail.google.com"]
    for bad in ("file:///C:/Windows", "javascript:alert(1)", 'https://x.fr/"a'):
        with pytest.raises(ToolError):
            asyncio.run(pc.open_url({"url": bad}))


def test_tools_master_key_only():
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(Scripted())),
        plans=PlanResolver({"gw-compte-0123456789abcdefghij": "pro"}, "free", master_key=KEY),
        store=DocumentStore(":memory:"),
    )
    user = {"Authorization": "Bearer gw-compte-0123456789abcdefghij"}
    with TestClient(app) as c:
        assert c.get("/v1/tools", headers=user).json()["data"] == []
        r = c.post("/v1/chat/completions", headers=user, json={**ASK, "gateway_tools": True})
        assert r.status_code == 403 and "clé maître" in r.json()["error"]["message"]


@pytest.mark.skipif(sys.platform != "win32", reason="contrôle du PC : Windows")
def test_mouse_first(monkeypatch):
    from gateway.tools import pc

    monkeypatch.setattr(pc, "_check_failsafe", lambda: None)
    pc._shot.update(elements=[{"kind": "bouton", "name": "OK"}], unchanged=False)
    pc._last.update(kind=None, refused=0)
    with pytest.raises(ToolError, match="SOURIS D'ABORD"):
        pc._mouse_first("keyboard")
    with pytest.raises(ToolError, match="clique d'abord dans le champ"):
        pc._mouse_first("type")
    pc._mouse_first("keyboard")  # 3e demande justifiée : permise
    pc._last.update(kind="click", refused=0)
    pc._mouse_first("type")  # après un clic dans le champ : écrire est permis
    pc._shot["unchanged"] = True
    pc._mouse_first("keyboard")  # le clic n'a rien fait : le clavier devient permis
    pc._shot.update(elements=[], unchanged=False)
    pc._last.update(kind="keyboard", refused=0)
    pc._mouse_first("keyboard")  # aucun élément détecté
    pc._shot.update(elements=[{"kind": "champ", "name": "Rechercher"}], unchanged=False)
    pc._last.update(kind="type", refused=0)
    pc._mouse_first("keyboard")  # Entrée juste après la frappe : valide la saisie
    pc._last.update(kind="command", refused=0)
    pc._mouse_first("type")  # application ou page qui vient de s'ouvrir : on peut y écrire
    pc._last.update(kind="keyboard", refused=0)
    with pytest.raises(tools.RuleRefusal):
        pc._mouse_first("keyboard")


class CallsPcTool(Scripted):
    def __call__(self, request):
        if request.url.path != "/v1/chat/completions":
            return super().__call__(request)
        body = json.loads(request.content)
        self.bodies.append(body)
        if body["messages"][-1]["role"] == "tool":
            msg = {"role": "assistant", "content": "fini"}
        else:
            msg = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "p1", "type": "function", "function": {"name": "pc_fake", "arguments": "{}"}}],
            }
        return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "stop", "message": msg}]})


def test_pc_tool_through_api_with_audit(monkeypatch):
    """Toutes plateformes : un outil de niveau pc factice traverse l'API (guide, journal d'audit)."""
    ran = []

    async def fake(args):
        ran.append(args)
        return ToolResult("fait")

    monkeypatch.setitem(
        tools.TOOLS,
        "pc_fake",
        Tool("pc_fake", "t", {"type": "object", "properties": {}}, fake, level="pc", confirm=False, local_only=True),
    )
    monkeypatch.setattr(tools, "pc_enabled", lambda: True)
    monkeypatch.setattr(runner, "PC_CONTEXT", 0)  # API compatible OpenAI (fenêtre agrandie : test suivant)
    fake_model = CallsPcTool()
    with make_client(fake_model) as c:
        assert c.post("/v1/chat/completions", headers=MASTER, json={**ASK, "gateway_tools": "oui"}).status_code == 400
        r = c.post("/v1/chat/completions", headers=MASTER, json={**ASK, "gateway_tools": ["pc_fake"]})
        audit = c.get("/v1/admin/audit", headers=MASTER)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "fini" and ran == [{}]
    assert fake_model.bodies[0]["messages"][0]["content"] == state.PLAN_PROMPT  # planificateur (24.2)
    assert fake_model.bodies[1]["messages"][0]["content"] == tools.PC_GUIDE
    if audit.status_code == 200:
        assert "outil" in audit.text


def test_small_error_paths(monkeypatch):
    calc = lambda e: asyncio.run(basic.calculate({"expression": e})).text  # noqa: E731
    assert calc("-pi + e") == f"-pi + e = {-3.141592653589793 + 2.718281828459045}"
    for bad in ("", "1/0", "x" * 301, "1 +"):
        with pytest.raises(ToolError):
            calc(bad)
    with pytest.raises(ToolError, match="introuvable"):
        asyncio.run(web._public_ip("nom-qui-n-existe-pas.invalid"))
    with pytest.raises(ToolError, match="Adresse refusée"):
        asyncio.run(web._public_ip("localhost"))

    def handler(request):
        if request.url.path == "/boucle":
            return httpx.Response(302, headers={"location": "/boucle"})
        if request.url.path == "/panne":
            raise httpx.ConnectError("panne")
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>rien</p>")

    _fake_web(monkeypatch, handler)
    for path, err in (("/boucle", "redirections"), ("/panne", "Connexion impossible")):
        with pytest.raises(ToolError, match=err):
            asyncio.run(web.web_fetch({"url": f"https://exemple.fr{path}"}))
    assert "Aucun résultat" in asyncio.run(web.web_search({"query": "rien"})).text
    assert runner.parse_emulated({"choices": [{"message": {"content": "{pas: du json}"}}]}, {"x"})


def test_virtual_key_tools_permission(monkeypatch):
    ran = []

    async def fake(args):
        ran.append(args)
        return ToolResult("fait")

    monkeypatch.setitem(
        tools.TOOLS,
        "pc_fake",
        Tool("pc_fake", "t", {"type": "object", "properties": {}}, fake, level="pc", confirm=False, local_only=True),
    )
    monkeypatch.setattr(tools, "pc_enabled", lambda: True)
    monkeypatch.setattr(runner, "PC_CONTEXT", 0)  # faux Ollama : API compatible OpenAI seulement
    with make_client(CallsPcTool()) as c:

        def key(perms):
            r = c.post("/v1/keys", headers=MASTER, json={"name": "k", "permissions": perms})
            assert r.status_code in (200, 201), r.text
            return {"Authorization": f"Bearer {r.json()['key']}"}

        assert (
            c.post("/v1/keys", headers=MASTER, json={"name": "k", "permissions": {"tools": "tout"}}).status_code == 400
        )
        none, web_key, pc_key = key({}), key({"tools": "web"}), key({"tools": "pc"})
        names = lambda h: {t["name"] for t in c.get("/v1/tools", headers=h).json()["data"]}  # noqa: E731
        assert names(none) == set()
        assert "web_fetch" in names(web_key) and "pc_fake" not in names(web_key)
        assert "pc_fake" in names(pc_key)
        r = c.post("/v1/chat/completions", headers=pc_key, json={**ASK, "gateway_tools": ["pc_fake"]})
    assert r.status_code == 200 and ran == [{}]


def test_empty_reply_is_nudged():
    """Petit modèle « pensant » : réponse vide (tout dans la réflexion) relancée, puis on rend la main."""
    empty = {"choices": [{"message": {"content": "", "reasoning": "je réfléchis…"}}]}

    async def drive(replies):
        seen = []

        async def call(msgs, specs):
            seen.append(msgs)
            return next(replies), "local"

        chunks = [
            c
            async for c in runner.run(
                call=call,
                tools={},
                messages=[{"role": "user", "content": "go"}],
                approvals=runner.Approvals(),
                identity="me",
                audit=lambda *a: None,
                max_steps=6,
            )
        ]
        text = "".join((c["choices"][0]["delta"] or {}).get("content") or "" for c in chunks if c.get("choices"))
        return text, seen

    text, seen = asyncio.run(drive(iter([empty, FINAL])))
    assert text == "fini" and seen[1][-1] == {"role": "user", "content": runner.NUDGE}
    text, seen = asyncio.run(drive(iter([empty] * 3)))
    assert text == "" and len(seen) == 1 + runner.MAX_NUDGES


class NativeOllama(Scripted):
    """Ollama : /api/chat (format natif) pour les tâches sur le PC, /v1/chat/completions pour le reste."""

    def __init__(self):
        super().__init__()
        self.native = []

    def __call__(self, request):
        if request.url.path != "/api/chat":
            return super().__call__(request)
        body = json.loads(request.content)
        self.native.append(body)
        if body["messages"][-1]["role"] == "tool":
            msg = {"role": "assistant", "content": "fini"}
        else:
            msg = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "pc_fake", "arguments": {}}}],
            }
        return httpx.Response(200, json={"model": body["model"], "message": msg, "done": True, "done_reason": "stop"})


def test_pc_run_asks_ollama_for_a_bigger_context(monkeypatch):
    async def fake(args):
        return ToolResult("fait")

    monkeypatch.setitem(
        tools.TOOLS,
        "pc_fake",
        Tool("pc_fake", "t", {"type": "object", "properties": {}}, fake, level="pc", confirm=False, local_only=True),
    )
    monkeypatch.setattr(tools, "pc_enabled", lambda: True)
    model = NativeOllama()
    with make_client(model) as c:
        r = c.post("/v1/chat/completions", headers=MASTER, json={**ASK, "gateway_tools": ["pc_fake"]})
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "fini"
        assert [b["options"]["num_ctx"] for b in model.native] == [runner.PC_CONTEXT] * 3  # plan + 2 tours
        assert all("_num_ctx" not in b for b in model.native)
        # hors contrôle du PC : requête habituelle, et la directive d'un client est ignorée
        r = c.post("/v1/chat/completions", headers=MASTER, json={**ASK, "_num_ctx": 999999})
        assert r.status_code == 200 and len(model.native) == 3 and "_num_ctx" not in model.bodies[-1]


def test_failsafe_stops_while_the_model_thinks():
    """Souris dans le coin pendant que le modèle réfléchit : arrêt immédiat, sans attendre l'action suivante."""
    approvals = runner.Approvals()
    state = {"corner": False, "cancelled": False}

    async def act(args):
        return ToolResult("ok")

    tool = _tool(act, confirm=True)

    async def drive():
        async def call(msgs, specs):
            state["corner"] = True  # l'utilisateur pousse la souris dans le coin pendant la réflexion
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise
            return FINAL, "local"

        out = []
        async for c in runner.run(
            call=call,
            tools={"act": tool},
            messages=[],
            approvals=approvals,
            identity="me",
            audit=lambda *a: None,
            max_steps=3,
            failsafe=lambda: "Arrêt d'urgence" if state["corner"] else None,
        ):
            out.append(c)
        return out

    chunks = asyncio.run(asyncio.wait_for(drive(), 5))
    text = "".join((c["choices"][0]["delta"] or {}).get("content") or "" for c in chunks if c.get("choices"))
    assert text == "⛔ Arrêt d'urgence" and state["cancelled"]
    assert not runner.PC_LOCK.locked()


def test_failsafe_ignored_without_pc_tools():
    async def drive():
        async def call(msgs, specs):
            return FINAL, "local"

        return [
            c
            async for c in runner.run(
                call=call,
                tools={},
                messages=[],
                approvals=runner.Approvals(),
                identity="me",
                audit=lambda *a: None,
                max_steps=2,
                failsafe=lambda: "Arrêt d'urgence",  # calculatrice, web : pas concernés
            )
        ]

    assert "fini" in json.dumps(asyncio.run(drive()))


def test_calculator_refuses_results_too_big():
    """((9**999)**999)**999 bloquait toute la gateway (calcul synchrone dans la boucle) : refusé avant calcul."""
    calc = lambda e: asyncio.run(basic.calculate({"expression": e})).text  # noqa: E731
    for bad in ("((9**999)**999)**999", "(9**999)**999", "factorial(1000)**999", "*".join(["(9**999)"] * 5)):
        with pytest.raises(ToolError):
            calc(bad)
    assert calc("2**1000").startswith("2**1000 = 1071508607186267320948425049060001810561")
    assert calc("(-1)**999") == "(-1)**999 = -1"


def test_pc_permission_granted_by_master_key_only(monkeypatch):
    """Un autre compte (GATEWAY_KEYS, compte nommé) ne délègue pas le contrôle du PC hôte à ses clés virtuelles."""
    from gateway.plans import account_identity

    async def fake(args):
        return ToolResult("fait")

    monkeypatch.setitem(
        tools.TOOLS,
        "pc_fake",
        Tool("pc_fake", "t", {"type": "object", "properties": {}}, fake, level="pc", confirm=False, local_only=True),
    )
    monkeypatch.setattr(tools, "pc_enabled", lambda: True)
    other = "compte-ami-0123456789abcdefghijklmnop"
    store = DocumentStore(":memory:")
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(CallsPcTool())),
        plans=PlanResolver({other: "pro"}, "free", master_key=KEY),
        store=store,
    )
    friend = {"Authorization": f"Bearer {other}"}
    with TestClient(app) as c:
        r = c.post("/v1/keys", headers=friend, json={"name": "k", "permissions": {"tools": "pc"}})
        assert r.status_code == 403 and "clé maître" in r.json()["error"]["message"]
        web_key = c.post("/v1/keys", headers=friend, json={"name": "k", "permissions": {"tools": "web"}}).json()
        r = c.patch(f"/v1/keys/{web_key['id']}", headers=friend, json={"permissions": {"tools": "pc"}})
        assert r.status_code == 403
        # clé déjà en base avec « pc » (créée avant cette règle, base restaurée…) : ramenée à « web »
        store.update_key(account_identity(other), web_key["id"], {"permissions": {"tools": "pc"}})
        h = {"Authorization": f"Bearer {web_key['key']}"}
        names = {t["name"] for t in c.get("/v1/tools", headers=h).json()["data"]}
        assert "web_fetch" in names and "pc_fake" not in names
        r = c.post("/v1/chat/completions", headers=h, json={**ASK, "gateway_tools": ["pc_fake"]})
        assert r.status_code == 403
        # la clé maître, elle, peut le déléguer
        assert c.post("/v1/keys", headers=MASTER, json={"name": "k", "permissions": {"tools": "pc"}}).status_code == 201


def test_web_search_through_your_own_searxng(monkeypatch):
    """SEARXNG_URL : la recherche part vers votre SearXNG (même sur le réseau local), sans repli sur un autre moteur."""
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "refuse.lan":
            return httpx.Response(403)
        if request.url.host == "casse.lan":
            return httpx.Response(200, text="<html>pas du json</html>")
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Titre <b>A</b>", "url": "https://ex.fr/a", "content": "Extrait &amp; suite"},
                    {"title": "Piège", "url": "javascript:alert(1)", "content": "x"},
                ]
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(web.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("SEARXNG_URL", "http://192.168.1.20:8080/")
    res = asyncio.run(web.web_search({"query": "lézard"})).text
    assert "Titre A — https://ex.fr/a" in res and "Extrait & suite" in res and "javascript" not in res
    q = calls[0].url
    assert str(q).startswith("http://192.168.1.20:8080/search?") and q.params["format"] == "json"
    monkeypatch.setenv("SEARXNG_URL", "http://refuse.lan:8080")
    with pytest.raises(ToolError, match="settings.yml"):
        asyncio.run(web.web_search({"query": "x"}))
    monkeypatch.setenv("SEARXNG_URL", "http://casse.lan:8080")
    with pytest.raises(ToolError, match="JSON"):
        asyncio.run(web.web_search({"query": "x"}))
    monkeypatch.setenv("SEARXNG_URL", "ftp://x")
    with pytest.raises(ToolError, match="invalide"):
        asyncio.run(web.web_search({"query": "x"}))
    assert all(c.url.host != "html.duckduckgo.com" for c in calls)  # jamais de repli


def test_web_tools_can_be_switched_off_at_install(monkeypatch):
    from types import SimpleNamespace

    admin_ctx = SimpleNamespace(is_admin=True, plan="pro", permissions={})
    monkeypatch.delenv("TOOLS_WEB", raising=False)
    assert any(t.level == "web" for t in tools.available(admin_ctx).values())
    monkeypatch.setenv("TOOLS_WEB", "false")
    assert not any(t.level == "web" for t in tools.available(admin_ctx).values())
