"""Projets du chat : une conversation appartient à un projet (instructions et fichiers communs, 3.2) ; la page
(projects-ui.js) les crée, les ouvre et y démarre des conversations."""

import sqlite3

from gateway import migrations
from gateway.core import WEB_DIR
from gateway.store import DocumentStore
from tests.test_conversations import ACCOUNT, exchange, make, new_key


def project(c, headers=ACCOUNT, **body):
    return c.post("/v1/projects", headers=headers, json={"name": "Coffre", **body}).json()


def test_a_conversation_is_created_in_a_project_and_listed_with_it():
    c, _ = make()
    with c:
        p = project(c, system_prompt="Réponds en une phrase.")
        inside = c.post("/v1/conversations", headers=ACCOUNT, json={"messages": [], "project": p["id"]}).json()
        outside = c.post("/v1/conversations", headers=ACCOUNT, json={"messages": []}).json()
        assert inside["project_id"] == p["id"] and outside["project_id"] is None
        assert c.get(f"/v1/conversations/{inside['id']}", headers=ACCOUNT).json()["project_id"] == p["id"]
        listed = c.get(f"/v1/conversations?project={p['id']}", headers=ACCOUNT).json()["data"]
        assert [x["id"] for x in listed] == [inside["id"]]
        assert len(c.get("/v1/conversations", headers=ACCOUNT).json()["data"]) == 2  # « Récents » : toutes


def test_unknown_or_foreign_project_is_refused():
    c, _ = make()
    with c:
        r = c.post("/v1/conversations", headers=ACCOUNT, json={"messages": [], "project": "proj_inconnu"})
        assert r.status_code == 404
        theirs = project(c, headers={"Authorization": "Bearer gw-master-0123456789abcdefghijklmnop"})
        r = c.post("/v1/conversations", headers=ACCOUNT, json={"messages": [], "project": theirs["id"]})
        assert r.status_code == 404  # projet d'un autre propriétaire : invisible
        r = c.post("/v1/conversations", headers=ACCOUNT, json={"messages": [], "project": 3})
        assert r.status_code == 404


def test_a_conversation_moves_into_and_out_of_a_project_without_a_new_revision():
    c, _ = make()
    with c:
        p = project(c)
        conv = c.post("/v1/conversations", headers=ACCOUNT, json={"messages": exchange("a", "b")}).json()
        moved = c.patch(f"/v1/conversations/{conv['id']}", headers=ACCOUNT, json={"project": p["id"]}).json()
        assert moved["project_id"] == p["id"] and moved["revision"] == conv["revision"]
        titled = c.patch(f"/v1/conversations/{conv['id']}", headers=ACCOUNT, json={"title": "Autre"}).json()
        assert titled["project_id"] == p["id"]  # sans « project » : inchangé
        out = c.patch(f"/v1/conversations/{conv['id']}", headers=ACCOUNT, json={"project": None}).json()
        assert out["project_id"] is None


def test_deleting_a_project_keeps_its_conversations_detached():
    c, _ = make()
    with c:
        p = project(c)
        conv = c.post("/v1/conversations", headers=ACCOUNT, json={"messages": [], "project": p["id"]}).json()
        assert c.delete(f"/v1/projects/{p['id']}", headers=ACCOUNT).status_code == 200
        assert c.get(f"/v1/conversations/{conv['id']}", headers=ACCOUNT).json()["project_id"] is None


def test_projects_belong_to_the_account_and_conversations_to_each_key():
    c, _ = make()
    with c:
        phone = new_key(c)
        p = project(c)  # projet du compte : visible avec chacune de ses clés
        conv = c.post("/v1/conversations", headers=phone, json={"messages": [], "project": p["id"]}).json()
        assert conv["project_id"] == p["id"]
        assert c.get(f"/v1/conversations?project={p['id']}", headers=ACCOUNT).json()["data"] == []


def test_older_databases_gain_the_project_column(tmp_path):
    path = tmp_path / "ancienne.db"
    DocumentStore(str(path))  # schéma actuel, puis la colonne retirée comme avant la migration 2
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE conversations DROP COLUMN project_id")
    conn.execute("DELETE FROM schema_version WHERE version >= 2")
    conn.commit()
    conn.close()
    DocumentStore(str(path))
    conn = sqlite3.connect(path)
    assert "project_id" in migrations.columns(conn, "conversations")
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == migrations.LATEST
    conn.close()


def test_the_page_sends_the_project_with_each_message():
    chat = (WEB_DIR / "static" / "chat.js").read_text(encoding="utf-8")
    page = (WEB_DIR / "static" / "projects-ui.js").read_text(encoding="utf-8")
    assert "...(state.project && { project: state.project })" in chat  # instructions et fichiers appliqués
    assert "body: { messages, private: state.private, project: state.project }" in chat
    assert "state.project = conv.project_id || null;" in chat
    assert 'form.append("project", p.id)' in page  # fichier rattaché au projet par la gateway
    assert "innerHTML" not in page  # noms, descriptions, fichiers : texte brut
    assert "ui.enabled = server && !!me.conversations?.stored" in page
