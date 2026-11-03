"""Pont vers les modèles locaux (7.4) : agent, relais WebSocket, isolation entre comptes."""

import asyncio
import base64
import json
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gateway.bridge import path_allowed, validate_name, BridgeError
from gateway.bridge_agent import Relay, run, ws_url
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.serve import WS_IMPL
from gateway.store import DocumentStore

ACCOUNT, OTHER = {"Authorization": "Bearer compte"}, {"Authorization": "Bearer autre"}


# ---------- briques ----------


def test_paths_and_names():
    assert path_allowed("POST", "/chat/completions") and path_allowed("GET", "/models")
    for method, path in (
        ("DELETE", "/models"),
        ("POST", "/api/delete"),
        ("POST", "/chat/completions/../../api/pull"),
        ("GET", "//evil/models"),
        ("POST", "/api/generate"),
    ):
        assert not path_allowed(method, path)
    assert validate_name("maison", set()) == "maison"
    for bad in ("M", "1abc", "a b", "x" * 40, None, "ollama/x"):
        with pytest.raises(BridgeError):
            validate_name(bad, set())
    with pytest.raises(BridgeError):
        validate_name("ollama", {"ollama"})
    assert ws_url("https://gw.example.com/") == "wss://gw.example.com/v1/bridge/connect"
    assert ws_url("http://127.0.0.1:8000") == "ws://127.0.0.1:8000/v1/bridge/connect"


class FakeConn:
    """Connexion côté agent : trames de la gateway en entrée, trames envoyées capturées."""

    def __init__(self, frames):
        self.incoming, self.sent = list(frames), []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0.05)
        if not self.incoming:
            await asyncio.sleep(0.2)  # laisse finir les relais en cours
            raise StopAsyncIteration
        return json.dumps(self.incoming.pop(0))


def test_relay_forwards_inference_only():
    seen = []

    def local(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.headers.get("authorization")))
        return httpx.Response(
            200, content=b"data: {}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"}
        )

    body = base64.b64encode(b'{"model":"m"}').decode()
    conn = FakeConn(
        [
            {
                "type": "request",
                "id": "1",
                "method": "POST",
                "path": "/chat/completions",
                "headers": {"content-type": "application/json", "authorization": "Bearer fuite"},
                "body": body,
            },
            {"type": "request", "id": "2", "method": "POST", "path": "/api/delete", "headers": {}, "body": ""},
        ]
    )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(local)) as client:
            await Relay(conn, client, "http://localhost:11434/v1", "cle-locale").serve()

    asyncio.run(go())
    # la clé vient de l'agent, jamais de la gateway ; la route d'administration n'est pas relayée
    assert seen == [("POST", "http://localhost:11434/v1/chat/completions", "Bearer cle-locale")]
    by_id = {}
    for f in conn.sent:
        by_id.setdefault(f.get("id"), []).append(f)
    assert conn.sent[0] == {"type": "hello", "agent": "1"}
    assert [f["type"] for f in by_id["1"]][0] == "response" and by_id["1"][-1]["type"] == "end"
    data = b"".join(base64.b64decode(f["data"]) for f in by_id["1"] if f["type"] == "chunk")
    assert data.endswith(b"[DONE]\n\n")
    assert by_id["2"][0]["type"] == "error" and "refusée" in by_id["2"][0]["message"]


# ---------- de bout en bout : uvicorn réel + agent réel ----------


def fake_ollama(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"object": "list", "data": [{"id": "llama3.1:8b"}]})
    payload = json.loads(request.content)
    if payload.get("stream"):
        sse = (
            "".join(
                f"data: {json.dumps(c)}\n\n"
                for c in [
                    {
                        "id": "c",
                        "object": "chat.completion.chunk",
                        "model": "llama3.1:8b",
                        "choices": [
                            {"index": 0, "delta": {"role": "assistant", "content": "Bonjour "}, "finish_reason": None}
                        ],
                    },
                    {
                        "id": "c",
                        "object": "chat.completion.chunk",
                        "model": "llama3.1:8b",
                        "choices": [{"index": 0, "delta": {"content": "depuis la maison"}, "finish_reason": "stop"}],
                    },
                    {
                        "id": "c",
                        "object": "chat.completion.chunk",
                        "model": "llama3.1:8b",
                        "choices": [],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 4},
                    },
                ]
            )
            + "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
    return httpx.Response(
        200,
        json={
            "id": "x",
            "object": "chat.completion",
            "model": "llama3.1:8b",
            "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Réponse locale"}}
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        },
    )


class Server:
    def __init__(self, app):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", ws=WS_IMPL))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)


class Agent:
    """Agent de pont dans son propre thread / sa propre boucle."""

    def __init__(self, url, token):
        self.url, self.token, self.result = url, token, None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._main, daemon=True)

    def _main(self):
        asyncio.set_event_loop(self.loop)
        client = httpx.AsyncClient(transport=httpx.MockTransport(fake_ollama))
        self.task = self.loop.create_task(
            run(self.url, self.token, "http://localhost:11434/v1", client=client, max_attempts=3)
        )
        try:
            self.result = self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            self.result = "annulé"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(10)


def wait_for(fn, timeout=8.0):
    t = time.time()
    while time.time() - t < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return None


def make_app():
    return create_app(
        [],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        plans=PlanResolver({"compte": "free", "autre": "free"}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )


def chat(model, **kw):
    return {"model": model, "messages": [{"role": "user", "content": "Bonjour"}], **kw}


def test_bridge_end_to_end():
    with Server(make_app()) as srv, httpx.Client(base_url=srv.url, timeout=20) as c:
        r = c.post("/v1/bridges", headers=ACCOUNT, json={"name": "maison"})
        assert r.status_code == 201
        created = r.json()
        token = created["token"]
        assert token.startswith("gwb_") and created["online"] is False
        assert f"--gateway {srv.url} --token {token}" in created["command"]
        assert c.post("/v1/bridges", headers=OTHER, json={"name": "maison"}).status_code == 409  # nom pris
        # hors ligne : le modèle explicite échoue proprement
        r = c.post("/v1/chat/completions", headers=ACCOUNT, json=chat("maison/llama3.1:8b"))
        assert r.status_code == 503 and "hors ligne" in r.text

        agent = Agent(srv.url, token).start()
        try:
            listed = wait_for(lambda: [b for b in c.get("/v1/bridges", headers=ACCOUNT).json()["data"] if b["online"]])
            assert listed and listed[0]["models"] == ["llama3.1:8b"]
            assert "token" not in listed[0]

            r = c.post("/v1/chat/completions", headers=ACCOUNT, json=chat("auto"))
            assert r.status_code == 200, r.text
            out = r.json()
            assert out["choices"][0]["message"]["content"] == "Réponse locale"
            assert out["x_gateway"]["provider"] == "maison" and out["x_gateway"]["hosting"] == "local"

            with c.stream(
                "POST", "/v1/chat/completions", headers=ACCOUNT, json=chat("maison/llama3.1:8b", stream=True)
            ) as s:
                text = "".join(s.iter_text())
            deltas = [
                json.loads(line[5:])["choices"][0]["delta"].get("content", "")
                for line in text.splitlines()
                if line.startswith("data: {") and '"choices": [{' in line
            ]
            assert "".join(deltas) == "Bonjour depuis la maison"

            # un autre compte ne voit ni n'utilise le pont
            assert c.post("/v1/chat/completions", headers=OTHER, json=chat("maison/llama3.1:8b")).status_code == 404
            assert c.post("/v1/chat/completions", headers=OTHER, json=chat("llama3.1:8b")).status_code == 404
            assert all(m["owned_by"] != "maison" for m in c.get("/v1/models", headers=OTHER).json()["data"])
            assert "maison" not in c.get("/v1/providers", headers=OTHER).json()
            assert "maison" in c.get("/v1/providers", headers=ACCOUNT).json()
            assert c.get("/v1/bridges", headers=OTHER).json()["data"] == []
            assert c.delete(f"/v1/bridges/{created['id']}", headers=OTHER).status_code == 404

            # suppression : l'agent est déconnecté définitivement (code de sortie 2)
            assert c.delete(f"/v1/bridges/{created['id']}", headers=ACCOUNT).json()["deleted"] is True
            assert wait_for(lambda: agent.result == 2)
            assert c.post("/v1/chat/completions", headers=ACCOUNT, json=chat("maison/llama3.1:8b")).status_code == 404
        finally:
            agent.stop()


def test_bad_token_is_refused():
    with TestClient(make_app()) as c:
        for headers in ({}, {"Authorization": "Bearer gwb_inconnu"}, ACCOUNT):
            with c.websocket_connect("/v1/bridge/connect", headers=headers) as ws:
                with pytest.raises(WebSocketDisconnect) as e:
                    ws.receive_text()
                assert e.value.code == 4401
        assert c.post("/v1/bridges", headers=ACCOUNT, json={"name": "Maison!"}).status_code == 400
        assert c.post("/v1/bridges", headers=ACCOUNT, json={"name": "auto"}).status_code == 409
        k = c.post("/v1/keys", headers=ACCOUNT, json={"name": "app"}).json()["key"]
        assert c.post("/v1/bridges", headers={"Authorization": f"Bearer {k}"}, json={"name": "x1"}).status_code == 403
