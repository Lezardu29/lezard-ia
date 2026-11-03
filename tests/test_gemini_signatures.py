"""Gemini 3 et les appels d'outils d'un autre modèle (secours en cours de tâche) : signature de contournement."""

from types import SimpleNamespace

from gateway.providers import openai_compat as oc

CALL = {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}


def convo(call):
    return {
        "model": "gemini-3.8-flash",
        "messages": [
            {"role": "user", "content": "actualités ?"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "c1", "content": "résultats"},
        ],
    }


def test_a_call_made_by_another_model_gets_the_documented_bypass_signature():
    out = oc.with_thought_signatures(convo(CALL))
    sig = out["messages"][1]["tool_calls"][0]["extra_content"]["google"]["thought_signature"]
    assert sig == "skip_thought_signature_validator"
    assert "extra_content" not in CALL  # la conversation d'origine n'est pas modifiée


def test_a_real_gemini_signature_is_kept():
    signed = {**CALL, "extra_content": {"google": {"thought_signature": "vraie-signature"}}}
    out = oc.with_thought_signatures(convo(signed))
    assert out["messages"][1]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "vraie-signature"


def test_only_gemini_requests_are_changed():
    def clean(base_url):
        fake = SimpleNamespace(cfg=SimpleNamespace(base_url=base_url))
        return oc.OpenAICompatProvider._clean(fake, convo(CALL))

    gemini = clean("https://generativelanguage.googleapis.com/v1beta/openai")
    groq = clean("https://api.groq.com/openai/v1")
    assert "extra_content" in gemini["messages"][1]["tool_calls"][0]
    assert "extra_content" not in groq["messages"][1]["tool_calls"][0]
