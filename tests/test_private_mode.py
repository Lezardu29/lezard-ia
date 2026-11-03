"""Mode privé d'une conversation : seules les IA locales (ce PC, réseau local) reçoivent les messages."""

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
MSG = [{"role": "user", "content": "Mes données confidentielles"}]


class Upstream:
    def __init__(self, local_online=True):
        self.local_online = local_online
        self.chats = []  # hôtes ayant reçu un message

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "ollama" and not self.local_online:
            raise httpx.ConnectError("hors ligne")
        if path.endswith("/models"):
            ids = ["llama3.1:8b"] if host == "ollama" else ["gpt-4o-mini"]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        if path.endswith("/chat/completions"):
            self.chats.append(host)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                },
            )
        return httpx.Response(404)


def make(up):
    return create_app(
        [
            ProviderConfig("ollama", "openai", "http://ollama:11434/v1", "local"),
            ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="sk-test-123456"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )


def test_private_conversation_stays_local():
    up = Upstream()
    with TestClient(make(up)) as c:
        for mode in ("auto", "auto/balanced", "auto/speed"):
            r = c.post("/v1/chat/completions", headers=MASTER, json={"model": mode, "messages": MSG, "private": True})
            assert r.status_code == 200 and r.json()["x_gateway"]["hosting"] == "local", mode
        # en-tête équivalent (dictée, clients OpenAI)
        r = c.post(
            "/v1/chat/completions",
            headers={**MASTER, "X-Gateway-Private": "true"},
            json={"model": "auto/balanced", "messages": MSG},
        )
        assert r.status_code == 200 and r.json()["x_gateway"]["provider"] == "ollama"
        # IA cloud demandée explicitement, ou en secours : refusée, rien n'est envoyé
        r = c.post(
            "/v1/chat/completions",
            headers=MASTER,
            json={"model": "openai/gpt-4o-mini", "messages": MSG, "private": True},
        )
        assert r.status_code == 403 and "Mode privé" in r.json()["error"]["message"]
        r = c.post(
            "/v1/chat/completions",
            headers=MASTER,
            json={"model": "ollama/llama3.1:8b", "messages": MSG, "private": True, "fallbacks": ["openai/gpt-4o-mini"]},
        )
        assert r.status_code == 403
        assert set(up.chats) == {"ollama"}
        # hors mode privé, le cloud reste utilisable
        r = c.post("/v1/chat/completions", headers=MASTER, json={"model": "openai/gpt-4o-mini", "messages": MSG})
        assert r.status_code == 200 and up.chats[-1] == "openai"
        assert (
            c.post(
                "/v1/chat/completions", headers=MASTER, json={"model": "auto", "messages": MSG, "private": "oui"}
            ).status_code
            == 400
        )


def test_private_without_local_ai_refuses_instead_of_using_cloud():
    up = Upstream(local_online=False)
    with TestClient(make(up)) as c:
        r = c.post("/v1/chat/completions", headers=MASTER, json={"model": "auto", "messages": MSG, "private": True})
        assert r.status_code == 503 and "aucune IA locale" in r.json()["error"]["message"]
        assert up.chats == []


def test_cloud_only_key_cannot_go_private():
    up = Upstream()
    with TestClient(make(up)) as c:
        key = c.post("/v1/keys", headers=MASTER, json={"name": "cloud", "permissions": {"hosting": "cloud"}}).json()[
            "key"
        ]
        h = {"Authorization": f"Bearer {key}"}
        assert c.get("/v1/me", headers=h).json()["routing_rights"] == {"hosting": "cloud"}
        r = c.post("/v1/chat/completions", headers=h, json={"model": "auto", "messages": MSG, "private": True})
        assert r.status_code == 403 and "limitée aux IA cloud" in r.json()["error"]["message"]


def test_private_mode_kept_with_gateway_tools():
    """/web, /pc : le mode Privé (dans le corps, comme l'envoie le chat) vaut pour chaque tour du modèle."""
    up = Upstream()
    with TestClient(make(up)) as c:
        body = {"model": "openai/gpt-4o-mini", "messages": MSG, "private": True, "gateway_tools": ["calculator"]}
        r = c.post("/v1/chat/completions", headers=MASTER, json=body)
        assert r.status_code == 403 and "Mode privé" in r.json()["error"]["message"]
        for mode in ("auto", "auto/speed"):
            r = c.post("/v1/chat/completions", headers=MASTER, json={**body, "model": mode})
            assert r.status_code == 200, r.text
    assert up.chats and set(up.chats) == {"ollama"}


def test_screen_capture_never_leaves_for_the_cloud(monkeypatch):
    """Capture prise par une IA locale : les tours suivants (routage « auto », secours) restent locaux."""
    import json

    from gateway import tools
    from gateway.tools import Tool, ToolResult

    async def shot(args):
        return ToolResult("capture", image="data:image/jpeg;base64,AAAA")

    monkeypatch.setitem(
        tools.TOOLS, "pc_shot", Tool("pc_shot", "t", {"type": "object", "properties": {}}, shot, level="pc")
    )
    monkeypatch.setattr(tools, "pc_enabled", lambda: True)
    from gateway.tools import runner

    monkeypatch.setattr(runner, "PC_CONTEXT", 0)
    seen, local = [], "llama3.1:8b"

    def upstream(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path.endswith("/models"):
            ids = [local] if host == "ollama" else ["gpt-4o-mini"]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        if path.endswith("/chat/completions"):
            body = json.loads(request.content)
            seen.append((host, any(isinstance(m.get("content"), list) for m in body["messages"])))
            msg = {"role": "assistant", "content": "fini"}
            # 1er tour avec outils (le planificateur, avant, n'en propose pas) : capture demandée
            if body.get("tools") and not any(m.get("role") == "tool" for m in body["messages"]):
                call = {"id": "s", "type": "function", "function": {"name": "pc_shot", "arguments": "{}"}}
                msg = {"role": "assistant", "content": "", "tool_calls": [call]}
            return httpx.Response(200, json={"choices": [{"index": 0, "message": msg}], "usage": {}})
        return httpx.Response(404)

    with TestClient(make(upstream)) as c:
        body = {"model": "auto", "messages": MSG, "gateway_tools": ["pc_shot"]}
        r = c.post("/v1/chat/completions", headers=MASTER, json=body)
    # aucune IA locale ne lit les images : refus plutôt que l'écran envoyé à gpt-4o-mini
    assert r.status_code == 503 and seen and not any(with_image for _, with_image in seen)
    seen.clear()
    local = "qwen2.5vl:7b"  # IA locale avec vision et outils : la tâche continue sur elle
    with TestClient(make(upstream)) as c:
        r = c.post("/v1/chat/completions", headers=MASTER, json=body)
    assert r.status_code == 200, r.text
    assert any(with_image for _, with_image in seen)
    assert all(host == "ollama" for host, with_image in seen if with_image), seen
