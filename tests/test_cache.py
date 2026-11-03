import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import translate
from gateway.cache import CacheError, ResponseCache, apply_prompt_cache, normalize_policy, response_ttl
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.registry import CLOUD_MODELS, infer_local
from gateway.store import DocumentStore

SONNET = next(m for m in CLOUD_MODELS if m.id == "claude-sonnet-4-5")
MINI = next(m for m in CLOUD_MODELS if m.id == "gpt-4o-mini")
GROQ = next(m for m in CLOUD_MODELS if m.id == "llama-3.1-8b-instant")
LONG = "Contexte de référence très détaillé. " * 400  # ~3600 tokens


# ---------- tarification ----------


def test_cache_pricing():
    assert SONNET.cache_read_price == pytest.approx(0.30) and SONNET.cache_write_price == pytest.approx(3.75)
    assert MINI.cache_read_price == pytest.approx(0.075) and MINI.cache_write_price == MINI.input_per_mtok_usd
    assert GROQ.cache_read_price == GROQ.input_per_mtok_usd  # pas de remise connue
    usage = {
        "prompt_tokens": 10_000,
        "completion_tokens": 1_000,
        "prompt_tokens_details": {"cached_tokens": 8_000, "cache_creation_tokens": 1_000},
    }
    # 1000 frais × 3 + 8000 lus × 0.30 + 1000 écrits × 3.75 + 1000 sortie × 15
    assert SONNET.usage_cost(usage) == pytest.approx((3000 + 2400 + 3750 + 15000) / 1e6)
    assert SONNET.cache_savings_usd(usage) == pytest.approx((8000 * 2.70 - 1000 * 0.75) / 1e6)
    assert infer_local("ollama", "llama3:8b").cache_savings_usd(usage) == 0


# ---------- politique & directives ----------


def _payload(system=None, n_turns=1, tools=False):
    msgs = [{"role": "system", "content": system}] if system else []
    for i in range(n_turns):
        msgs += [{"role": "user", "content": f"q{i} " + "x " * 400}, {"role": "assistant", "content": "r"}]
    msgs.append({"role": "user", "content": "dernière"})
    p = {"model": "m", "messages": msgs}
    if tools:
        p["tools"] = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    return p


def test_policy_parsing():
    assert normalize_policy(None) == "auto" and normalize_policy(True) == "on" and normalize_policy("off") == "off"
    with pytest.raises(CacheError):
        normalize_policy("parfois")
    assert response_ttl(None) is None and response_ttl(True) == 3600 and response_ttl(120) == 120
    assert response_ttl({"ttl": 60}) == 60 and response_ttl("off") is None and response_ttl("300") == 300
    with pytest.raises(CacheError):
        response_ttl("bientôt")


def test_anthropic_breakpoints_by_policy():
    short = apply_prompt_cache(_payload("Sois bref."), SONNET, "anthropic", "anthropic", "auto", None)
    assert "_prompt_cache" not in short  # trop court pour être caché
    big = apply_prompt_cache(_payload(LONG, tools=True), SONNET, "anthropic", "anthropic", "auto", None)
    assert big["_prompt_cache"] == {"tools": True, "system": True, "messages": True}
    forced = apply_prompt_cache(_payload("x"), SONNET, "anthropic", "anthropic", "on", None)
    assert forced["_prompt_cache"]["system"] and forced["_prompt_cache"]["messages"]
    assert apply_prompt_cache(_payload(LONG), SONNET, "anthropic", "anthropic", "off", None) == _payload(LONG)
    local = infer_local("ollama", "llama3:8b")
    assert apply_prompt_cache(_payload(LONG), local, "ollama", "openai", "on", "k") == _payload(LONG)


def test_openai_gets_cache_key_only():
    assert (
        apply_prompt_cache(_payload(LONG), MINI, "openai", "openai", "auto", "gw-abc")["prompt_cache_key"] == "gw-abc"
    )
    assert "prompt_cache_key" not in apply_prompt_cache(_payload(LONG), GROQ, "groq", "openai", "auto", "gw-abc")


def test_anthropic_translation_places_cache_control():
    p = apply_prompt_cache(_payload(LONG, n_turns=2, tools=True), SONNET, "anthropic", "anthropic", "auto", None)
    body = translate.openai_to_anthropic(p, "claude")
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-1]["content"][-1] == {
        "type": "text",
        "text": "dernière",
        "cache_control": {"type": "ephemeral"},
    }
    marks = json.dumps(body).count("cache_control")
    assert marks == 3  # limite Anthropic : 4
    plain = translate.openai_to_anthropic(_payload("Sois bref."), "claude")
    assert plain["system"] == "Sois bref." and "cache_control" not in json.dumps(plain)


def test_usage_normalization_both_ways():
    r = translate.anthropic_to_openai(
        {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 3000,
                "cache_creation_input_tokens": 500,
            },
        },
        "m",
    )
    assert r["usage"]["prompt_tokens"] == 3600
    assert r["usage"]["prompt_tokens_details"] == {"cached_tokens": 3000, "cache_creation_tokens": 500}
    back = translate.openai_usage_to_anthropic(r["usage"])
    assert back == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 3000,
        "cache_creation_input_tokens": 500,
    }
    conv = translate.AnthropicStreamConverter("m")
    conv.feed({"type": "message_start", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 90}}})
    conv.feed({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}})
    usage = conv.feed({"type": "message_stop"})[-1]["usage"]
    assert usage["prompt_tokens"] == 100 and usage["prompt_tokens_details"]["cached_tokens"] == 90


# ---------- intégration ----------


class Upstream:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path.endswith("/models"):
            ids = {"ollama": ["llama3.1:8b"], "openai": ["gpt-4o-mini"], "anthropic": ["claude-sonnet-4-5"]}[host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        body = json.loads(request.content)
        self.calls.append((host, body))
        if host == "anthropic":
            cached = "cache_control" in json.dumps(body)
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "réponse claude"}],
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 10,
                        "cache_read_input_tokens": 4000 if cached else 0,
                    },
                },
            )
        text = body["messages"][-1]["content"]
        msg = {"role": "assistant", "content": f"echo:{text}"}
        if "outil" in str(text):
            msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
            }
        usage = {"prompt_tokens": 2000, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 1024}}
        if body.get("stream"):
            chunks = [
                {"model": body["model"], "choices": [{"delta": {"content": msg["content"]}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": usage},
            ]
            return httpx.Response(200, text="".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n")
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "usage": usage,
                "choices": [
                    {"index": 0, "message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}
                ],
            },
        )


@pytest.fixture
def api():
    up, now = Upstream(), [1000.0]
    app = create_app(
        [
            ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
            ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k"),
            ProviderConfig("anthropic", "anthropic", "http://anthropic/v1", "cloud", api_key="k"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free"),
        store=DocumentStore(":memory:"),
        cache=ResponseCache(clock=lambda: now[0]),
    )
    with TestClient(app) as c:
        c.headers.update({"Authorization": "Bearer alice"})
        yield c, up, now


def ask(text, **kw):
    return {"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": text}], **kw}


def test_anthropic_project_context_is_cached(api):
    c, up, _ = api
    proj = c.post(
        "/v1/projects", json={"name": "Juridique", "system_prompt": LONG, "model": "anthropic/claude-sonnet-4-5"}
    ).json()
    r = c.post(
        "/v1/chat/completions", json={"project": proj["id"], "messages": [{"role": "user", "content": "Article 3 ?"}]}
    ).json()
    sent = up.calls[-1][1]
    assert sent["system"][-1]["cache_control"] == {"type": "ephemeral"} and "_prompt_cache" not in sent
    cache = r["x_gateway"]["cache"]
    assert cache["cached_tokens"] == 4000 and cache["savings_usd"] > 0
    assert r["x_gateway"]["cost_usd"] < SONNET.cost_usd(4020, 10)  # moins cher que sans cache
    c.post(
        "/v1/chat/completions",
        json={"project": proj["id"], "prompt_cache": "off", "messages": [{"role": "user", "content": "Article 3 ?"}]},
    )
    assert "cache_control" not in json.dumps(up.calls[-1][1])


def test_openai_cache_key_is_stable_per_project_and_hidden(api):
    c, up, _ = api
    c.post("/v1/chat/completions", json=ask("a"))
    c.post("/v1/chat/completions", json=ask("b"))
    k1, k2 = up.calls[0][1]["prompt_cache_key"], up.calls[1][1]["prompt_cache_key"]
    assert k1 == k2 and "alice" not in k1 and not any(k.startswith("_") for k in up.calls[0][1])
    c.post("/v1/chat/completions", json=ask("a"), headers={"Authorization": "Bearer bob"})
    assert up.calls[-1][1]["prompt_cache_key"] != k1


def test_response_cache_hit_miss_ttl(api):
    c, up, now = api
    r1 = c.post("/v1/chat/completions", json=ask("bonjour", cache=True)).json()
    assert "response_hit" not in r1["x_gateway"].get("cache", {}) and len(up.calls) == 1
    r2 = c.post("/v1/chat/completions", json=ask("bonjour", cache=True)).json()
    assert len(up.calls) == 1  # aucun appel modèle
    assert r2["choices"] == r1["choices"] and r2["x_gateway"]["cost_usd"] == 0
    assert (
        r2["x_gateway"]["cache"]["response_hit"]
        and r2["x_gateway"]["cache"]["saved_usd"] == r1["x_gateway"]["cost_usd"]
    )
    c.post("/v1/chat/completions", json=ask("bonjour", cache=True, temperature=0.9))
    assert len(up.calls) == 2  # paramètres différents -> miss
    c.post("/v1/chat/completions", json=ask("bonjour"))
    assert len(up.calls) == 3  # cache non demandé
    c.post("/v1/chat/completions", json=ask("bonjour", cache=True), headers={"Authorization": "Bearer bob"})
    assert len(up.calls) == 4  # isolé par client
    now[0] += 3601
    c.post("/v1/chat/completions", json=ask("bonjour", cache=True))
    assert len(up.calls) == 5  # expiré
    st = c.get("/v1/cache").json()
    assert st["hits"] == 1 and st["entries"] >= 1 and st["saved_usd"] > 0
    assert c.delete("/v1/cache").json()["purged"] >= 1 and c.get("/v1/cache").json()["entries"] == 0


def test_response_cache_streaming_and_header(api):
    c, up, _ = api
    r = c.post("/v1/chat/completions", json=ask("flux", stream=True), headers={"X-Gateway-Cache": "300"})
    assert "echo:flux" in r.text and len(up.calls) == 1
    r = c.post("/v1/chat/completions", json=ask("flux", stream=True), headers={"X-Gateway-Cache": "on"})
    events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: {")]
    text = "".join(e["choices"][0]["delta"].get("content", "") for e in events if e.get("choices"))
    assert text == "echo:flux" and len(up.calls) == 1 and r.text.strip().endswith("[DONE]")
    assert events[-1]["x_gateway"]["cache"]["response_hit"]
    # une réponse mise en cache en streaming sert aussi le non-streaming
    r = c.post("/v1/chat/completions", json=ask("flux", cache=True)).json()
    assert r["choices"][0]["message"]["content"] == "echo:flux" and len(up.calls) == 1


def test_tool_calls_are_not_cached(api):
    c, up, _ = api
    for _ in range(2):
        c.post("/v1/chat/completions", json=ask("appelle un outil", cache=True))
    assert len(up.calls) == 2
    assert c.post("/v1/chat/completions", json=ask("x", prompt_cache="parfois")).status_code == 400


def test_anthropic_inbound_reports_cache_usage(api):
    c, _, _ = api
    r = c.post(
        "/v1/messages",
        json={
            "model": "anthropic/claude-sonnet-4-5",
            "max_tokens": 50,
            "system": LONG,
            "messages": [{"role": "user", "content": "?"}],
        },
    ).json()
    assert r["usage"]["cache_read_input_tokens"] == 4000 and r["usage"]["input_tokens"] == 20
