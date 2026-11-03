"""Journal d'audit du contrôle du PC (21.4) : décision, résultat et capture d'écran chiffrée, purgée après N jours."""

import base64
import json

import httpx

from gateway import tools
from gateway.tools import Tool, ToolResult, runner

from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

from .test_gateway_tools import ASK, KEY, MASTER, Scripted

STORE = {}


def make_client(fake):
    STORE["db"] = DocumentStore(":memory:")
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        plans=PlanResolver({}, "free", master_key=KEY),
        store=STORE["db"],
    )
    return TestClient(app)


class CallsPcTool(Scripted):
    """Appelle pc_fake une fois (au premier tour qui propose des outils), puis répond."""

    def __call__(self, request):
        if request.url.path != "/v1/chat/completions":
            return super().__call__(request)
        body = json.loads(request.content)
        msg = {"role": "assistant", "content": "fini"}
        if body.get("tools") and not any(m.get("role") == "tool" for m in body["messages"]):
            call = {"id": "p1", "type": "function", "function": {"name": "pc_fake", "arguments": "{}"}}
            msg = {"role": "assistant", "content": "", "tool_calls": [call]}
        return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "stop", "message": msg}]})


JPEG = b"\xff\xd8\xff\xe0 capture secrete de l'ecran"
URI = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()


def _setup(monkeypatch, image=URI):
    async def fake(args):
        return ToolResult("fait", image=image)

    monkeypatch.setitem(
        tools.TOOLS,
        "pc_fake",
        Tool("pc_fake", "t", {"type": "object", "properties": {}}, fake, level="pc", confirm=False, local_only=True),
    )
    monkeypatch.setattr(tools, "pc_enabled", lambda: True)
    monkeypatch.setattr(runner, "PC_CONTEXT", 0)  # faux Ollama : API compatible OpenAI seulement


def _tool_rows(c):
    rows = c.get("/v1/admin/audit?action=outil", headers=MASTER).json()["data"]
    return [r for r in rows if r["action"] == "outil"]


def test_action_logged_once_with_result_and_encrypted_capture(monkeypatch):
    _setup(monkeypatch)
    with make_client(CallsPcTool()) as c:
        r = c.post("/v1/chat/completions", headers=MASTER, json={**ASK, "gateway_tools": ["pc_fake"]})
        assert r.status_code == 200
        rows = _tool_rows(c)
        by_target = {}
        for row in rows:
            by_target.setdefault(row["target"], []).append(row)
        [action] = by_target["pc_fake"]  # une seule entrée : décision et résultat ensemble
        assert action["detail"]["action"] == "sans validation (lecture de l'écran)"
        assert action["detail"]["résultat"] == "fait" and action["capture"] is True
        tasks = sorted(row["detail"]["action"] for row in by_target["tâche"])
        assert tasks == ["début", "fin"]
        assert all("Combien font 2+2" in row["detail"]["arguments"] for row in by_target["tâche"])

        # chiffrée en base : ni l'image ni son encodage n'y figurent en clair
        token = _store(c).get_capture(action["id"])
        assert token and base64.b64encode(JPEG).decode()[:20] not in token

        got = c.get(f"/v1/admin/audit/{action['id']}/capture", headers=MASTER)
        assert got.status_code == 200 and got.content == JPEG and got.headers["content-type"] == "image/jpeg"
        assert got.headers["cache-control"] == "no-store"
        assert c.get(f"/v1/admin/audit/{action['id']}/capture").status_code in (401, 403)  # clé maître seulement
        assert c.get("/v1/admin/audit/999999/capture", headers=MASTER).status_code == 404
        viewed = c.get("/v1/admin/audit?action=audit", headers=MASTER).json()["data"]
        assert any(v["action"] == "audit.capture" for v in viewed)  # consulter une capture est journalisé


def _store(c):
    return STORE["db"]


def test_purge_and_retention_zero(monkeypatch):
    _setup(monkeypatch)
    with make_client(CallsPcTool()) as c:
        c.post("/v1/chat/completions", headers=MASTER, json={**ASK, "gateway_tools": ["pc_fake"]})
        [action] = [r for r in _tool_rows(c) if r["target"] == "pc_fake"]
        store = _store(c)
        assert store.purge_captures(0) == 0 and store.get_capture(action["id"])  # trop récente
        assert store.purge_captures(10**12) == 1 and store.get_capture(action["id"]) is None
        assert [r for r in _tool_rows(c) if r["target"] == "pc_fake"][0]["capture"] is False  # l'entrée reste

        monkeypatch.setenv("AUDIT_CAPTURE_DAYS", "0")  # aucune capture gardée
        c.post("/v1/chat/completions", headers=MASTER, json={**ASK, "gateway_tools": ["pc_fake"]})
        assert not any(r["capture"] for r in _tool_rows(c))


def test_purged_audit_entry_takes_its_capture(monkeypatch):
    _setup(monkeypatch)
    with make_client(CallsPcTool()) as c:
        c.post("/v1/chat/completions", headers=MASTER, json={**ASK, "gateway_tools": ["pc_fake"]})
        [action] = [r for r in _tool_rows(c) if r["target"] == "pc_fake"]
        store = _store(c)
        store.purge_audit(10**12)
        assert store.get_capture(action["id"]) is None  # ON DELETE CASCADE


def test_refusal_logged_at_once_without_capture():
    events = []

    async def act(args):
        raise AssertionError("jamais exécutée")

    tool = Tool("pc_act", "t", {"type": "object", "properties": {}}, act, level="pc", confirm=True)
    replies = iter(
        [
            {"choices": [{"message": {"content": "", "tool_calls": [{"id": "a", "function": {"name": "pc_act"}}]}}]},
            {"choices": [{"message": {"content": "fini"}}]},
        ]
    )

    async def drive():
        async def call(msgs, specs):
            return next(replies), "local"

        async for chunk in runner.run(
            call=call,
            tools={"pc_act": tool},
            messages=[],
            approvals=runner.APPROVALS,
            identity="refus-audit",
            audit=lambda *a: events.append(a),
            max_steps=3,
        ):
            ev = chunk.get("x_gateway_tool") or {}
            if ev.get("type") == "approval":
                runner.APPROVALS.decide(ev["approval"], "refus-audit", False)

    import asyncio

    asyncio.run(drive())
    assert events == [("pc_act", {}, "refusée")]  # ni résultat ni capture
