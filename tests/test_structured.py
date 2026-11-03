"""Sorties structurées (16.3) : response_format natif, consigne + contrôle pour les autres, une seule nouvelle
tentative, routeur qui privilégie les modèles « json »."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import structured
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer master"}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ville", "temp"],
    "properties": {
        "ville": {"type": "string", "minLength": 1},
        "temp": {"type": "number", "minimum": -90},
        "ciel": {"enum": ["clair", "nuageux"]},
    },
}
RF = {"type": "json_schema", "json_schema": {"name": "meteo", "schema": SCHEMA, "strict": True}}


# ---------- extraction et contrôle ----------


def test_extract_json_from_model_text():
    assert structured.extract('{"a": 1}') == {"a": 1}
    assert structured.extract('Voici :\n```json\n{"a": [1, 2]}\n```\nBonne journée') == {"a": [1, 2]}
    assert structured.extract('Résultat : {"a": {"b": "}"}} fin') == {"a": {"b": "}"}}
    with pytest.raises(ValueError):
        structured.extract("Je ne sais pas.")


def test_schema_errors():
    assert structured.errors({"ville": "Brest", "temp": 12.5, "ciel": "clair"}, SCHEMA) == []
    found = structured.errors({"ville": "", "temp": "12", "vent": 3, "ciel": "pluie"}, SCHEMA)
    assert "$.ville : au moins 1 caractère(s)" in found and "$.temp : number attendu" in found
    assert "$ : champ « vent » non prévu par le schéma" in found
    assert any(e.startswith("$.ciel : valeur parmi") for e in found)
    assert structured.errors({"ville": "x"}, SCHEMA) == ["$ : champ « temp » manquant"]
    nested = {
        "type": "array",
        "minItems": 1,
        "items": {"$ref": "#/$defs/p"},
        "$defs": {"p": {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}},
    }
    assert structured.errors([{"n": 1}, {"n": 2.0}], nested) == []
    assert structured.errors([{"n": 1.5}], nested) == ["$[0].n : integer attendu"]
    assert structured.errors([], nested) == ["$ : au moins 1 élément(s)"]
    assert structured.errors(None, {"anyOf": [{"type": "string"}, {"type": "null"}]}) == []


# ---------- parcours complet ----------


def openai_reply(content, usage=(10, 5), tool=False):
    msg = {"role": "assistant", "content": content}
    if tool:
        msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
        }
    return {
        "choices": [{"index": 0, "finish_reason": "stop", "message": msg}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)},
    }


class Upstream:
    """OpenAI, Anthropic et un serveur local ; `replies[host]` : contenus successifs ; `refuse` : 400 sur
    response_format."""

    def __init__(self, replies=None, refuse=False):
        self.replies = {k: list(v) for k, v in (replies or {}).items()}
        self.refuse = refuse
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path.endswith("/models"):
            ids = {"openai": ["gpt-4o-mini"], "anthropic": ["claude-haiku-4-5"], "lms": ["qwen2.5-7b"]}[host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        body = json.loads(request.content)
        self.calls.append((host, body))
        if self.refuse and host == "lms" and "response_format" in body:
            return httpx.Response(400, json={"error": "'response_format.type' must be 'json_schema'"})
        content = (self.replies.get(host) or ['{"ville": "Brest", "temp": 12}']).pop(0)
        if host == "anthropic":
            return httpx.Response(
                200,
                json={
                    "id": "m",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [{"type": "text", "text": content}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 20, "output_tokens": 8},
                },
            )
        if body.get("stream"):  # flux OpenAI : contenu en deux morceaux, puis l'usage
            half = len(content) // 2
            events = [
                {"choices": [{"index": 0, "delta": {"content": part}}]} for part in (content[:half], content[half:])
            ]
            events.append({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
            sse = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=openai_reply(content, tool=content == "TOOL"))


def make(up, *names):
    configs = {
        "openai": ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="o"),
        "anthropic": ProviderConfig("anthropic", "anthropic", "http://anthropic/v1", "cloud", api_key="a"),
        "lms": ProviderConfig("lms", "openai", "http://lms:1234/v1", "local"),
    }
    return create_app(
        [configs[n] for n in names],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "pro", master_key="master"),
        store=DocumentStore(":memory:"),
    )


def ask(c, model, rf=RF, stream=False):
    return c.post(
        "/v1/chat/completions",
        headers=MASTER,
        json={
            "model": model,
            "response_format": rf,
            "stream": stream,
            "messages": [
                {"role": "system", "content": "Tu es météorologue."},
                {"role": "user", "content": "Météo à Brest ?"},
            ],
        },
    )


def test_native_response_format_is_forwarded_and_checked():
    up = Upstream({"openai": ['```json\n{"ville": "Brest", "temp": 12}\n```']})
    with TestClient(make(up, "openai")) as c:
        r = ask(c, "openai/gpt-4o-mini").json()
        assert up.calls[0][1]["response_format"] == RF
        assert json.loads(r["choices"][0]["message"]["content"]) == {"ville": "Brest", "temp": 12}
        assert r["x_gateway"]["structured"] == {"mode": "native", "valid": True, "retried": False}


def test_anthropic_gets_the_schema_in_the_prompt():
    up = Upstream({"anthropic": ['Voici la météo : {"ville": "Brest", "temp": 11.5, "ciel": "nuageux"}']})
    with TestClient(make(up, "anthropic")) as c:
        r = ask(c, "anthropic/claude-haiku-4-5").json()
        sent = up.calls[0][1]
        assert "response_format" not in sent
        system = sent["system"] if isinstance(sent["system"], str) else " ".join(b["text"] for b in sent["system"])
        assert system.startswith("Tu es météorologue.") and "UNIQUEMENT" in system and '"minLength": 1' in system
        assert r["choices"][0]["message"]["content"] == '{"ville": "Brest", "temp": 11.5, "ciel": "nuageux"}'
        assert r["x_gateway"]["structured"]["mode"] == "prompt"


def test_one_retry_with_the_error_then_flagged():
    up = Upstream({"openai": ['{"ville": "Brest"}', '{"ville": "Brest", "temp": 9}']})
    with TestClient(make(up, "openai")) as c:
        r = ask(c, "openai/gpt-4o-mini").json()
        assert len(up.calls) == 2
        retry = up.calls[1][1]["messages"]
        assert retry[-2] == {"role": "assistant", "content": '{"ville": "Brest"}'}
        assert "champ « temp » manquant" in retry[-1]["content"]
        assert r["x_gateway"]["structured"] == {"mode": "native", "valid": True, "retried": True}
        assert r["usage"]["prompt_tokens"] == 20 and r["usage"]["completion_tokens"] == 10  # deux appels comptés
        up.replies["openai"] = ["Désolé.", "Toujours pas."]
        r = ask(c, "openai/gpt-4o-mini").json()
        assert len(up.calls) == 4  # une seule nouvelle tentative
        assert r["x_gateway"]["structured"]["valid"] is False
        assert r["choices"][0]["message"]["content"] == "Toujours pas."  # rendue telle quelle


def test_server_refusing_response_format_falls_back_to_prompt():
    up = Upstream(refuse=True)
    with TestClient(make(up, "lms")) as c:
        r = ask(c, "lms/qwen2.5-7b", rf={"type": "json_object"}).json()
        assert [("response_format" in b) for _, b in up.calls] == [True, False]
        assert r["x_gateway"]["structured"]["mode"] == "prompt" and r["x_gateway"]["structured"]["valid"]
        assert "fallback" not in r["x_gateway"]
        r2 = ask(c, "lms/qwen2.5-7b", rf={"type": "json_object"})
        assert r2.status_code == 200  # santé intacte


def test_json_object_requires_an_object_and_tool_calls_pass():
    up = Upstream({"openai": ["[1, 2]", "[3]", "TOOL"]})
    with TestClient(make(up, "openai")) as c:
        r = ask(c, "openai/gpt-4o-mini", rf={"type": "json_object"}).json()
        assert r["x_gateway"]["structured"]["error"] == "objet JSON attendu à la racine ({…})"
        r = ask(c, "openai/gpt-4o-mini", rf={"type": "json_object"}).json()
        assert r["choices"][0]["message"]["tool_calls"] and r["x_gateway"]["structured"]["valid"]


def test_stream_without_native_support_is_checked_before_sending():
    up = Upstream({"anthropic": ['```json\n{"ville": "Brest", "temp": 3}\n```']})
    with TestClient(make(up, "anthropic")) as c:
        with c.stream(
            "POST",
            "/v1/chat/completions",
            headers=MASTER,
            json={
                "model": "anthropic/claude-haiku-4-5",
                "response_format": RF,
                "stream": True,
                "messages": [{"role": "user", "content": "Météo ?"}],
            },
        ) as r:
            events = [
                json.loads(line[5:])
                for line in r.iter_lines()
                if line.startswith("data:") and line[5:].strip() != "[DONE]"
            ]
        text = "".join((ch.get("delta") or {}).get("content") or "" for e in events for ch in e.get("choices") or [])
        assert text == '{"ville": "Brest", "temp": 3}'
        meta = next(e["x_gateway"] for e in events if e.get("x_gateway"))
        assert meta["structured"] == {"mode": "prompt", "valid": True, "retried": False, "buffered": True}


def stream_events(c, model, rf=RF):
    with c.stream(
        "POST",
        "/v1/chat/completions",
        headers=MASTER,
        json={"model": model, "response_format": rf, "stream": True, "messages": [{"role": "user", "content": "?"}]},
    ) as r:
        return [
            json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:") and line[5:].strip() != "[DONE]"
        ]


def test_native_stream_is_checked_at_the_end():
    up = Upstream({"openai": ['{"ville": "Brest", "temp": 12}', '{"ville": "Brest"}', "Pas de JSON"]})
    with TestClient(make(up, "openai")) as c:
        meta = next(e["x_gateway"] for e in stream_events(c, "openai/gpt-4o-mini") if e.get("x_gateway"))
        assert meta["structured"] == {"mode": "native", "valid": True, "retried": False, "streamed": True}
        meta = next(e["x_gateway"] for e in stream_events(c, "openai/gpt-4o-mini") if e.get("x_gateway"))
        assert meta["structured"]["valid"] is False and "« temp » manquant" in meta["structured"]["error"]
        events = stream_events(c, "openai/gpt-4o-mini", rf={"type": "json_object"})
        meta = next(e["x_gateway"] for e in events if e.get("x_gateway"))
        assert meta["structured"]["valid"] is False and meta["structured"]["streamed"]
        text = "".join((ch.get("delta") or {}).get("content") or "" for e in events for ch in e.get("choices") or [])
        assert text == "Pas de JSON"  # déjà envoyé : rien n'est réécrit, le rapport le signale
    with TestClient(make(Upstream(), "openai")) as c:  # sans response_format : pas de rapport
        with c.stream(
            "POST",
            "/v1/chat/completions",
            headers=MASTER,
            json={"model": "openai/gpt-4o-mini", "stream": True, "messages": [{"role": "user", "content": "?"}]},
        ) as r:
            events = [json.loads(x[5:]) for x in r.iter_lines() if x.startswith("data:") and "[DONE]" not in x]
        assert "structured" not in next(e["x_gateway"] for e in events if e.get("x_gateway"))


def test_router_prefers_native_json_models_but_does_not_refuse_others():
    up = Upstream()
    with TestClient(make(up, "openai", "anthropic")) as c:
        r = ask(c, "auto/speed").json()
        assert r["x_gateway"]["provider"] == "openai"  # response_format natif
        assert not any("anthropic" in x for x in r["x_gateway"]["routing"].get("alternatives", []))
    with TestClient(make(Upstream(), "anthropic")) as c:
        r = ask(c, "auto").json()  # avant 16.3 : 503
        assert r["x_gateway"]["provider"] == "anthropic" and r["x_gateway"]["structured"]["valid"]


def test_invalid_response_format_is_rejected():
    with TestClient(make(Upstream(), "openai")) as c:
        for rf in (
            {"type": "xml"},
            {"type": "json_schema"},
            {"type": "json_schema", "json_schema": {"schema": 3}},
            {"type": "json_schema", "json_schema": {"name": "a b", "schema": {}}},
        ):
            r = ask(c, "openai/gpt-4o-mini", rf=rf)
            assert r.status_code == 400, rf
        assert ask(c, "openai/gpt-4o-mini", rf={"type": "text"}).status_code == 200
