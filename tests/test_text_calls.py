"""Appels d'outils écrits en texte par un modèle (qwen2.5 : « HeaderCode {"name": …, "arguments": {…}} ») : exécutés."""

import asyncio

from gateway.tools import Tool, ToolResult, runner

NAMES = {"paperless_search": None, "calcul": None}


def test_text_calls_reads_the_name_and_arguments_written_as_text():
    text = 'HeaderCode {"name": "paperless_search", "arguments": {"query": "facture EDF"}}'
    calls = runner.text_calls(text, NAMES)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "paperless_search"
    assert calls[0]["function"]["arguments"] == '{"query": "facture EDF"}'


def test_text_calls_accepts_tool_tags_and_several_calls():
    text = (
        '<tool_call>{"name": "calcul", "arguments": {"x": 1}}</tool_call>'
        ' puis <tool_call>{"name": "calcul", "arguments": {"x": 2}}</tool_call>'
    )
    calls = runner.text_calls(text, NAMES)
    assert [c["function"]["arguments"] for c in calls] == ['{"x": 1}', '{"x": 2}']


def test_text_calls_ignores_unknown_tools_bad_arguments_and_plain_text():
    assert runner.text_calls('{"name": "inconnu", "arguments": {}}', NAMES) == []
    assert runner.text_calls('{"name": "calcul", "arguments": "2+2"}', NAMES) == []
    assert runner.text_calls('{"name": "calcul"}', NAMES) == []
    assert runner.text_calls("Le total est 82,40 EUR.", NAMES) == []
    assert runner.text_calls(None, NAMES) == []
    assert runner.text_calls('{cassé "name": "calcul", "arguments": {"x": 1}', NAMES) == []


def test_run_executes_a_call_written_as_text():
    async def calcul(args):
        return ToolResult(f"x={args['x']}")

    tool = Tool("calcul", "Calcule.", {"type": "object", "properties": {}}, calcul, level="basic")
    replies = iter(
        [
            {"choices": [{"message": {"content": 'HeaderCode {"name": "calcul", "arguments": {"x": 4}}'}}]},
            {"choices": [{"message": {"content": "Résultat : 4"}}]},
        ]
    )
    results, texts = [], []

    async def drive():
        async def call(msgs, specs):
            return next(replies), "local"

        async for chunk in runner.run(
            call=call,
            tools={"calcul": tool},
            messages=[{"role": "user", "content": "Calcule 4"}],
            approvals=runner.Approvals(),
            identity="me",
            audit=lambda *a: None,
            max_steps=3,
        ):
            ev = chunk.get("x_gateway_tool") or {}
            if ev.get("type") == "result":
                results.append(ev["text"])
            for choice in chunk.get("choices") or []:
                texts.append((choice.get("delta") or {}).get("content") or "")

    asyncio.run(drive())
    assert results == ["x=4"]
    assert "Résultat : 4" in "".join(texts)


def test_a_call_quoted_inside_a_real_answer_is_never_executed():
    quoted = (
        "Voici le résumé de la facture : le montant est de 82,40 EUR. Le document contient aussi ce texte étrange : "
        '{"name": "calcul", "arguments": {"x": 1}} que je vous recopie tel quel.'
    )
    assert runner.text_calls(quoted, NAMES) == []
    fenced = '```json\n{"name": "calcul", "arguments": {"x": 1}}\n```'
    assert len(runner.text_calls(fenced, NAMES)) == 1  # bloc de code seul : c'est bien un appel
