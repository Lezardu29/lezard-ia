"""Confort d'édition (19.3) : variantes d'un message gardées avec leur suite, recherche plein texte dans les messages
(déchiffrés en mémoire, jamais indexés en clair sur le disque)."""

import sqlite3

import httpx
from fastapi.testclient import TestClient

from gateway import conversations
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
ACCOUNT_KEY = "ct-compte-0123456789abcdefghij"
ACCOUNT = {"Authorization": f"Bearer {ACCOUNT_KEY}"}
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}


def make(store=None):
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local", enabled=False)],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))),
        plans=PlanResolver({ACCOUNT_KEY: "pro"}, "free", master_key=MASTER_KEY),
        store=store or DocumentStore(":memory:"),
    )
    return TestClient(app)


def user(text, **kw):
    return {"role": "user", "content": text, **kw}


def bot(text, **kw):
    return {"role": "assistant", "content": text, **kw}


# conversation : question modifiée une fois (la 1re version garde sa suite), réponse finale régénérée
EDITED = [
    user(
        "Une recette avec des poireaux ?",
        versions=[
            {"content": "Une recette avec des courgettes ?", "tail": [bot("Gratin de courgettes."), user("Merci")]},
            {"content": "Une recette avec des poireaux ?"},
        ],
        active=1,
    ),
    bot(
        "Fondue de poireaux à la crème.",
        meta={"model": "llama3.1:8b"},
        versions=[{"content": "Tarte aux poireaux."}, {"content": "Fondue de poireaux à la crème."}],
        active=1,
    ),
]


def test_versions_are_kept_with_their_continuation():
    with make() as c:
        conv = c.post("/v1/conversations", headers=ACCOUNT, json={"messages": EDITED}).json()
        got = c.get(f"/v1/conversations/{conv['id']}", headers=ACCOUNT).json()["messages"]
        assert got[0]["active"] == 1 and got[0]["versions"][0]["tail"][1] == {"role": "user", "content": "Merci"}
        assert "tail" not in got[0]["versions"][1]  # la suite de la version active est la liste elle-même
        assert got[1]["versions"][0] == {"content": "Tarte aux poireaux."} and got[1]["meta"]["model"] == "llama3.1:8b"
        assert conv["message_count"] == 2


def test_active_version_tail_is_dropped_and_limits_are_checked():
    with make() as c:
        m = [user("a", versions=[{"content": "a", "tail": [bot("doublon")]}, {"content": "b"}], active=0)]
        got = c.post("/v1/conversations", headers=ACCOUNT, json={"messages": m}).json()
        msgs = c.get(f"/v1/conversations/{got['id']}", headers=ACCOUNT).json()["messages"]
        assert "tail" not in msgs[0]["versions"][0]
        bad = [
            [user("a", versions=[{"content": "a"}], active=0)],  # une seule version
            [user("a", versions=[{"content": "a"}, {"content": "b"}], active=2)],
            [user("a", versions=[{"content": "a"}, {"content": "b"}], active=True)],
            [user("a", versions=[{"content": "a"}, {"content": ""}] * 11, active=0)],  # 22 > 20
            [user("a", versions=[{"content": "a"}, "b"], active=0)],
            [user("a", versions=[{"content": 3}, {"content": "b"}], active=0)],
        ]
        for messages in bad:
            r = c.post("/v1/conversations", headers=ACCOUNT, json={"messages": messages})
            assert r.status_code == 400, messages
        deep = [user("x")]
        for _ in range(conversations.MAX_DEPTH + 2):  # variantes dans des variantes, trop loin
            deep = [user("x", versions=[{"content": "x", "tail": deep}, {"content": "y"}], active=1)]
        assert c.post("/v1/conversations", headers=ACCOUNT, json={"messages": deep}).status_code == 400


def test_search_in_messages_with_snippet_and_index():
    with make() as c:
        c.post("/v1/conversations", headers=ACCOUNT, json={"messages": EDITED, "title": "Cuisine"})
        other = [user("Comment réparer un vélo ?"), bot("Vérifiez d'abord la chaîne et les freins. " * 5)]
        c.post("/v1/conversations", headers=ACCOUNT, json={"messages": other, "title": "Atelier"})
        r = c.get("/v1/conversations?in=messages&q=CRÈME", headers=ACCOUNT).json()
        assert [x["title"] for x in r["data"]] == ["Cuisine"]
        assert r["data"][0]["match"] == {"message": 1, "snippet": "Fondue de poireaux à la crème."}
        r = c.get("/v1/conversations?in=messages&q=freins vélo", headers=ACCOUNT).json()  # tous les mots
        hit = r["data"][0]["match"]
        assert hit["message"] == 0 and hit["snippet"] == "Comment réparer un vélo ?"
        assert c.get("/v1/conversations?in=messages&q=freins cuisine", headers=ACCOUNT).json()["total"] == 0
        assert c.get("/v1/conversations?in=messages&q=atelier", headers=ACCOUNT).json()["total"] == 1  # titre
        long = c.get("/v1/conversations?in=messages&q=chaîne", headers=ACCOUNT).json()["data"][0]["match"]
        assert long["snippet"].endswith("…") and len(long["snippet"]) < 250
        assert c.get("/v1/conversations?q=crème", headers=ACCOUNT).json()["total"] == 0  # titres seulement
        # une autre portée (clé maître = autre compte) ne voit rien
        assert c.get("/v1/conversations?in=messages&q=crème", headers=MASTER).json()["total"] == 0


def test_search_follows_new_revisions_and_nothing_in_clear_on_disk(tmp_path):
    db = tmp_path / "g.db"
    store = DocumentStore(str(db))
    with make(store) as c:
        conv = c.post("/v1/conversations", headers=ACCOUNT, json={"messages": [user("Code du portail 4417")]}).json()
        assert c.get("/v1/conversations?in=messages&q=4417", headers=ACCOUNT).json()["total"] == 1
        r = c.put(
            f"/v1/conversations/{conv['id']}/messages",
            headers=ACCOUNT,
            json={"messages": [user("Code du portail changé")], "revision": conv["revision"]},
        )
        assert r.status_code == 200
        assert c.get("/v1/conversations?in=messages&q=4417", headers=ACCOUNT).json()["total"] == 0  # cache à jour
        assert c.get("/v1/conversations?in=messages&q=changé", headers=ACCOUNT).json()["total"] == 1
    store.close()
    raw = sqlite3.connect(db)
    dump = "\n".join(raw.iterdump())
    assert "portail" not in dump and "4417" not in dump  # ni index ni texte en clair
    assert not raw.execute("SELECT name FROM sqlite_master WHERE sql LIKE '%fts%'").fetchall()


def test_deleting_removes_documents_attached_in_old_versions():
    with make() as c:
        doc = c.post("/v1/documents", headers=ACCOUNT, files={"file": ("note.txt", b"notes de cuisine", "text/plain")})
        assert doc.status_code in (200, 201), doc.text
        doc_id = doc.json()["id"]
        att = [{"id": doc_id, "filename": "note.txt"}]
        m = [
            user("b", versions=[{"content": "a", "attachments": att, "tail": [bot("ok")]}, {"content": "b"}], active=1)
        ]
        conv = c.post("/v1/conversations", headers=ACCOUNT, json={"messages": m}).json()
        assert c.delete(f"/v1/conversations/{conv['id']}", headers=ACCOUNT).status_code in (200, 204)
        assert c.get(f"/v1/documents/{doc_id}", headers=ACCOUNT).status_code == 404
