"""Travail en durée (/travail) : l'IA travaille seule, se relit, ne garde que ce qui est meilleur, s'arrête à l'heure."""

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import travail
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "text, seconds, task",
    [
        ("30min écris un poème", 1800, "écris un poème"),
        ("1h30 relis ce texte", 5400, "relis ce texte"),
        ("2 h fais un plan", 7200, "fais un plan"),
        ("1 heure 15 x", 4500, "x"),
        ("45 minutes x", 2700, "x"),
    ],
)
def test_duration_comes_first(text, seconds, task):
    assert travail.parse(text) == (seconds, task)


@pytest.mark.parametrize("text", ["", "écris un poème", "5 conseils pour dormir", "30min", "1min x", "9h x"])
def test_bad_durations_are_refused_with_an_example(text):
    with pytest.raises(travail.TravailError) as e:
        travail.parse(text)
    assert e.value.status == 400 and "/travail" in e.value.message or "Durée" in e.value.message


def test_critique_is_read_even_when_the_model_thinks_out_loud():
    note, items, calm = travail.read_critique("<think>hmm</think>NOTE : 6,5/10\n- Ajouter un exemple\n2) Raccourcir")
    assert (note, items, calm) == (6.5, ["Ajouter un exemple", "Raccourcir"], False)
    assert travail.read_critique("NOTE: 9/10\nAUCUNE") == (9.0, [], True)
    assert travail.clean("```markdown\nTexte\n```") == "Texte"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def scripted(clock, answers, step=60.0):
    """Fausse IA : rend les réponses dans l'ordre selon le type de demande ; chaque appel « dure » `step` secondes."""
    calls = []

    async def ask(system, prompt, quick=False):
        kind = (
            "compare"
            if "Réponds seulement par A ou B" in prompt
            else "improve"
            if "AMÉLIORATIONS À APPLIQUER" in prompt
            else "critique"
            if "NOTE : <note>/10" in prompt
            else "first"
        )
        calls.append((kind, prompt))
        clock.now += step
        await asyncio.sleep(0)
        answer = answers[kind].pop(0) if isinstance(answers[kind], list) else answers[kind]
        return answer(prompt) if callable(answer) else answer

    return ask, calls


def run_session(ask, text, clock):
    async def go():
        s = travail.Sessions(clock=clock)
        run = s.start(owner="moi", text=text, ask=ask)
        await run["task"]
        return run

    return asyncio.run(go())


def test_a_better_version_is_kept_and_the_ai_stops_when_it_finds_nothing_more():
    clock = Clock()
    better = "Version deux, plus complète, avec un exemple concret."

    def chosen(prompt):  # la fausse IA choisit toujours la nouvelle version, où qu'elle soit placée
        return "A" if prompt.index(better) < prompt.index("VERSION B") else "B"

    ask, calls = scripted(
        clock,
        {
            "first": "Version un.",
            "critique": ["NOTE : 5/10\n- Ajouter un exemple concret", "NOTE : 9/10\nAUCUNE", "NOTE : 9/10\nAUCUNE"],
            "improve": [better],
            "compare": chosen,
        },
    )
    run = run_session(ask, "30min explique la photosynthèse", clock)
    assert run["status"] == "done" and run["best"] == better
    assert run["reason"] == "l'IA ne trouve plus rien à améliorer"
    assert [c[0] for c in calls] == ["first", "critique", "improve", "compare", "critique", "critique"]
    assert "Ajouter un exemple concret" in calls[4][1]  # la critique suivante sait ce qui a déjà été fait
    report = travail.report(run)
    assert report.startswith(better) and "1 amélioration gardée" in report and "Tour 1 (note 5/10)" in report


def test_a_worse_or_shrunk_rewrite_is_never_kept():
    clock = Clock()
    ask, calls = scripted(
        clock,
        {
            "first": "Une première version assez longue pour la tâche demandée.",
            "critique": ["NOTE : 6/10\n- Mieux", "NOTE : 6/10\n- Encore mieux", "NOTE : 6/10\nAUCUNE", "AUCUNE"],
            "improve": ["Court.", "Une autre version, de même longueur que la première !"],
            "compare": lambda p: "A" if p.index("première version") < p.index("VERSION B") else "B",  # l'ancienne
        },
    )
    run = run_session(ask, "30min fais", clock)
    assert run["best"] == "Une première version assez longue pour la tâche demandée."
    assert [j.get("kept") for j in run["journal"] if j["round"] in (1, 2)] == [False, False]
    assert sum(1 for c in calls if c[0] == "compare") == 1  # la version raccourcie n'est même pas comparée


def test_the_session_ends_on_time():
    clock = Clock()
    ask, calls = scripted(
        clock,
        {"first": "V1", "critique": "NOTE : 5/10\n- Améliorer", "improve": "V2 un peu plus longue", "compare": "A"},
        step=200,
    )
    run = run_session(ask, "10min fais", clock)  # 600 s : première version, puis un ou deux tours
    assert run["status"] == "done" and run["reason"] == "temps écoulé" and run["round"] <= 3
    assert clock.now <= 600 + 3 * 200


def test_stopping_returns_the_best_version_so_far():
    async def go():
        gate = asyncio.Event()

        async def ask(system, prompt, quick=False):
            if "Fais une première version" in prompt:
                return "Première version."
            await gate.wait()  # bloquée dans la relecture
            return ""

        s = travail.Sessions()
        run = s.start(owner="moi", text="1h fais", ask=ask)
        for _ in range(20):
            await asyncio.sleep(0)
        with pytest.raises(travail.TravailError):
            s.stop(run["id"], "quelqu'un d'autre")
        s.stop(run["id"], "moi")
        await asyncio.gather(run["task"], return_exceptions=True)
        return run

    run = asyncio.run(go())
    assert run["status"] == "done" and run["best"] == "Première version." and run["reason"] == "arrêtée à votre demande"


def test_an_error_before_any_version_is_reported():
    async def ask(system, prompt, quick=False):
        raise travail.TravailError(503, "Aucune IA disponible.")

    run = run_session(ask, "30min fais", Clock())
    assert run["status"] == "error" and run["error"] == "Aucune IA disponible." and run["best"] == ""


def test_two_sessions_at_most():
    async def go():
        async def ask(system, prompt, quick=False):
            await asyncio.sleep(3600)

        s = travail.Sessions()
        a, b = s.start(owner="x", text="1h a", ask=ask), s.start(owner="y", text="1h b", ask=ask)
        with pytest.raises(travail.TravailError) as e:
            s.start(owner="x", text="1h c", ask=ask)
        assert e.value.status == 429
        assert s.last("x")["id"] == a["id"]
        for r in (a, b):
            r["task"].cancel()
        await asyncio.gather(a["task"], b["task"], return_exceptions=True)

    asyncio.run(go())


MASTER, ACCOUNT = {"Authorization": "Bearer master"}, {"Authorization": "Bearer compte"}


def test_routes_for_the_master_key_only(monkeypatch):
    seen = {}

    def fake_ask(port, key, model, private=False):
        seen.update(key=key, model=model, private=private)

        async def ask(system, prompt, quick=False):
            if "Fais une première version" in prompt:
                return "Le résultat."
            return "NOTE : 9/10\nAUCUNE"

        return ask

    monkeypatch.setattr(travail, "gateway_ask", fake_ask)
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama:11434/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        plans=PlanResolver({"compte": "free"}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        assert c.post("/v1/travail", headers=ACCOUNT, json={"text": "30min x"}).status_code == 403
        assert c.post("/v1/travail", headers=MASTER, json={"text": "écris"}).status_code == 400
        assert c.get("/v1/travail/last", headers=MASTER).status_code == 404
        r = c.post("/v1/travail", headers=MASTER, json={"text": "30min écris", "model": "qwen3:8b", "private": True})
        assert r.status_code == 200 and "text" not in r.json()
        assert seen == {"key": "master", "model": "qwen3:8b", "private": True}
        rid = r.json()["id"]
        for _ in range(50):
            d = c.get(f"/v1/travail/{rid}", headers=MASTER).json()
            if d["status"] != "running":
                break
            time.sleep(0.05)
        assert d["status"] == "done" and d["report"].startswith("Le résultat.")
        assert c.get("/v1/travail/last", headers=MASTER).json()["id"] == rid
        assert c.get("/v1/travail/inconnu", headers=MASTER).status_code == 404


def test_the_chat_has_the_command():
    js = (ROOT / "gateway" / "web" / "static" / "slash-commands.js").read_text(encoding="utf-8")
    assert 'name: "travail"' in js and 'kind: "travail"' in js
    chat = (ROOT / "gateway" / "web" / "static" / "chat.js").read_text(encoding="utf-8")
    assert 'if (app.kind === "travail") return askTravail(request, signal, onWait);' in chat


def test_auto_keeps_the_model_chosen_for_the_first_version(monkeypatch):
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(
            (body["model"], body.get("private"), body.get("reasoning_effort"), request.headers["authorization"])
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "x_gateway": {"routing": {"ref": "ollama/gemma4:e4b"}},
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(travail.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))

    async def go():
        ask = travail.gateway_ask(8000, "cle", None, private=True)
        await ask("s", "a")
        await ask("s", "b", True)

    asyncio.run(go())
    assert sent == [("auto", True, None, "Bearer cle"), ("ollama/gemma4:e4b", True, "low", "Bearer cle")]
