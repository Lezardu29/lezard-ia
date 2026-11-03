"""Appel d'outils (function calling) avec les modèles locaux (16.2) : capacités lues sur Ollama, routage, échanges
complets (appel -> résultat -> réponse) en flux et hors flux, via /v1/chat/completions et /v1/messages."""

import json

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.providers.openai_compat import ToolCallStream, normalize_tool_calls
from gateway.registry import infer_local
from gateway.store import DocumentStore

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "meteo",
            "description": "Météo d'une ville",
            "parameters": {"type": "object", "properties": {"ville": {"type": "string"}}, "required": ["ville"]},
        },
    }
]
ANTHROPIC_TOOLS = [
    {"name": "meteo", "description": "Météo d'une ville", "input_schema": TOOLS[0]["function"]["parameters"]}
]
QUESTION = {"role": "user", "content": "Quel temps fait-il à Brest ?"}


class FakeOllama:
    """Ollama 0.6+ : /api/show annonce les capacités. Ses écarts connus à l'API OpenAI sont reproduits : appel
    d'outil terminé par finish_reason « stop », sans id ni index, arguments en objet."""

    def __init__(self, caps=True, models=("llama3.1:8b", "gemma3:12b")):
        self.caps, self.models, self.bodies = caps, list(models), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if path == "/api/show":
            name = json.loads(request.content)["model"]
            show = {"model_info": {"llama.context_length": 131072}, "parameters": ""}
            if self.caps:
                show["capabilities"] = ["completion", "tools"] if name.startswith("llama") else ["completion", "vision"]
            return httpx.Response(200, json=show)
        body = json.loads(request.content)
        self.bodies.append(body)
        last = body["messages"][-1]
        if last["role"] == "tool":
            text = f"D'après l'outil : {last['content']}"
            return self._text(body, text)
        if body.get("tools"):
            return self._call(body)
        return self._text(body, "Bonjour.")

    def _text(self, body, text):
        usage = {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38}
        if body.get("stream"):
            chunks = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": w + " "}}]} for w in text.split(" ")
            ]
            chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            chunks.append({"choices": [], "usage": usage})
            return httpx.Response(200, text="".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n")
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
                "usage": usage,
            },
        )

    def _call(self, body):
        usage = {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}
        if body.get("stream"):
            chunks = [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [{"function": {"name": "meteo", "arguments": '{"ville": '}}],
                            },
                        }
                    ]
                },
                {"choices": [{"index": 0, "delta": {"tool_calls": [{"function": {"arguments": '"Brest"}'}}]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": usage},
            ]
            return httpx.Response(200, text="".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{"function": {"name": "meteo", "arguments": {"ville": "Brest"}}}],
                        },
                    }
                ],
                "usage": usage,
            },
        )


def make(up, monkeypatch, cloud=False):
    monkeypatch.setenv("LOCAL_CONTEXT_PROBE", "true")  # capacités lues sur /api/show
    configs = [ProviderConfig("ollama", "openai", "http://localhost:11434/v1", "local")]
    if cloud:
        configs.append(ProviderConfig("openai", "openai", "http://openai/v1", "cloud", enabled=False))
    app = create_app(
        configs,
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free"),
        store=DocumentStore(":memory:"),
    )
    return TestClient(app)


def sse(text: str, prefix="data:") -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith(prefix) and line[len(prefix) :].strip() != "[DONE]":
            out.append(json.loads(line[len(prefix) :]))
    return out


def test_router_sends_tools_only_to_models_that_support_them(monkeypatch):
    up = FakeOllama()
    with make(up, monkeypatch) as c:
        image = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Et ici ?"},
                {"type": "image_url", "image_url": {"url": "https://exemple.org/ciel.jpg"}},
            ],
        }
        seen = c.post("/v1/chat/completions", json={"model": "auto", "messages": [image]}).json()
        assert seen["x_gateway"]["routing"]["ref"] == "ollama/gemma3:12b"  # vision annoncée par /api/show
        r = c.post("/v1/chat/completions", json={"model": "auto", "messages": [QUESTION], "tools": TOOLS}).json()
        assert r["x_gateway"]["routing"]["ref"] == "ollama/llama3.1:8b"  # avec outils : seul capable
        r = c.post("/v1/chat/completions", json={"model": "ollama/gemma3:12b", "messages": [QUESTION], "tools": TOOLS})
        assert r.status_code == 400 and "ne gère pas l'appel d'outils (function calling) d'après son serveur" in r.text


def test_explicit_400_when_no_allowed_model_supports_tools(monkeypatch):
    up = FakeOllama(models=("gemma3:12b",))
    with make(up, monkeypatch) as c:
        r = c.post("/v1/chat/completions", json={"model": "auto", "messages": [QUESTION], "tools": TOOLS})
        assert r.status_code == 400, r.text
        assert "aucun modèle autorisé ne les gère" in r.text and "ollama/gemma3:12b" in r.text
        assert up.bodies == []


def test_older_ollama_without_capabilities_falls_back_on_the_name(monkeypatch):
    up = FakeOllama(caps=False)
    with make(up, monkeypatch) as c:
        r = c.post("/v1/chat/completions", json={"model": "auto", "messages": [QUESTION], "tools": TOOLS}).json()
        assert r["x_gateway"]["routing"]["ref"] == "ollama/llama3.1:8b"
    assert not infer_local("ollama", "gemma3:12b").tools and infer_local("ollama", "qwen2.5:14b").tools


def test_full_tool_exchange_without_stream(monkeypatch):
    up = FakeOllama()
    with make(up, monkeypatch) as c:
        r = c.post("/v1/chat/completions", json={"model": "auto", "messages": [QUESTION], "tools": TOOLS})
        assert r.status_code == 200, r.text
        choice = r.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"  # « stop » d'Ollama corrigé
        call = choice["message"]["tool_calls"][0]
        assert call["id"].startswith("call_") and call["type"] == "function"
        assert json.loads(call["function"]["arguments"]) == {"ville": "Brest"}  # texte JSON, pas un objet
        assert up.bodies[-1]["tools"] == TOOLS
        turn2 = [QUESTION, choice["message"], {"role": "tool", "tool_call_id": call["id"], "content": "14 °C, averses"}]
        r = c.post("/v1/chat/completions", json={"model": "auto", "messages": turn2, "tools": TOOLS})
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["message"]["content"] == "D'après l'outil : 14 °C, averses"
        sent = up.bodies[-1]["messages"]
        assert sent[1]["tool_calls"][0]["id"] == call["id"] and sent[2]["tool_call_id"] == call["id"]


def test_full_tool_exchange_in_stream(monkeypatch):
    up = FakeOllama()
    with make(up, monkeypatch) as c:
        r = c.post(
            "/v1/chat/completions", json={"model": "auto", "messages": [QUESTION], "tools": TOOLS, "stream": True}
        )
        assert r.status_code == 200, r.text
        chunks = sse(r.text)
        deltas = [
            tc for ch in chunks for c in ch.get("choices", []) for tc in c.get("delta", {}).get("tool_calls") or []
        ]
        assert all(d["index"] == 0 for d in deltas)  # index ajouté
        assert deltas[0]["id"].startswith("call_") and deltas[0]["function"]["name"] == "meteo"
        args = "".join(d["function"].get("arguments", "") for d in deltas)
        assert json.loads(args) == {"ville": "Brest"}
        finish = [c["finish_reason"] for ch in chunks for c in ch.get("choices", []) if c.get("finish_reason")]
        assert finish == ["tool_calls"]
        call = {"id": deltas[0]["id"], "type": "function", "function": {"name": "meteo", "arguments": args}}
        turn2 = [
            QUESTION,
            {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call["id"], "content": "14 °C, averses"},
        ]
        r = c.post("/v1/chat/completions", json={"model": "auto", "messages": turn2, "tools": TOOLS, "stream": True})
        text = "".join(c["delta"].get("content") or "" for ch in sse(r.text) for c in ch.get("choices", []))
        assert text.strip() == "D'après l'outil : 14 °C, averses"


def test_tool_exchange_through_anthropic_messages(monkeypatch):
    up = FakeOllama()
    with make(up, monkeypatch) as c:
        body = {
            "model": "auto",
            "max_tokens": 200,
            "tools": ANTHROPIC_TOOLS,
            "tool_choice": {"type": "any"},
            "messages": [{"role": "user", "content": "Quel temps fait-il à Brest ?"}],
        }
        r = c.post("/v1/messages", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["stop_reason"] == "tool_use"
        use = next(b for b in data["content"] if b["type"] == "tool_use")
        assert use["name"] == "meteo" and use["input"] == {"ville": "Brest"} and use["id"]
        assert up.bodies[-1]["tool_choice"] == "required"  # any -> required
        body["messages"] += [
            {"role": "assistant", "content": data["content"]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "14 °C, averses"}],
            },
        ]
        body.pop("tool_choice")
        r = c.post("/v1/messages", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["content"][0]["text"] == "D'après l'outil : 14 °C, averses"
        assert up.bodies[-1]["messages"][-1] == {"role": "tool", "tool_call_id": use["id"], "content": "14 °C, averses"}


def test_tool_call_streamed_through_anthropic_messages(monkeypatch):
    up = FakeOllama()
    with make(up, monkeypatch) as c:
        r = c.post(
            "/v1/messages",
            json={
                "model": "auto",
                "max_tokens": 200,
                "stream": True,
                "tools": ANTHROPIC_TOOLS,
                "messages": [{"role": "user", "content": "Quel temps fait-il à Brest ?"}],
            },
        )
        assert r.status_code == 200, r.text
        events = sse(r.text)
        start = next(
            e for e in events if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use"
        )
        assert start["content_block"]["name"] == "meteo" and start["content_block"]["id"].startswith("call_")
        partial = "".join(
            e["delta"]["partial_json"]
            for e in events
            if e["type"] == "content_block_delta" and e["delta"]["type"] == "input_json_delta"
        )
        assert json.loads(partial) == {"ville": "Brest"}
        assert next(e for e in events if e["type"] == "message_delta")["delta"]["stop_reason"] == "tool_use"


def test_normalizers_leave_compliant_responses_alone():
    ok = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
                },
            }
        ]
    }
    assert normalize_tool_calls(json.loads(json.dumps(ok))) == ok
    s = ToolCallStream()
    first = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "id": "call_1", "type": "function", "function": {"name": "f", "arguments": ""}}
                    ]
                }
            }
        ]
    }
    more = {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}}]}
    second = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 1, "id": "call_2", "type": "function", "function": {"name": "g", "arguments": "{}"}}
                    ]
                }
            }
        ]
    }
    for chunk in (first, more, second):  # deux appels parallèles, conformes : intacts
        assert s.fix(json.loads(json.dumps(chunk))) == chunk
    text_only = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    assert ToolCallStream().fix(json.loads(json.dumps(text_only))) == text_only
