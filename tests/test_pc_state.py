"""État de la tâche sur le PC gardé hors du modèle et planificateur (24.2)."""

import asyncio
import json

from gateway.tools import Tool, ToolResult, runner
from gateway.tools.state import PLAN_PROMPT, TaskState, parse_plan


def _resp(content="", calls=None):
    msg = {"content": content}
    if calls:
        msg["tool_calls"] = [
            {"id": f"c{i}", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)
        ]
    return {"choices": [{"message": msg}]}


def test_parse_plan():
    assert parse_plan(_resp('Voici : {"etapes": ["ouvrir le bloc-notes", "  écrire   bonjour ", 3, ""]}')) == [
        "ouvrir le bloc-notes",
        "écrire bonjour",
    ]
    for bad in ("", "pas de json", '{"etapes": "un"}', "{cassé}", '["a"]'):
        assert parse_plan(_resp(bad)) == []
    assert len(parse_plan(_resp(json.dumps({"etapes": [f"étape {i}" for i in range(10)]})))) == 6


def test_render_and_limits():
    now = [100.0]
    s = TaskState("  ouvre   le bloc-notes ", max_steps=12, max_seconds=60, clock=lambda: now[0])
    s.plan = ["ouvrir le bloc-notes", "écrire bonjour"]
    assert "Aucune action" in s.render() and "1. ouvrir le bloc-notes" in s.render()
    s.record("pc_open", {"name": "bloc-notes", "reason": "pas encore ouvert"}, "ok")
    s.record("pc_click", {"element": 4, "reason": "le champ"}, "Erreur. Détails longs…")
    text = s.render()
    assert 'pc_open {"name": "bloc-notes"} (pas encore ouvert) → ok' in text
    assert "pc_click (le champ) → Erreur." in text and "Détails" not in text and s.actions == 2
    assert "Objectif : ouvre le bloc-notes" in text and "tour 1 sur 12" in text
    assert not s.expired()
    now[0] += 61
    assert s.expired() and "61 s écoulées sur 60 s" in s.render()
    assert not TaskState("x", 5, max_seconds=0, clock=lambda: 10**9).expired()  # 0 : sans limite de durée


def test_runner_keeps_the_state_outside_the_model():
    seen = []
    replies = iter(
        [
            _resp('{"etapes": ["ouvrir le bloc-notes", "écrire bonjour"]}'),  # planificateur
            _resp(calls=[("pc_act", {"reason": "ouvrir", "name": "bloc-notes"})]),
            _resp(calls=[("pc_act", {"reason": "écrire", "text": "bonjour"})]),
            _resp("fini"),
        ]
    )

    async def act(args):
        return ToolResult("fait")

    tool = Tool("pc_act", "t", {"type": "object", "properties": {}}, act, level="pc", confirm=True)
    approvals = runner.Approvals()
    state = TaskState("ouvre le bloc-notes et écris bonjour", max_steps=5, max_seconds=0, plan=True)

    async def drive():
        async def call(msgs, specs):
            seen.append((msgs, specs))
            return next(replies), "local"

        out = []
        async for c in runner.run(
            call=call,
            tools={"pc_act": tool},
            messages=[{"role": "user", "content": "ouvre le bloc-notes et écris bonjour"}],
            approvals=approvals,
            identity="me",
            audit=lambda *a: None,
            max_steps=5,
            state=state,
        ):
            ev = c.get("x_gateway_tool") or {}
            if ev.get("type") == "approval":  # chaque action sur le PC demande sa validation
                approvals.decide(ev["approval"], "me", True)
            out.append(c)
        return out

    events = [c["x_gateway_tool"] for c in asyncio.run(drive()) if "x_gateway_tool" in c]
    assert events[0] == {"type": "plan", "id": "plan", "steps": ["ouvrir le bloc-notes", "écrire bonjour"]}
    plan_msgs, plan_specs = seen[0]
    assert plan_msgs[0]["content"] == PLAN_PROMPT and plan_specs is None  # sans outils : il n'agit pas encore
    last_msgs, _ = seen[-1]
    states = [m for m in last_msgs if "État de la tâche" in str(m.get("content"))]
    assert len(states) == 1 and last_msgs[1] is states[0]  # un seul, juste après la demande
    assert "1. ouvrir le bloc-notes" in states[0]["content"] and "→ ok" in states[0]["content"]
    assert all(not k.startswith("x_") for m in last_msgs for k in m)  # marqueurs internes jamais envoyés
    assert state.actions == 2 and state.steps == 2


def test_duration_limit_forces_a_final_answer():
    now = [0.0]
    specs_seen = []

    async def act(args):
        now[0] += 100  # l'action prend du temps
        return ToolResult("fait")

    tool = Tool("pc_act", "t", {"type": "object", "properties": {}}, act, level="pc", confirm=False)
    approvals = runner.Approvals()
    state = TaskState("tâche longue", max_steps=10, max_seconds=60, plan=False, clock=lambda: now[0])
    replies = iter([_resp(calls=[("pc_act", {"reason": "x"})]), _resp("je m'arrête ici")])

    async def drive():
        async def call(msgs, specs):
            specs_seen.append(specs)
            return next(replies), "local"

        chunks = [
            c
            async for c in runner.run(
                call=call,
                tools={"pc_act": tool},
                messages=[{"role": "user", "content": "tâche longue"}],
                approvals=approvals,
                identity="me",
                audit=lambda *a: None,
                max_steps=10,
                state=state,
            )
        ]
        return "".join((c["choices"][0]["delta"] or {}).get("content") or "" for c in chunks if c.get("choices"))

    text = asyncio.run(drive())
    assert specs_seen[-1] is None  # dernier tour sans outils : le modèle dit où il en est
    assert text.startswith("je m'arrête ici") and "Durée maximale de la tâche atteinte (60 s)" in text
