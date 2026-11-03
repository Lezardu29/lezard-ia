"""Conversations enregistrées sur la gateway (19.1)."""

import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.conversations import MAX_MESSAGES, fallback_title
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
ACCOUNT_KEY = "ct-compte-0123456789abcdefghij"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
ACCOUNT = {"Authorization": f"Bearer {ACCOUNT_KEY}"}
SECRET_TEXT = "Mon code d'accès au coffre est 4417"


class Upstream:
    """Faux Ollama : répond aux demandes de titre ; compte les appels."""

    def __init__(self, title="Coffre et code d'accès"):
        self.title, self.calls = title, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
        self.calls += 1
        if self.title is None:
            return httpx.Response(500, json={"error": {"message": "panne"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": f"« {self.title}. »"},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )


def make(store=None, up=None, local=True):
    up = up or Upstream()
    configs = [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")] if local else []
    app = create_app(
        configs,
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({ACCOUNT_KEY: "pro"}, "free", master_key=MASTER_KEY),
        store=store or DocumentStore(":memory:"),
    )
    return TestClient(app), up


def new_key(c, name="telephone"):
    return {"Authorization": f"Bearer {c.post('/v1/keys', headers=ACCOUNT, json={'name': name}).json()['key']}"}


def exchange(question, answer):
    return [
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer, "meta": {"model": "ollama/llama3.1:8b", "cost_eur": 0}},
    ]


def test_same_key_two_browsers_other_key_404():
    """Critère d'acceptation : commencée dans un navigateur, poursuivie dans un autre avec la même clé ;
    une autre clé (et le compte lui-même) reçoit 404."""
    c, _ = make()
    with c:
        phone, laptop = new_key(c, "telephone"), new_key(c, "portable")
        conv = c.post("/v1/conversations", headers=phone, json={"messages": exchange("Bonjour", "Salut !")}).json()
        assert conv["revision"] == 1 and conv["message_count"] == 2 and conv["title"] == ""
        # « deuxième navigateur » : rien en commun avec le premier, sauf la clé
        other_browser = c.get("/v1/conversations", headers=phone).json()
        assert [x["id"] for x in other_browser["data"]] == [conv["id"]]
        loaded = c.get(f"/v1/conversations/{conv['id']}", headers=phone).json()
        assert loaded["messages"][1]["meta"]["model"] == "ollama/llama3.1:8b"
        more = loaded["messages"] + exchange("Et ensuite ?", "On continue.")
        r = c.put(
            f"/v1/conversations/{conv['id']}/messages",
            headers=phone,
            json={"messages": more, "revision": loaded["revision"]},
        )
        assert r.status_code == 200 and r.json()["revision"] == 2 and r.json()["message_count"] == 4
        for intruder in (laptop, ACCOUNT, MASTER):
            assert c.get(f"/v1/conversations/{conv['id']}", headers=intruder).status_code == 404
            assert c.get("/v1/conversations", headers=intruder).json()["data"] == []
            assert c.delete(f"/v1/conversations/{conv['id']}", headers=intruder).status_code == 404
            assert (
                c.put(
                    f"/v1/conversations/{conv['id']}/messages", headers=intruder, json={"messages": [], "revision": 2}
                ).status_code
                == 404
            )
        assert c.get(f"/v1/conversations/{conv['id']}", headers=phone).json()["message_count"] == 4


def test_encrypted_at_rest(tmp_path):
    store = DocumentStore(str(tmp_path / "g.db"))
    c, _ = make(store=store)
    with c:
        key = new_key(c)
        conv = c.post(
            "/v1/conversations",
            headers=key,
            json={"messages": exchange(SECRET_TEXT, "Noté."), "title": "Coffre secret"},
        ).json()
        assert c.get(f"/v1/conversations/{conv['id']}", headers=key).json()["messages"][0]["content"] == SECRET_TEXT
    store.close()
    raw = (tmp_path / "g.db").read_bytes() + b"".join(p.read_bytes() for p in tmp_path.glob("g.db-*"))
    assert b"4417" not in raw and b"Coffre secret" not in raw
    con = sqlite3.connect(tmp_path / "g.db")
    title, body = con.execute("SELECT title, body FROM conversations").fetchone()
    assert title.startswith("v1:") and body.startswith("v1:")
    con.close()


def test_other_device_wrote_first_conflict():
    c, _ = make()
    with c:
        key = new_key(c)
        conv = c.post("/v1/conversations", headers=key, json={"messages": exchange("A", "B")}).json()
        first = c.put(
            f"/v1/conversations/{conv['id']}/messages",
            headers=key,
            json={"messages": exchange("A", "B") + exchange("C", "D"), "revision": 1},
        )
        assert first.status_code == 200
        late = c.put(
            f"/v1/conversations/{conv['id']}/messages",
            headers=key,  # appareil resté en révision 1
            json={"messages": exchange("A", "B") + exchange("X", "Y"), "revision": 1},
        )
        assert late.status_code == 409 and "autre appareil" in late.text
        assert c.get(f"/v1/conversations/{conv['id']}", headers=key).json()["messages"][2]["content"] == "C"
        assert (
            c.put(f"/v1/conversations/{conv['id']}/messages", headers=key, json={"messages": []}).status_code == 400
        )  # révision obligatoire
        # renommer / mode privé : sans changer la révision (n'interrompt pas l'autre appareil)
        r = c.patch(f"/v1/conversations/{conv['id']}", headers=key, json={"title": "Renommée", "private": True})
        assert r.json()["title"] == "Renommée" and r.json()["private"] is True and r.json()["revision"] == 2


def test_title_from_local_model_then_fallback():
    c, up = make()
    with c:
        key = new_key(c)
        conv = c.post("/v1/conversations", headers=key, json={"messages": exchange(SECRET_TEXT, "Noté.")}).json()
        titled = c.post(f"/v1/conversations/{conv['id']}/title", headers=key).json()
        assert titled["title"] == "Coffre et code d'accès" and up.calls == 1  # guillemets et point retirés
        assert c.post(f"/v1/conversations/{conv['id']}/title", headers=key).json()["title"] == titled["title"]
        assert up.calls == 1  # déjà titrée : pas d'appel
    broken = Upstream(title=None)
    c, _ = make(up=broken)
    with c:
        key = new_key(c)
        long = "Peux-tu m'expliquer   en détail comment fonctionne le routage entre IA locales et cloud ?"
        conv = c.post("/v1/conversations", headers=key, json={"messages": exchange(long, "Oui.")}).json()
        title = c.post(f"/v1/conversations/{conv['id']}/title", headers=key).json()["title"]
        assert broken.calls >= 1 and title == fallback_title(exchange(long, "Oui.")) and title.endswith("…")
        assert "  " not in title and len(title) <= 61


def test_title_never_uses_cloud():
    """Pas de modèle local : début du message, aucun appel (le cloud coûterait et ferait sortir le texte)."""
    up = Upstream()
    c, _ = make(up=up, local=False)
    with c:
        key = new_key(c)
        conv = c.post("/v1/conversations", headers=key, json={"messages": exchange("Bonjour à tous", "Salut")}).json()
        assert c.post(f"/v1/conversations/{conv['id']}/title", headers=key).json()["title"] == "Bonjour à tous"
        assert up.calls == 0


def test_list_search_paging_delete():
    c, _ = make()
    with c:
        key = new_key(c)
        ids = []
        for t in ("Recette de crêpes", "Script Python", "Voyage à Lisbonne", "Python et SQLite"):
            ids.append(c.post("/v1/conversations", headers=key, json={"title": t, "messages": []}).json()["id"])
        data = c.get("/v1/conversations", headers=key).json()
        assert [x["title"] for x in data["data"]][0] == "Python et SQLite" and data["total"] == 4  # récente d'abord
        assert {x["title"] for x in c.get("/v1/conversations?q=python", headers=key).json()["data"]} == {
            "Script Python",
            "Python et SQLite",
        }
        page = c.get("/v1/conversations?limit=3&offset=0", headers=key).json()
        assert len(page["data"]) == 3 and page["has_more"]
        assert c.get("/v1/conversations?limit=abc", headers=key).status_code == 400
        assert c.delete(f"/v1/conversations/{ids[0]}", headers=key).json()["deleted"]
        assert c.get(f"/v1/conversations/{ids[0]}", headers=key).status_code == 404
        assert c.get("/v1/conversations", headers=key).json()["total"] == 3


def test_validation_and_limits():
    c, _ = make()
    with c:
        key = new_key(c)
        for bad in (
            {"messages": "texte"},
            {"messages": [{"role": "pirate", "content": "x"}]},
            {"messages": [{"role": "user", "content": 3}]},
            {"messages": [], "private": "oui"},
        ):
            assert c.post("/v1/conversations", headers=key, json=bad).status_code == 400, bad
        too_many = [{"role": "user", "content": "x"}] * (MAX_MESSAGES + 1)
        assert c.post("/v1/conversations", headers=key, json={"messages": too_many}).status_code == 413
        # champs inconnus retirés (rien d'autre que role / content / meta n'est stocké)
        conv = c.post(
            "/v1/conversations", headers=key, json={"messages": [{"role": "user", "content": "x", "html": "<script>"}]}
        ).json()
        assert c.get(f"/v1/conversations/{conv['id']}", headers=key).json()["messages"] == [
            {"role": "user", "content": "x"}
        ]
        assert c.patch(f"/v1/conversations/{conv['id']}", headers=key, json={"title": "   "}).status_code == 400


def test_anonymous_not_stored(monkeypatch):
    c, _ = make()
    with c:
        assert c.get("/v1/me").json()["conversations"]["stored"] is False
        assert c.post("/v1/conversations", json={"messages": []}).status_code == 403
        assert c.get("/v1/me", headers=new_key(c)).json()["conversations"]["stored"] is True


def test_deleting_key_deletes_its_conversations():
    store = DocumentStore(":memory:")
    c, _ = make(store=store)
    with c:
        created = c.post("/v1/keys", headers=ACCOUNT, json={"name": "jetable"}).json()
        h = {"Authorization": f"Bearer {created['key']}"}
        c.post("/v1/conversations", headers=h, json={"messages": exchange("a", "b")})
        c.post("/v1/conversations", headers=ACCOUNT, json={"messages": exchange("c", "d")})
        assert c.delete(f"/v1/keys/{created['id']}", headers=ACCOUNT).status_code == 200
    from gateway.plans import account_identity

    owner = account_identity(ACCOUNT_KEY)
    assert store.count_conversations(owner, f"key:{created['id']}") == 0
    assert store.count_conversations(owner, "account") == 1  # celles du compte restent


def test_retention_purge():
    store = DocumentStore(":memory:")
    from gateway.conversations import Conversations
    from gateway.crypto import Cipher
    import secrets

    clock = {"now": 1_000_000.0}
    conv = Conversations(store, Cipher(secrets.token_bytes(32)), clock=lambda: clock["now"])
    old = conv.create("acct:x", "account", [])["id"]
    clock["now"] += 40 * 86400
    recent = conv.create("acct:x", "account", [])["id"]
    assert store.purge_conversations(clock["now"] - 30 * 86400) == 1
    assert [x["id"] for x in conv.list("acct:x", "account")["data"]] == [recent] and old != recent


def test_chat_page_has_panel():
    c, _ = make()
    with c:
        page = c.get("/app?frame=1").text
        assert 'id="conv-panel"' in page and 'id="conv-btn"' in page and "/static/conversations.css" in page
        js = c.get("/static/conversations-ui.js")
        assert js.status_code == 200 and "textContent = c.title" in js.text  # titres en texte brut
        chat = c.get("/static/chat.js").text
        assert "/v1/conversations" in chat and "state.generation" in chat  # réponse liée à sa conversation


def test_unreadable_after_key_change():
    """Clé de chiffrement remplacée sans être restaurée : la liste reste utilisable, l'ouverture explique."""
    import secrets
    from gateway.conversations import ConversationError, Conversations, UNREADABLE
    from gateway.crypto import Cipher

    store = DocumentStore(":memory:")
    conv_id = Conversations(store, Cipher(secrets.token_bytes(32))).create("acct:x", "account", [], "Titre")["id"]
    other = Conversations(store, Cipher(secrets.token_bytes(32)))
    assert other.list("acct:x", "account")["data"][0]["title"] == UNREADABLE
    with pytest.raises(ConversationError) as e:
        other.get("acct:x", "account", conv_id)
    assert e.value.status == 500 and "import-key" in e.value.message
