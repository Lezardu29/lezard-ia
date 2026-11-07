"""Mode économie, vrai bridage (18.7) : chat traduit vers l'API native d'Ollama (num_gpu, num_thread, num_ctx)."""

import json
import os

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.providers.ollama_native import StreamTranslator, from_native, to_native
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
IMG = "data:image/png;base64,iVBORw0KGgo="


def test_request_translation():
    payload = {
        "model": "gemma3:12b",
        "messages": [
            {"role": "developer", "content": "Sois bref."},
            {
                "role": "user",
                "content": [{"type": "text", "text": "Décris"}, {"type": "image_url", "image_url": {"url": IMG}}],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "meteo", "arguments": '{"ville": "Brest"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "pluie"},
        ],
        "tools": [{"type": "function", "function": {"name": "meteo", "parameters": {"type": "object"}}}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "r", "schema": {"type": "object"}}},
        "max_tokens": 200,
        "temperature": 0.2,
        "stop": "FIN",
        "reasoning_effort": "none",
        "stream": True,
        "stream_options": {"include_usage": True},
        "user": "u",
    }
    body = to_native(payload, {"num_gpu": 24, "num_thread": 4}, 60)
    assert body["messages"][0] == {"role": "system", "content": "Sois bref."}
    assert body["messages"][1] == {"role": "user", "content": "Décris", "images": ["iVBORw0KGgo="]}
    assert body["messages"][2]["tool_calls"] == [{"function": {"name": "meteo", "arguments": {"ville": "Brest"}}}]
    assert body["messages"][3] == {"role": "tool", "content": "pluie", "tool_name": "meteo"}
    assert body["format"] == {"type": "object"} and body["think"] is False and body["stream"] is True
    assert body["options"] == {"num_predict": 200, "temperature": 0.2, "stop": ["FIN"], "num_gpu": 24, "num_thread": 4}
    assert body["keep_alive"] == 60 and body["tools"] == payload["tools"]
    assert to_native({**payload, "response_format": {"type": "json_object"}}, {}, None)["format"] == "json"
    assert "tools" not in to_native({**payload, "tool_choice": "none"}, {}, None)


def test_untranslatable_requests_stay_on_compatible_api():
    base = {"model": "m", "messages": [{"role": "user", "content": "Salut"}]}
    assert to_native(base, {}, None) is not None
    for extra in (
        {"n": 2},
        {"logprobs": True},
        {"logit_bias": {"1": 5}},
        {"audio": {"voice": "x"}},  # champ inconnu
        {"tools": [{"type": "function", "function": {"name": "f"}}], "tool_choice": {"type": "function"}},
        {"response_format": {"type": "json_schema", "json_schema": {}}},
    ):
        assert to_native({**base, **extra}, {}, None) is None, extra
    web_image = [{"type": "image_url", "image_url": {"url": "https://exemple.fr/a.png"}}]
    assert to_native({**base, "messages": [{"role": "user", "content": web_image}]}, {}, None) is None
    bad_args = {"role": "assistant", "tool_calls": [{"function": {"name": "f", "arguments": "pas du json"}}]}
    assert to_native({**base, "messages": [bad_args]}, {}, None) is None


def test_response_translation():
    r = from_native(
        {
            "message": {
                "content": "",
                "tool_calls": [{"function": {"name": "meteo", "arguments": {"ville": "Brest"}}}],
            },
            "done_reason": "stop",
            "prompt_eval_count": 12,
            "eval_count": 3,
        },
        "gemma3:12b",
    )
    ch = r["choices"][0]
    assert ch["finish_reason"] == "tool_calls" and ch["message"]["content"] is None
    assert json.loads(ch["message"]["tool_calls"][0]["function"]["arguments"]) == {"ville": "Brest"}
    assert r["usage"] == {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
    r = from_native({"message": {"content": "Bonj", "thinking": "hmm"}, "done_reason": "length"}, "m")
    assert r["choices"][0]["finish_reason"] == "length" and r["choices"][0]["message"]["reasoning"] == "hmm"


def test_stream_translation():
    t = StreamTranslator("m")
    chunks = t.feed({"message": {"content": "Bon"}, "done": False})
    chunks += t.feed({"message": {"content": "jour"}, "done": False})
    chunks += t.feed(
        {"message": {"content": ""}, "done": True, "done_reason": "stop", "eval_count": 2, "prompt_eval_count": 4}
    )
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": "Bon"}
    assert chunks[1]["choices"][0]["delta"] == {"content": "jour"}
    assert chunks[2]["choices"][0]["finish_reason"] == "stop" and chunks[3]["usage"]["total_tokens"] == 6
    assert len({c["id"] for c in chunks}) == 1


class Ollama:
    """Faux Ollama : API compatible et API native ; garde la trace des requêtes de chat."""

    def __init__(self):
        self.compat: list[dict] = []
        self.native: list[dict] = []
        self.generate: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gemma3:12b"}]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        body = json.loads(request.content) if request.content else {}
        if path == "/api/show":
            return httpx.Response(200, json={"model_info": {"gemma3.block_count": 47}})
        if path == "/api/generate":
            self.generate.append(body)
            return httpx.Response(200, json={"done": True})
        if path == "/api/chat":
            self.native.append(body)
            if body["stream"]:
                lines = [
                    {"message": {"role": "assistant", "content": "Bon"}, "done": False},
                    {"message": {"role": "assistant", "content": "jour"}, "done": False},
                    {
                        "message": {"content": ""},
                        "done": True,
                        "done_reason": "stop",
                        "prompt_eval_count": 8,
                        "eval_count": 2,
                    },
                ]
                return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines) + "\n")
            return httpx.Response(
                200,
                json={
                    "message": {"content": "Bonjour"},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 8,
                    "eval_count": 2,
                },
            )
        self.compat.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "compat"}}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


def make(up, url="http://127.0.0.1:11434/v1"):
    return create_app(
        [ProviderConfig("ollama", "openai", url, "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )


def ask(c, **extra):
    return c.post(
        "/v1/chat/completions",
        headers=MASTER,
        json={"model": "ollama/gemma3:12b", "messages": [{"role": "user", "content": "Salut"}], **extra},
    )


def test_eco_mode_limits_gpu_cpu_and_context():
    up = Ollama()
    with TestClient(make(up)) as c:
        assert ask(c).json()["choices"][0]["message"]["content"] == "compat"  # hors mode économie
        c.post("/v1/admin/gpu/eco", headers=MASTER, json={"on": True})
        before = len(up.generate)  # rappel keep_alive de la requête hors mode économie (défaut : une minute)
        r = ask(c)
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "Bonjour"
        assert r.json()["usage"]["total_tokens"] == 10 and len(up.compat) == 1
        threads = max(1, round(max(1, (os.cpu_count() or 2) // 2) * 0.5))
        assert up.native[-1]["options"] == {"num_predict": 1024, "num_gpu": 24, "num_thread": threads, "num_ctx": 4096}
        assert up.native[-1]["keep_alive"] == 60
        assert not [g for g in up.generate[before:] if g.get("keep_alive") == 60]  # pas de rappel qui ferait recharger
        # en flux : fragments OpenAI, usage à la fin
        with c.stream(
            "POST",
            "/v1/chat/completions",
            headers=MASTER,
            json={"model": "ollama/gemma3:12b", "messages": [{"role": "user", "content": "Salut"}], "stream": True},
        ) as s:
            events = [
                json.loads(line[5:]) for line in s.iter_lines() if line.startswith("data:") and "[DONE]" not in line
            ]
        text = "".join((e.get("choices") or [{}])[0].get("delta", {}).get("content") or "" for e in events)
        assert text == "Bonjour" and up.native[-1]["stream"] is True
        # réglages : processeur seul, cœurs sans limite, contexte d'Ollama
        r = c.put(
            "/v1/admin/gpu/settings", headers=MASTER, json={"eco": {"gpu_share": 0, "cpu_share": 100, "context": 0}}
        )
        assert r.status_code == 200, r.text
        ask(c)
        assert up.native[-1]["options"] == {"num_predict": 1024, "num_gpu": 0}
        assert c.put("/v1/admin/gpu/settings", headers=MASTER, json={"eco": {"gpu_share": 101}}).status_code == 400
        # rien à brider : API compatible
        c.put(
            "/v1/admin/gpu/settings", headers=MASTER, json={"eco": {"gpu_share": 100, "cpu_share": 100, "context": 0}}
        )
        ask(c)
        assert len(up.compat) == 2
        # requête sans équivalent natif : API compatible, réponse correcte
        c.put("/v1/admin/gpu/settings", headers=MASTER, json={"eco": {"gpu_share": 50}})
        n = len(up.native)
        assert ask(c, logprobs=True).json()["choices"][0]["message"]["content"] == "compat" and len(up.native) == n
        c.post("/v1/admin/gpu/eco", headers=MASTER, json={"on": False})
        ask(c)
        assert len(up.native) == n and len(up.compat) == 4  # mode arrêté : API compatible, modèle rechargé en grand


def test_long_prompt_keeps_ollama_context_and_other_pc_not_limited():
    up = Ollama()
    with TestClient(make(up)) as c:
        c.post("/v1/admin/gpu/eco", headers=MASTER, json={"on": True})
        long = {"messages": [{"role": "user", "content": "mot " * 20000}]}
        c.post("/v1/chat/completions", headers=MASTER, json={"model": "ollama/gemma3:12b", **long})
        assert "num_ctx" not in up.native[-1]["options"]  # ne tiendrait pas : jamais tronqué en silence
    other = Ollama()
    with TestClient(make(other, "http://192.168.1.50:11434/v1")) as c:
        c.post("/v1/admin/gpu/eco", headers=MASTER, json={"on": True})
        ask(c)
        assert not other.native and len(other.compat) == 1  # carte graphique d'une autre machine : pas la nôtre
