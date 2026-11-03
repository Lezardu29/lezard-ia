"""Mémoire du contrôle du PC : préférences dictées par l'utilisateur et guide des applications de ce PC (écrit par une
IA forte d'après l'inventaire, lu par le modèle sans encombrer son contexte)."""

import asyncio
import json

import httpx
import pytest

from gateway import tools
from gateway.store import DocumentStore
from gateway.tools import ToolError, app_guides, app_scan, memory, runner

from .test_gateway_tools import ASK, MASTER, Scripted, make_client

GUIDE = {
    "updated_at": "2026-10-25",
    "apps": [
        {"id": "arc", "name": "Arc", "role": "navigateur principal", "usage": "souvent",
         "aliases": ["navigateur", "youtube", "site"], "guide": ["Ctrl+T : nouvel onglet."]},
        {"id": "discord", "name": "Discord", "role": "messagerie", "usage": "souvent",
         "aliases": ["salon", "message privé"], "guide": ["Ctrl+K : aller vers un ami ou un salon."]},
        {"id": "steam", "name": "Steam", "role": "jeux", "usage": "parfois", "aliases": ["jeu", "terraria"],
         "guide": ["BIBLIOTHÈQUE puis JOUER."]},
        {"id": "edge", "name": "Microsoft Edge", "role": "navigateur", "usage": "jamais", "aliases": ["edge"],
         "guide": []},
        {"name": "sans id"},
    ],
}  # fmt: skip


@pytest.fixture
def guide_file(tmp_path, monkeypatch):
    p = tmp_path / "pc_apps.json"
    p.write_text(json.dumps(GUIDE), encoding="utf-8")
    monkeypatch.setenv("PC_APP_GUIDES", str(p))
    return p


def test_store_dedupes_and_caps():
    s = DocumentStore(":memory:")
    a = s.add_memory("me", "preference", "Firefox, pas Edge", "agent", 5)
    b = s.add_memory("me", "preference", "Firefox, pas Edge", "user", 5)
    assert a["id"] == b["id"] and b["hits"] == 2 and b["source"] == "user"  # dicté par l'utilisateur : le reste
    for i in range(6):
        s.add_memory("me", "lesson", f"leçon {i}", "auto", 5)
    rows = s.list_memory("me")
    assert len(rows) == 5 and any(r["source"] == "user" for r in rows)  # les anciennes leçons auto partent d'abord
    assert s.list_memory("autre") == []
    assert s.delete_memory("autre", rows[0]["id"]) == 0 and s.delete_memory("me", rows[0]["id"]) == 1
    assert s.delete_memory("me") == 4


def test_store_same_timestamp_keeps_newest():
    """Horloge de Windows (~15 ms) : plusieurs souvenirs au même instant, le dernier écrit n'est jamais effacé."""
    from unittest import mock

    with mock.patch("gateway.store.time.time", return_value=1000.0):
        s = DocumentStore(":memory:")
        for i in range(8):
            assert s.add_memory("me", "lesson", f"leçon {i}", "auto", 5)["text"] == f"leçon {i}"
        assert [r["text"] for r in s.list_memory("me")][0] in {f"leçon {i}" for i in range(3, 8)}
        assert len(s.list_memory("me")) == 5


def test_only_preferences_are_recalled():
    s = DocumentStore(":memory:")
    s.add_memory("me", "preference", "J'utilise Arc", "user", 50)
    s.add_memory("me", "lesson", "ancienne leçon automatique", "auto", 50)
    assert memory.recall(s, "me") == ["J'utilise Arc"]  # les leçons automatiques ne sont plus rappelées
    assert "J'utilise Arc" in memory.guide(memory.recall(s, "me")) and memory.guide([]) == ""
    for kind, bad in (("autre", "x"), ("lesson", "  "), ("lesson", "x" * 301)):
        with pytest.raises(memory.InvalidMemory):
            memory.clean(kind, bad)


def test_guide_index_and_only_the_apps_of_the_request(guide_file):
    text = app_guides.guide("envoie « salut » à Lucas sur Discord")
    assert "- Arc : navigateur principal (souvent)" in text and "- Steam : jeux (parfois)" in text
    assert "pas utilisées récemment" in text and "Microsoft Edge" in text
    assert "Ctrl+K" in text and "Ctrl+T" not in text and "JOUER" not in text  # seul le guide de Discord, en entier
    assert "sans id" not in text  # entrée abîmée ignorée
    assert "JOUER" in app_guides.guide("lance terraria") and "Ctrl+T" in app_guides.guide("ouvre YouTube")
    assert "Guide des applications de cette demande" not in app_guides.guide("quelle heure est-il")
    both = app_guides.guide("ouvre un site puis le salon discord")
    assert "Ctrl+T" in both and "Ctrl+K" in both
    assert len(app_guides.guide("ouvre discord")) < 1500  # l'index reste court


def test_guide_tool_reads_one_app(guide_file):
    tool = tools.TOOLS["pc_app_guide"]
    assert tool.level == "pc" and not tool.confirm  # lecture : jamais de validation
    out = asyncio.run(tool.run({"app": "discord"}))
    assert "Ctrl+K" in out.text and "Discord (messagerie, utilisée souvent)" in out.text
    with pytest.raises(ToolError, match="Arc, Discord"):
        asyncio.run(tool.run({"app": "Photoshop"}))


def test_guide_missing_broken_and_reloaded(tmp_path, monkeypatch):
    p = tmp_path / "pc_apps.json"
    monkeypatch.setenv("PC_APP_GUIDES", str(p))
    assert app_guides.load() == [] and app_guides.guide("ouvre discord") == ""
    p.write_text("{pas du json", encoding="utf-8")
    assert app_guides.load() == []
    p.write_text(json.dumps(GUIDE), encoding="utf-8")
    assert [a["name"] for a in app_guides.load()][:2] == ["Arc", "Discord"]  # relu quand le fichier change


class Answers(Scripted):
    """Le modèle répond sans outil : on regarde le message système qu'il a reçu."""

    def __call__(self, request):
        if request.url.path != "/v1/chat/completions":
            return super().__call__(request)
        self.bodies.append(json.loads(request.content))
        msg = {"role": "assistant", "content": "ok"}
        return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "stop", "message": msg}]})


def test_memory_api_and_prompt(monkeypatch, guide_file):
    monkeypatch.setattr(tools, "pc_enabled", lambda: True)
    monkeypatch.setattr(runner, "PC_CONTEXT", 0)  # faux Ollama : API compatible OpenAI seulement
    fake = Answers()
    with make_client(fake) as c:
        assert c.get("/v1/tools/memory").status_code == 403  # pas la clé maître
        empty = c.get("/v1/tools/memory", headers=MASTER).json()
        assert empty["data"] == [] and [a["name"] for a in empty["apps"]][:2] == ["Arc", "Discord"]
        assert c.post("/v1/tools/memory", headers=MASTER, json={"text": ""}).status_code == 400
        r = c.post("/v1/tools/memory", headers=MASTER, json={"text": "  J'utilise   Arc "})
        assert r.status_code == 200 and r.json()["text"] == "J'utilise Arc" and r.json()["source"] == "user"
        names = {t["name"] for t in c.get("/v1/tools", headers=MASTER).json()["data"]}
        assert "pc_app_guide" in names and "pc_memory_save" not in names  # le modèle lit, n'écrit plus
        ask = {**ASK, "messages": [{"role": "user", "content": "ouvre le salon général sur Discord"}]}
        r = c.post("/v1/chat/completions", headers=MASTER, json={**ask, "gateway_tools": True})
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "ok"
        # premier appel : le plan de la tâche ; ensuite, la tâche avec la méthode, les préférences et le guide
        system = next(
            b["messages"][0]["content"]
            for b in fake.bodies
            if str(b["messages"][0].get("content", "")).startswith(tools.PC_GUIDE)
        )
        assert "J'utilise Arc" in system
        assert "Ctrl+K" in system and "BIBLIOTHÈQUE" not in system
        rows = c.get("/v1/tools/memory", headers=MASTER).json()["data"]
        assert c.delete(f"/v1/tools/memory/{rows[0]['id']}", headers=MASTER).json() == {"deleted": 1}
        assert c.delete(f"/v1/tools/memory/{rows[0]['id']}", headers=MASTER).status_code == 404


@pytest.mark.skipif(not hasattr(app_scan, "usage") or __import__("os").name != "nt", reason="Windows seulement")
def test_scan_reads_usage_without_window_titles(tmp_path, monkeypatch):
    monkeypatch.setattr(app_scan, "store_apps", lambda: ["Microsoft.WindowsNotepad"])
    monkeypatch.setattr(app_scan, "running", lambda: ["Code", "explorer"])
    data = app_scan.scan()
    assert set(data) == {"scanned_at", "usage", "running", "installed", "store"}
    assert all({"path", "runs", "focus_minutes", "last_used"} == set(u) for u in data["usage"])
    assert all(set(a) == {"name", "publisher", "location"} for a in data["installed"])
    monkeypatch.setenv("GATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_scan, "scan", lambda: data)
    assert app_scan.main() == 0 and json.loads((tmp_path / "pc_apps_scan.json").read_text(encoding="utf-8"))
    assert app_scan._filetime(0) is None and app_scan._filetime(2**63) is None
