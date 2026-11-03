import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.compression import SUMMARY_TAG, _cut_index, transcript
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore


class Upstream:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.summaries: list[dict] = []  # appels au résumeur
        self.answers: list[dict] = []  # appels de réponse

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path.endswith("/models"):
            ids = {"ollama": ["llama3.1:8b"], "openai": ["gpt-4o-mini"]}[host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        body = json.loads(request.content)
        is_summary = "Tu résumes une conversation" in str(body["messages"][0].get("content"))
        (self.summaries if is_summary else self.answers).append({"host": host, **body})
        if is_summary and host in self.fail:
            return httpx.Response(503, text="indisponible")
        text = f"RÉSUMÉ#{len(self.summaries)}" if is_summary else "ok"
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
            },
        )


def make(up):
    app = create_app(
        [
            ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
            ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free"),
        store=DocumentStore(":memory:"),
    )
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer alice"})
    return c


def history(turns: int, words: int = 150) -> list[dict]:
    msgs = []
    for i in range(turns):
        msgs += [
            {"role": "user", "content": f"question {i} " + "bla " * words},
            {"role": "assistant", "content": f"réponse {i} " + "mot " * words},
        ]
    return msgs


def ask(msgs, **kw):
    return {
        "model": "ollama/llama3.1:8b",
        "max_context_tokens": 1024,
        **kw,
        "messages": msgs + [{"role": "user", "content": "et maintenant ?"}],
    }


# ---------- unités ----------


def test_cut_index_keeps_recent_and_valid_boundaries():
    convo = history(6)
    k = _cut_index(convo, 450)
    assert 0 < k < len(convo) and convo[k]["role"] == "user"
    tooly = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "f"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "r " * 300},
        {"role": "user", "content": "b"},
    ]
    assert tooly[_cut_index(tooly, 100)]["role"] == "user"
    assert _cut_index(history(1), -50) == 1  # au moins le dernier message


def test_transcript_format():
    t = transcript(
        [
            {
                "role": "user",
                "content": [{"type": "text", "text": "vois"}, {"type": "image_url", "image_url": {"url": "x"}}],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "1", "function": {"name": "meteo", "arguments": '{"v":"Paris"}'}}],
            },
            {"role": "tool", "content": "12°C"},
            {"role": "system", "content": f"{SUMMARY_TAG} (4 messages) :\nancien"},
        ]
    )
    assert "Utilisateur : vois [image]" in t and 'meteo({"v":"Paris"})' in t
    assert "Résultat d'outil : 12°C" in t and "Résumé antérieur :" in t


# ---------- intégration ----------


def test_no_compression_under_limit():
    up = Upstream()
    with make(up) as c:
        r = c.post("/v1/chat/completions", json=ask(history(1, 20))).json()
        assert "compression" not in r["x_gateway"] and not up.summaries


def test_summary_replaces_old_messages_and_is_incremental():
    up = Upstream()
    with make(up) as c:
        h = history(8)
        r = c.post("/v1/chat/completions", json=ask(h)).json()
        comp = r["x_gateway"]["compression"]
        assert comp["mode"] == "summary" and comp["cache"] == "miss" and len(up.summaries) == 1
        assert comp["summarizer"] == ["ollama/llama3.1:8b"] and comp["cost_usd"] == 0  # local gratuit
        assert comp["final_tokens"] <= 1024 < comp["original_tokens"]
        sent = up.answers[-1]["messages"]
        assert sent[0]["role"] == "system" and sent[0]["content"].startswith(SUMMARY_TAG)
        assert "RÉSUMÉ#1" in sent[0]["content"] and sent[-1]["content"] == "et maintenant ?"
        assert comp["summarized_messages"] + comp["kept_messages"] == len(h) + 1
        assert "question 0" in up.summaries[0]["messages"][1]["content"]  # transcription

        # même historique : résumé servi depuis le cache, aucun appel
        r = c.post("/v1/chat/completions", json=ask(h)).json()
        assert r["x_gateway"]["compression"]["cache"] == "hit" and len(up.summaries) == 1

        # tour suivant : seul le nouveau segment est résumé, à partir du résumé existant
        h2 = (
            h
            + [{"role": "user", "content": "et maintenant ?"}, {"role": "assistant", "content": "ok " * 150}]
            + history(2)
        )
        r = c.post("/v1/chat/completions", json=ask(h2)).json()
        assert r["x_gateway"]["compression"]["cache"] == "partial" and len(up.summaries) == 2
        prompt = up.summaries[-1]["messages"][1]["content"]
        assert prompt.startswith("Résumé existant à compléter :\nRÉSUMÉ#1") and "question 0" not in prompt


def test_modes_trim_off_and_validation():
    up = Upstream()
    with make(up) as c:
        r = c.post("/v1/chat/completions", json=ask(history(8), compression="trim")).json()
        assert r["x_gateway"]["compression"]["mode"] == "trim" and not up.summaries
        r = c.post("/v1/chat/completions", json=ask(history(8)), headers={"X-Gateway-Compression": "off"}).json()
        assert "compression" not in r["x_gateway"] and len(up.answers[-1]["messages"]) == 17
        assert c.post("/v1/chat/completions", json=ask(history(2), compression="magie")).status_code == 400
        assert c.post("/v1/chat/completions", json=ask(history(2), max_context_tokens=100)).status_code == 400
        proj = c.post(
            "/v1/projects",
            json={"name": "P", "model": "ollama/llama3.1:8b", "max_context_tokens": 1024, "compression": "trim"},
        ).json()
        assert proj["compression"] == "trim"
        r = c.post("/v1/chat/completions", json={"project": proj["id"], "messages": history(8)}).json()
        assert r["x_gateway"]["compression"]["mode"] == "trim"
        assert c.post("/v1/projects", json={"name": "X", "compression": "zip"}).status_code == 400


def test_summarizer_failure_falls_back_to_trim():
    up = Upstream(fail={"ollama", "openai"})
    with make(up) as c:
        r = c.post("/v1/chat/completions", json=ask(history(8)))
        assert r.status_code == 200
        comp = r.json()["x_gateway"]["compression"]
        assert comp["mode"] == "trim" and comp["fallback_from"] == "summary" and comp["trimmed_messages"] > 0
        assert len(up.summaries) >= 2  # local puis alternative cloud


def test_cloud_summarizer_cost_is_added(monkeypatch):
    monkeypatch.setenv("SUMMARY_MODEL", "openai/gpt-4o-mini")
    up = Upstream()
    with make(up) as c:
        r = c.post("/v1/chat/completions", json=ask(history(8))).json()
        comp = r["x_gateway"]["compression"]
        assert comp["summarizer"] == ["openai/gpt-4o-mini"] and comp["cost_usd"] > 0
        assert r["x_gateway"]["cost_usd"] == pytest.approx(comp["cost_usd"])  # réponse locale gratuite + résumé


def test_huge_history_summarized_in_batches():
    up = Upstream()
    with make(up) as c:
        r = c.post("/v1/chat/completions", json=ask(history(60, 300), max_context_tokens=4096)).json()
        assert len(up.summaries) >= 3  # plusieurs lots
        assert up.summaries[1]["messages"][1]["content"].startswith("Résumé existant à compléter :\nRÉSUMÉ#1")
        assert r["x_gateway"]["compression"]["final_tokens"] <= 4096


def test_compress_endpoint_and_anthropic_route():
    up = Upstream()
    with make(up) as c:
        out = c.post("/v1/compress", json={"messages": history(8), "max_context_tokens": 1024}).json()
        assert out["compression"]["mode"] == "summary" and out["messages"][0]["content"].startswith(SUMMARY_TAG)
        assert not up.answers
        assert c.post("/v1/compress", json={"messages": history(1)}).status_code == 400
        r = c.post(
            "/v1/messages",
            json={
                "model": "ollama/llama3.1:8b",
                "max_tokens": 50,
                "max_context_tokens": 1024,
                "messages": history(8) + [{"role": "user", "content": "?"}],
            },
        ).json()
        assert r["x_gateway"]["compression"]["mode"] == "summary"


def test_store_migrates_old_projects_table(tmp_path):
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, "
        "description TEXT, system_prompt TEXT, prompt_id TEXT, prompt_vars TEXT NOT NULL DEFAULT '{}', "
        "model TEXT, mode TEXT, effort TEXT, documents_budget INTEGER, max_context_tokens INTEGER, "
        "max_output_tokens INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
    )
    db.commit()
    db.close()
    store = DocumentStore(path)
    p = store.add_project("alice", {"name": "Migré", "compression": "trim"})
    assert store.get_project("alice", p["id"])["compression"] == "trim"


def test_compact_endpoint_summarizes_whole_conversation():
    """Commande /compacte du chat : toute la conversation devient un résumé, consigne et résumé antérieur compris."""
    up = Upstream()
    with make(up) as c:
        msgs = [{"role": "system", "content": "Consigne de projet"}] + history(2, 20)
        r = c.post("/v1/compact", json={"messages": msgs, "instructions": "garde les chiffres"})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["summary"].startswith(f"{SUMMARY_TAG} (4 messages) :\nRÉSUMÉ#1")
        assert out["summarized_messages"] == 4 and out["summarizer"] == ["ollama/llama3.1:8b"]
        call = up.summaries[-1]["messages"]
        assert "garde les chiffres" in call[0]["content"] and "Consigne de projet" not in call[1]["content"]

        # second compactage : le résumé précédent est repris
        again = [{"role": "system", "content": out["summary"]}] + history(1, 20)
        assert c.post("/v1/compact", json={"messages": again}).status_code == 200
        assert "Résumé antérieur : " in up.summaries[-1]["messages"][1]["content"]

        assert c.post("/v1/compact", json={"messages": msgs[:1]}).status_code == 400  # rien à compacter
        assert c.post("/v1/compact", json={"messages": msgs, "instructions": 3}).status_code == 400


def test_compact_failure_is_reported():
    up = Upstream(fail={"ollama", "openai"})
    with make(up) as c:
        r = c.post("/v1/compact", json={"messages": history(2, 20)})
        assert r.status_code == 502 and "Compactage impossible" in r.text
