"""Entrées invalides -> 400 explicite, jamais 500 (10.1)."""

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore
from gateway.validation import tool_arguments

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
OK = {
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}


class Upstream:
    def __init__(self):
        self.mode = "ok"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
        if self.mode == "html":
            return httpx.Response(200, text="<html>Bad gateway</html>")
        if self.mode == "no-choices":
            return httpx.Response(200, json={"oops": True})
        if self.mode == "bad-sse":
            return httpx.Response(
                200,
                text='data: {"choices": [{"delta": {"content": "a"}}]}\n\ndata: {pas du json\n\n',
                headers={"content-type": "text/event-stream"},
            )
        if self.mode == "bad-tool-args":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "type": "function",
                                        "function": {"name": "meteo", "arguments": "{ville: Paris"},
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                },
            )
        return httpx.Response(200, json=OK)


@pytest.fixture
def api():
    up = Upstream()
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )
    # raise_server_exceptions=False : une erreur 500 apparaît comme réponse, au lieu de faire échouer le test
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, up


def msgs(*m):
    return {"model": "ollama/llama3.1:8b", "messages": list(m)}


USER = {"role": "user", "content": "Bonjour"}

BAD_CHAT = [
    ("{pas du json", "Corps JSON invalide"),
    ("[1, 2]", "objet JSON"),
    ("", "Corps JSON requis"),
    ({"model": "auto"}, "'messages' est requis"),
    ({"model": "auto", "messages": "Bonjour"}, "'messages' est requis"),
    (msgs("Bonjour"), "messages[0] : objet attendu"),
    (msgs({"content": "x"}), "messages[0] : 'role'"),
    (msgs({"role": "pirate", "content": "x"}), "messages[0] : 'role'"),
    (msgs({"role": "user"}), "'content' est requis"),
    (msgs({"role": "user", "content": 42}), "'content' doit être"),
    (msgs({"role": "user", "content": [{"text": "x"}]}), "champ 'type'"),
    (msgs({"role": "user", "content": [{"type": "image_url"}]}), "URL"),
    (msgs(USER, {"role": "tool", "content": "21°C"}), "tool_call_id"),
    (msgs(USER, {"role": "assistant", "content": None, "tool_calls": [{"function": "meteo"}]}), "tool_calls[0]"),
    ({**msgs(USER), "model": 12}, "'model' doit être une chaîne"),
    ({**msgs(USER), "mode": 5}, "'mode' doit être une chaîne"),
    ({**msgs(USER), "fallbacks": [1, 2]}, "'fallbacks'"),
    ({**msgs(USER), "prompt_vars": ["a"]}, "'prompt_vars'"),
    ({**msgs(USER), "max_tokens": "beaucoup"}, "'max_tokens'"),
    ({**msgs(USER), "tools": [{"name": "x"}]}, "'tools'"),
    ({**msgs(USER), "stream": "oui"}, "'stream'"),
]


@pytest.mark.parametrize("body, expected", BAD_CHAT)
@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/route", "/v1/classify"])
def test_bad_chat_requests_get_400(api, path, body, expected):
    c, _ = api
    r = (
        c.post(path, content=body, headers={"Content-Type": "application/json"})
        if isinstance(body, str)
        else c.post(path, json=body)
    )
    assert r.status_code == 400, r.text
    assert expected in r.json()["error"]["message"]


@pytest.mark.parametrize(
    "body, expected",
    [
        (
            {"model": "auto", "max_tokens": 10, "messages": [{"role": "system", "content": "x"}]},
            "'role' user ou assistant",
        ),
        (
            {
                "model": "auto",
                "messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "url"}}]}],
            },
            "'source'",
        ),
        (
            {"model": "auto", "messages": [{"role": "user", "content": [{"type": "tool_result", "content": "x"}]}]},
            "tool_use_id",
        ),
        ({"model": "auto", "messages": [USER], "thinking": {"type": "enabled"}}, "budget_tokens"),
        ({"model": "auto", "messages": [USER], "system": 5}, "system"),
    ],
)
def test_bad_anthropic_requests_get_400(api, body, expected):
    c, _ = api
    r = c.post("/v1/messages", json=body)
    assert r.status_code == 400 and expected in r.json()["error"]["message"], r.text


def test_valid_requests_still_work(api):
    c, _ = api
    assert c.post("/v1/chat/completions", json=msgs(USER)).status_code == 200
    tool_turn = msgs(
        USER,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "meteo", "arguments": '{"ville": "Paris"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "21°C"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Et demain ?"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
                    },
                },
            ],
        },
    )
    assert c.post("/v1/chat/completions", json=tool_turn).status_code == 200
    r = c.post("/v1/messages", json={"model": "ollama/llama3.1:8b", "max_tokens": 50, "messages": [USER]})
    assert r.status_code == 200 and r.json()["content"][0]["text"] == "ok"


def test_other_endpoints_reject_non_objects(api):
    c, _ = api
    for method, path in (
        ("post", "/v1/keys"),
        ("post", "/v1/prompts"),
        ("post", "/v1/projects"),
        ("post", "/v1/alerts"),
        ("put", "/v1/preferences/theme"),
        ("post", "/v1/pricing/estimate"),
    ):
        r = getattr(c, method)(
            path,
            headers=MASTER,
            content="[]",
        )
        assert r.status_code == 400, (path, r.status_code, r.text)
    # corps vide accepté là où il est facultatif
    k = c.post("/v1/keys", headers=MASTER, json={"name": "k"}).json()
    assert c.post(f"/v1/keys/{k['id']}/suspend", headers=MASTER).status_code == 200
    assert c.post("/v1/keys/bulk", headers=MASTER, json={"action": "resume", "ids": [["x"]]}).status_code == 400
    assert c.post("/v1/notifications/read", headers=MASTER, json={"ids": list(range(501))}).status_code == 400
    assert c.post("/v1/notifications/read", headers=MASTER).status_code == 200


@pytest.mark.parametrize("mode", ["html", "no-choices"])
def test_unreadable_provider_response_is_502(api, mode):
    c, up = api
    up.mode = mode
    r = c.post("/v1/chat/completions", json=msgs(USER))
    assert r.status_code == 502 and r.json()["error"]["provider"] in ("ollama", "gateway"), r.text


def test_unreadable_stream_line_ends_stream_cleanly(api):
    c, up = api
    up.mode = "bad-sse"
    r = c.post("/v1/chat/completions", json={**msgs(USER), "stream": True})
    assert r.status_code == 200
    assert "Flux illisible" in r.text and r.text.rstrip().endswith("data: [DONE]")


def test_invalid_tool_arguments_from_model_do_not_crash_messages(api):
    c, up = api
    up.mode = "bad-tool-args"
    r = c.post("/v1/messages", json={"model": "ollama/llama3.1:8b", "max_tokens": 50, "messages": [USER]})
    assert r.status_code == 200
    assert r.json()["content"][0]["input"] == {"arguments": "{ville: Paris"}


JUNK = [
    "[]",
    "{}",
    '"texte"',
    "42",
    "null",
    "{pas du json",
    '{"messages": 5, "model": []}',
    '{"name": [], "content": {}, "ids": {}, "provider": 3, "model": {}, "query": [], "action": 1}',
]


def all_routes(routes):
    """Routes à plat, y compris celles des routeurs par domaine (gateway/api/, 10.8) : selon la version de FastAPI,
    un routeur inclus est recopié route par route ou gardé en une seule entrée (`original_router`)."""
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from all_routes(inner.routes)
        else:
            yield r


def test_no_endpoint_answers_500_to_junk(api):
    """Fuzz minimal : chaque route POST/PUT/PATCH, corps absurdes, en admin -> jamais 500."""
    c, _ = api
    # vrais objets : les routes /{id} lisent alors réellement le corps au lieu de répondre 404
    ids = {
        "key_id": c.post("/v1/keys", headers=MASTER, json={"name": "k"}).json()["id"],
        "prompt_id": c.post("/v1/prompts", headers=MASTER, json={"name": "p", "content": "Tu es {{role}}."}).json()[
            "id"
        ],
        "project_id": c.post("/v1/projects", headers=MASTER, json={"name": "proj"}).json()["id"],
        "rule_id": c.post("/v1/alerts", headers=MASTER, json={"name": "a", "amount": 5}).json()["id"],
        "doc_id": c.post("/v1/documents", headers=MASTER, json={"filename": "n.txt", "content": "Texte."}).json()["id"],
        "provider": "ollama",
        "name": "ollama",
        "bridge_id": "x",
        "ip": "203.0.113.9",
    }
    routes = [
        (m, r.path)
        for r in all_routes(c.app.routes)
        for m in getattr(r, "methods", set()) or ()
        if m in ("POST", "PUT", "PATCH")
    ]
    assert len(routes) > 30
    failures = []
    for method, path in routes:
        url = path.replace("{ref:path}", "ollama/x").replace("{model:path}", "x")
        for p, v in ids.items():
            url = url.replace("{" + p + "}", v)
        for body in JUNK:
            r = c.request(method, url, headers={**MASTER, "Content-Type": "application/json"}, content=body)
            if r.status_code >= 500 and r.status_code not in (502, 503):
                failures.append((method, url, body, r.status_code, r.text[:120]))
    assert not failures, failures


def test_tool_arguments():
    assert tool_arguments('{"a": 1}') == {"a": 1}
    assert tool_arguments("") == {} and tool_arguments(None) == {}
    assert tool_arguments("[1]") == {"arguments": [1]}
    assert tool_arguments("pas du json") == {"arguments": "pas du json"}
