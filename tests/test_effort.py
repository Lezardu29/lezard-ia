import pytest

from gateway import translate
from gateway.classifier import classify
from gateway.effort import COT_PROMPT, EffortError, apply_effort, normalize_effort, thinking_budget
from gateway.registry import CLOUD_MODELS, infer_local
from gateway.router import route

O4 = next(m for m in CLOUD_MODELS if m.id == "o4-mini")
MINI = next(m for m in CLOUD_MODELS if m.id == "gpt-4o-mini")
SONNET = next(m for m in CLOUD_MODELS if m.id == "claude-sonnet-4-5")
GROQ8 = next(m for m in CLOUD_MODELS if m.id == "llama-3.1-8b-instant")
LLAMA = infer_local("ollama", "llama3.1:8b")
QWEN3 = infer_local("ollama", "qwen3:8b")
ALL = [O4, MINI, SONNET, GROQ8, LLAMA, QWEN3]

REQ = {
    "model": "x",
    "messages": [{"role": "user", "content": "Explique pourquoi le ciel est bleu"}],
    "temperature": 0.3,
}


def test_normalize():
    assert normalize_effort(None) == "medium"
    assert normalize_effort("Élevé") == "high" and normalize_effort("bas") == "low"
    hard = classify(
        {
            "messages": [
                {"role": "user", "content": "Calcule l'intégrale de x^2, démontre et explique pourquoi étape par étape"}
            ]
        }
    )
    assert normalize_effort("auto", hard) == "high"
    with pytest.raises(EffortError):
        normalize_effort("turbo")


def test_local_reasoning_detection():
    assert QWEN3.reasoning and infer_local("ollama", "deepseek-r1:14b").reasoning
    assert not LLAMA.reasoning and not infer_local("ollama", "starcoder1:3b").reasoning


def test_openai_reasoning_model_gets_reasoning_effort():
    out = apply_effort({**REQ, "reasoning_effort": "low"}, O4, "openai", "high")
    assert out["reasoning_effort"] == "high" and out["messages"] == REQ["messages"]


def test_non_reasoning_model_high_gets_cot_and_no_param():
    out = apply_effort({**REQ, "reasoning_effort": "high"}, MINI, "openai", "high")
    assert "reasoning_effort" not in out
    assert out["messages"][0] == {"role": "system", "content": COT_PROMPT}
    with_sys = {**REQ, "messages": [{"role": "system", "content": "Sois bref."}] + REQ["messages"]}
    out = apply_effort(with_sys, MINI, "openai", "high")
    assert out["messages"][0]["content"].startswith("Sois bref.") and COT_PROMPT in out["messages"][0]["content"]


def test_low_and_medium_leave_non_reasoning_untouched():
    for e in ("low", "medium"):
        assert apply_effort({**REQ, "reasoning_effort": e}, MINI, "openai", e) == REQ


def test_anthropic_thinking_only_on_high():
    assert "thinking" not in apply_effort(REQ, SONNET, "anthropic", "medium")
    out = apply_effort(REQ, SONNET, "anthropic", "high")
    assert out["thinking"] == {"type": "enabled", "budget_tokens": thinking_budget()}
    body = translate.openai_to_anthropic(out, "claude-sonnet-4-5")
    assert body["max_tokens"] > thinking_budget() and "temperature" not in body


def test_anthropic_thinking_response_and_stream():
    r = translate.anthropic_to_openai(
        {
            "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Rayleigh"}],
            "stop_reason": "end_turn",
            "usage": {},
        },
        "m",
    )
    msg = r["choices"][0]["message"]
    assert msg["content"] == "Rayleigh" and msg["reasoning_content"] == "hmm"
    conv = translate.AnthropicStreamConverter("m")
    ch = conv.feed({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "h"}})
    assert ch[0]["choices"][0]["delta"] == {"reasoning_content": "h"}


def test_effort_steers_routing():
    medium = classify({"messages": [{"role": "user", "content": "Compare Python et Go pour une API, avec trade-offs"}]})
    low = route(medium, "balanced", ALL, effort="low")
    high = route(medium, "balanced", ALL, effort="high")
    assert high.effort == "high" and "effort high" in high.reason
    by_ref = {m.ref: m for m in ALL}
    assert by_ref[high.ref].quality > by_ref[low.ref].quality
    assert by_ref[high.ref].reasoning
