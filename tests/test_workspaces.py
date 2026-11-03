import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore
from gateway.workspaces import WorkspaceError, extract_vars, render, trim_to_window, validate_project


# ---------- unités ----------


def test_template_vars_and_render():
    tpl = "Tu es {{ role }} pour {{project}}, le {{date}}. Langue : {{langue}}."
    assert extract_vars(tpl) == ["langue", "role"]
    out = render(tpl, {"role": "un juriste", "langue": "fr"}, "Contrats")
    assert out == f"Tu es un juriste pour Contrats, le {date.today().isoformat()}. Langue : fr."
    with pytest.raises(WorkspaceError) as e:
        render(tpl, {"role": "x"})
    assert "langue" in e.value.message


def test_validate_project():
    p = validate_project({"name": " Support ", "mode": "éco", "effort": "élevé", "max_context_tokens": 4096})
    assert p == {"name": "Support", "mode": "economy", "effort": "high", "max_context_tokens": 4096}
    assert validate_project({"name": "x", "effort": "auto"})["effort"] == "auto"
    for bad in (
        {"name": ""},
        {"name": "x", "mode": "turbo"},
        {"name": "x", "max_context_tokens": 100},
        {"name": "x", "documents": "doc_1"},
        {"name": "x", "max_output_tokens": True},
    ):
        with pytest.raises(WorkspaceError):
            validate_project(bad)


def _m(role, n_words, **kw):
    return {"role": role, "content": "mot " * n_words, **kw}


def test_trim_to_window_drops_oldest_and_keeps_tool_pairs():
    msgs = [
        _m("system", 50),
        _m("user", 400),
        _m("assistant", 400, tool_calls=[{"id": "1"}]),
        {"role": "tool", "tool_call_id": "1", "content": "mot " * 400},
        _m("user", 400),
        _m("user", 100),
    ]
    out, dropped = trim_to_window(msgs, limit=600)
    assert out[0]["role"] == "system" and out[-1] == msgs[-1]
    assert out[1]["role"] != "tool"  # pas de résultat d'outil orphelin
    assert dropped >= 3
    with pytest.raises(WorkspaceError) as e:
        trim_to_window([_m("user", 5000)], limit=1024)
    assert e.value.status == 413


# ---------- API ----------


class Upstream:
    def __init__(self):
        self.bodies = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            ids = {"ollama": ["llama3.1:8b"], "openai": ["gpt-4o-mini"]}[request.url.host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        self.bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )


@pytest.fixture
def api():
    up = Upstream()
    app = create_app(
        [
            ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
            ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        c.headers.update({"Authorization": "Bearer alice"})
        yield c, up


def test_prompt_crud_and_render(api):
    c, _ = api
    p = c.post("/v1/prompts", json={"name": "Traducteur", "content": "Traduis en {{langue}}."}).json()
    assert p["variables"] == ["langue"]
    assert (
        c.post(f"/v1/prompts/{p['id']}/render", json={"variables": {"langue": "anglais"}}).json()["content"]
        == "Traduis en anglais."
    )
    upd = c.patch(f"/v1/prompts/{p['id']}", json={"content": "Translate to {{lang}} ({{tone}})."}).json()
    assert upd["variables"] == ["lang", "tone"] and upd["name"] == "Traducteur"
    assert len(c.get("/v1/prompts").json()["data"]) == 1
    assert c.get(f"/v1/prompts/{p['id']}", headers={"Authorization": "Bearer bob"}).status_code == 404
    assert c.post("/v1/prompts", json={"name": "x"}).status_code == 400
    assert c.delete(f"/v1/prompts/{p['id']}").json()["deleted"]


def test_project_applies_defaults_prompt_and_documents(api):
    c, up = api
    prompt = c.post(
        "/v1/prompts", json={"name": "Support", "content": "Tu es l'assistant support de {{project}}. Ton : {{ton}}."}
    ).json()
    doc = c.post("/v1/documents", json={"filename": "faq.txt", "content": "Le remboursement prend 5 jours."}).json()
    proj = c.post(
        "/v1/projects",
        json={
            "name": "Boutique",
            "prompt_id": prompt["id"],
            "prompt_vars": {"ton": "chaleureux"},
            "model": "auto",
            "mode": "economy",
            "effort": "low",
            "documents": [doc["id"]],
            "max_output_tokens": 300,
        },
    ).json()
    assert proj["documents"] == [doc["id"]] and proj["mode"] == "economy"

    r = c.post(
        "/v1/chat/completions",
        json={"project": proj["id"], "messages": [{"role": "user", "content": "Délai de remboursement ?"}]},
    ).json()
    sent = up.bodies[-1]
    assert sent["messages"][0]["content"] == "Tu es l'assistant support de Boutique. Ton : chaleureux."
    assert "5 jours" in sent["messages"][1]["content"] and sent["messages"][2]["role"] == "user"
    assert sent["max_tokens"] == 300 and "project" not in sent
    g = r["x_gateway"]
    assert g["routing"]["mode"] == "economy" and g["effort"] == "low"
    assert g["project"] == {
        "id": proj["id"],
        "name": "Boutique",
        "prompt_id": prompt["id"],
        "max_context_tokens": None,
        "trimmed_messages": 0,
    }
    assert g["documents"]["ids"] == [doc["id"]]

    # la requête prime sur le projet ; les variables de la requête aussi
    r = c.post(
        "/v1/chat/completions",
        json={
            "project": proj["id"],
            "model": "openai/gpt-4o-mini",
            "effort": "medium",
            "max_tokens": 50,
            "prompt_vars": {"ton": "formel"},
            "messages": [{"role": "user", "content": "yo"}],
        },
    ).json()
    sent = up.bodies[-1]
    assert sent["model"] == "gpt-4o-mini" and sent["max_tokens"] == 50
    assert sent["messages"][0]["content"].endswith("Ton : formel.") and r["x_gateway"]["effort"] == "medium"

    # en-tête X-Gateway-Project + endpoint Anthropic
    r = c.post(
        "/v1/messages",
        headers={"X-Gateway-Project": proj["id"]},
        json={"max_tokens": 20, "messages": [{"role": "user", "content": "yo"}]},
    ).json()
    assert r["type"] == "message" and r["x_gateway"]["project"]["id"] == proj["id"]


def test_project_inline_system_prompt_and_prompt_switch(api):
    c, up = api
    proj = c.post(
        "/v1/projects",
        json={"name": "Dev", "system_prompt": "Réponds en code {{lang}}.", "prompt_vars": {"lang": "Python"}},
    ).json()
    c.post(
        "/v1/chat/completions",
        json={
            "project": proj["id"],
            "model": "ollama/llama3.1:8b",
            "messages": [{"role": "system", "content": "Sois bref."}, {"role": "user", "content": "tri"}],
        },
    )
    msgs = up.bodies[-1]["messages"]
    assert [m["content"] for m in msgs[:2]] == ["Réponds en code Python.", "Sois bref."]
    # un prompt explicite dans la requête remplace celui du projet
    p = c.post("/v1/prompts", json={"name": "Concis", "content": "Une phrase max."}).json()
    c.post(
        "/v1/chat/completions",
        json={"project": proj["id"], "prompt_id": p["id"], "messages": [{"role": "user", "content": "tri"}]},
    )
    assert up.bodies[-1]["messages"][0]["content"] == "Une phrase max."
    # PATCH : choisir un prompt_id efface le system_prompt
    upd = c.patch(f"/v1/projects/{proj['id']}", json={"prompt_id": p["id"]}).json()
    assert upd["prompt_id"] == p["id"] and upd["system_prompt"] is None
    assert c.delete(f"/v1/prompts/{p['id']}").status_code == 409  # utilisé par un projet
    # les deux à la fois à la création -> 400
    assert c.post("/v1/projects", json={"name": "X", "system_prompt": "a", "prompt_id": p["id"]}).status_code == 400


def test_project_context_window_trims_history(api):
    c, up = api
    proj = c.post(
        "/v1/projects",
        json={"name": "Court", "model": "ollama/llama3.1:8b", "max_context_tokens": 1024, "compression": "trim"},
    ).json()
    history = []
    for i in range(10):
        history += [
            {"role": "user", "content": f"question {i} " + "bla " * 150},
            {"role": "assistant", "content": "réponse " * 150},
        ]
    history.append({"role": "user", "content": "dernière question"})
    r = c.post("/v1/chat/completions", json={"project": proj["id"], "messages": history}).json()
    sent = up.bodies[-1]["messages"]
    assert sent[-1]["content"] == "dernière question" and len(sent) < len(history)
    assert r["x_gateway"]["project"]["trimmed_messages"] == len(history) - len(sent)
    assert sum(len(m["content"]) for m in sent) // 4 <= 1024
    too_big = c.post(
        "/v1/chat/completions", json={"project": proj["id"], "messages": [{"role": "user", "content": "x " * 5000}]}
    )
    assert too_big.status_code == 413


def test_project_documents_management(api):
    c, up = api
    proj = c.post("/v1/projects", json={"name": "Docs"}).json()
    d1 = c.post("/v1/documents", json={"filename": "a.txt", "content": "alpha", "project": proj["id"]}).json()
    assert d1["project"] == proj["id"]
    d2 = c.post("/v1/documents", files={"file": ("b.txt", b"beta")}, data={"project": proj["id"]}).json()
    assert c.get(f"/v1/projects/{proj['id']}").json()["documents"] == [d1["id"], d2["id"]]
    c.delete(f"/v1/projects/{proj['id']}/documents/{d1['id']}")
    assert c.get(f"/v1/projects/{proj['id']}").json()["documents"] == [d2["id"]]
    c.post(f"/v1/projects/{proj['id']}/documents/{d1['id']}")
    c.delete(f"/v1/documents/{d2['id']}")  # suppression en cascade
    assert c.get(f"/v1/projects/{proj['id']}").json()["documents"] == [d1["id"]]
    assert c.post(f"/v1/projects/{proj['id']}/documents/doc_inexistant").status_code == 404
    # projet sans modèle -> auto ; projet inconnu -> 404 ; isolement entre utilisateurs
    r = c.post("/v1/chat/completions", json={"project": proj["id"], "messages": [{"role": "user", "content": "?"}]})
    assert r.status_code == 200 and r.json()["x_gateway"]["routing"]
    assert (
        c.post(
            "/v1/chat/completions", json={"project": "proj_x", "messages": [{"role": "user", "content": "?"}]}
        ).status_code
        == 404
    )
    assert c.get(f"/v1/projects/{proj['id']}", headers={"Authorization": "Bearer bob"}).status_code == 404
    assert c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "?"}]}).status_code == 400
    prev = c.post("/v1/route", json={"project": proj["id"], "messages": [{"role": "user", "content": "?"}]}).json()
    assert prev["project"]["id"] == proj["id"]
    assert c.delete(f"/v1/projects/{proj['id']}").json()["deleted"]
