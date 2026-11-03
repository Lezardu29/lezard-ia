"""Chat, comme Claude : favoris, partage d'une conversation par lien (lecture seule), préférences personnelles et
styles de réponse (profil), menu « ⋯ » des conversations."""

import hashlib

import pytest

from gateway import preferences
from gateway.core import WEB_DIR
from gateway.store import DocumentStore
from tests.test_conversations import ACCOUNT, exchange, make, new_key

STATIC = WEB_DIR / "static"


def conversation(c, headers=ACCOUNT, messages=None):
    body = {"messages": messages if messages is not None else exchange("Code du coffre ?", "C'est 4417.")}
    return c.post("/v1/conversations", headers=headers, json=body).json()


# ---------- favoris ----------
def test_a_conversation_is_starred_and_unstarred_without_a_new_revision():
    c, _ = make()
    with c:
        conv = conversation(c)
        assert conv["starred"] is False
        on = c.patch(f"/v1/conversations/{conv['id']}", headers=ACCOUNT, json={"starred": True}).json()
        assert on["starred"] is True and on["revision"] == conv["revision"]
        assert c.get("/v1/conversations", headers=ACCOUNT).json()["data"][0]["starred"] is True
        assert c.patch(f"/v1/conversations/{conv['id']}", headers=ACCOUNT, json={"starred": "oui"}).status_code == 400
        off = c.patch(f"/v1/conversations/{conv['id']}", headers=ACCOUNT, json={"starred": False}).json()
        assert off["starred"] is False


# ---------- partage par lien ----------
def test_a_shared_conversation_is_readable_without_a_key_and_frozen():
    c, _ = make()
    with c:
        conv = conversation(c)
        share = c.post(f"/v1/conversations/{conv['id']}/share", headers=ACCOUNT).json()
        assert share["path"] == f"/partage/{share['token']}" and len(share["token"]) >= 40
        public = c.get(f"/v1/shared/{share['token']}")  # aucun en-tête d'autorisation
        assert public.status_code == 200 and public.headers["cache-control"] == "no-store"
        data = public.json()
        assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
        assert data["messages"][1]["model"] == "ollama/llama3.1:8b" and "meta" not in data["messages"][1]
        assert data["title"]  # sans titre : le début de la question
        # copie figée : la suite de la conversation n'y apparaît pas
        c.put(
            f"/v1/conversations/{conv['id']}/messages",
            headers=ACCOUNT,
            json={"messages": exchange("a", "b") + exchange("Autre secret ?", "9999"), "revision": conv["revision"]},
        )
        assert "9999" not in c.get(f"/v1/shared/{share['token']}").text
        status = c.get(f"/v1/conversations/{conv['id']}/share", headers=ACCOUNT).json()
        assert status["shared"] and status["token"] == share["token"]
        assert c.get(f"/partage/{share['token']}").status_code == 200


def test_resharing_replaces_the_link_and_revoking_or_deleting_kills_it():
    c, _ = make()
    with c:
        conv = conversation(c)
        first = c.post(f"/v1/conversations/{conv['id']}/share", headers=ACCOUNT).json()["token"]
        second = c.post(f"/v1/conversations/{conv['id']}/share", headers=ACCOUNT).json()["token"]
        assert first != second
        assert c.get(f"/v1/shared/{first}").status_code == 404
        assert c.delete(f"/v1/conversations/{conv['id']}/share", headers=ACCOUNT).json() == {"deleted": True}
        assert c.get(f"/v1/shared/{second}").status_code == 404
        third = c.post(f"/v1/conversations/{conv['id']}/share", headers=ACCOUNT).json()["token"]
        c.delete(f"/v1/conversations/{conv['id']}", headers=ACCOUNT)
        assert c.get(f"/v1/shared/{third}").status_code == 404


def test_only_the_owner_shares_and_an_empty_conversation_is_refused():
    c, _ = make()
    with c:
        conv = conversation(c)
        phone = new_key(c)  # autre clé du même compte : ses conversations à elle seulement
        assert c.post(f"/v1/conversations/{conv['id']}/share", headers=phone).status_code == 404
        assert c.post(f"/v1/conversations/{conv['id']}/share").status_code in (401, 403)
        empty = conversation(c, messages=[])
        assert c.post(f"/v1/conversations/{empty['id']}/share", headers=ACCOUNT).status_code == 400
        assert c.get("/v1/shared/inconnu").status_code == 404
        assert c.get("/v1/shared/" + "x" * 300).status_code == 404


def test_the_share_token_is_stored_encrypted_and_looked_up_by_hash():
    store = DocumentStore(":memory:")
    c, _ = make(store=store)
    with c:
        conv = conversation(c)
        token = c.post(f"/v1/conversations/{conv['id']}/share", headers=ACCOUNT).json()["token"]
        row = store._db.execute("SELECT id, token, title, body FROM conversation_shares").fetchone()
        assert row[0] == hashlib.sha256(token.encode()).hexdigest()  # recherche par empreinte
        assert token not in row[1] and "4417" not in row[3] and "coffre" not in row[2].lower()


# ---------- profil : préférences personnelles et styles ----------
def test_profile_is_validated_and_kept_per_key():
    c, _ = make()
    with c:
        body = {
            "name": "Tonio",
            "about": "Lycéen, code en Python.",
            "style": "pirate",
            "styles": [{"id": "pirate", "name": "Pirate", "instructions": "Parle comme un pirate."}],
        }
        assert c.put("/v1/preferences/profile", headers=ACCOUNT, json=body).status_code == 200
        assert c.get("/v1/preferences/profile", headers=ACCOUNT).json()["profile"]["style"] == "pirate"
        assert c.get("/v1/preferences/profile", headers=new_key(c)).json()["profile"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {"style": "inconnu"},
        {"name": "x" * 61},
        {"about": "x" * 2001},
        {"styles": [{"id": "concis", "name": "A", "instructions": "B"}]},  # identifiant d'un style fourni
        {"styles": [{"id": "a", "name": "", "instructions": "B"}]},
        {"styles": [{"id": "a", "name": "A", "instructions": "B"}] * 2},
        {
            "styles": [
                {"id": "a", "name": "Pirate", "instructions": "B"},
                {"id": "b", "name": " pirate", "instructions": "C"},
            ]
        },
        {"styles": [{"id": "a", "name": "Concis", "instructions": "B"}]},  # nom d'un style fourni
        {"styles": [{"id": f"s{i}", "name": "A", "instructions": "B"} for i in range(11)]},
        {"autre": 1},
    ],
)
def test_bad_profiles_are_refused(bad):
    with pytest.raises(preferences.PreferenceError):
        preferences.validate_profile(bad)


def test_builtin_styles_match_between_server_and_page():
    page = (STATIC / "profile.js").read_text(encoding="utf-8")
    for sid in preferences.BUILTIN_STYLES:
        assert f'id: "{sid}"' in page


# ---------- la page ----------
def test_the_page_sends_the_profile_but_never_stores_it():
    chat = (STATIC / "chat.js").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "...[state.getSystem()].filter(Boolean)" in chat and "getSystem: profileMessage" in app
    assert '(name ? `, ${name}` : "")' in app  # « Bonsoir, Tonio » par textContent


def test_conversation_menu_and_shared_page_never_interpret_text():
    conv = (STATIC / "conversations-ui.js").read_text(encoding="utf-8")
    share = (STATIC / "share.js").read_text(encoding="utf-8")
    html = (WEB_DIR / "share.html").read_text(encoding="utf-8")
    for label in ("Ajouter aux favoris", "Ranger dans un projet", "Partager", "Supprimer"):
        assert label in conv
    assert "innerHTML" not in conv  # titres, noms de projets : texte brut
    assert "body.textContent = textOf(m.content)" in share and "renderMarkdown(" in share
    assert "/^data:image\\//" in share  # images jointes : data:image seulement
    assert 'content="no-referrer"' in html and "noindex" in html
    # styles du menu « ⋯ » communs au tableau de bord et au chat
    for page in ("app.html", "admin.html"):
        assert 'href="/static/row-menu.css"' in (WEB_DIR / page).read_text(encoding="utf-8")


def test_the_settings_list_styles_compactly_and_refuses_twins():
    page = (STATIC / "profile.js").read_text(encoding="utf-8")
    assert 'smallButton("Modifier"' in page and 'smallButton("Supprimer"' in page  # liste compacte
    assert "const problem = nameProblem(kept);" in page  # doublon signalé avant l'envoi
