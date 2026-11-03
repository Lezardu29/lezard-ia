"""Mise à jour assistée (18.3) : migrations numérotées du schéma, version installée, nouvelle version signalée."""

import sqlite3
import subprocess
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import migrations, updates
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.migrations import MigrationError, Step, add_columns, migrate, rebuild_table
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}


def test_fresh_database_gets_all_steps(tmp_path):
    db = tmp_path / "g.db"
    DocumentStore(str(db)).close()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == migrations.LATEST
    assert "embed_model" in migrations.columns(conn, "documents")
    DocumentStore(str(db)).close()  # rouvrir : rien à refaire
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == len(migrations.STEPS)


def test_database_from_before_numbered_migrations_keeps_its_data(tmp_path):
    """Base d'une version antérieure : certaines colonnes déjà ajoutées par l'ancien mécanisme, d'autres non."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE api_keys (id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, description TEXT,
            project_id TEXT, plan TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, key_prefix TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL,
            updated_at REAL NOT NULL, last_used_at REAL, permissions TEXT NOT NULL DEFAULT '{}');
        INSERT INTO api_keys (id, owner, name, plan, key_hash, key_prefix, created_at, updated_at)
            VALUES ('key_1', 'acct:x', 'telephone', 'free', 'h', 'sk-app-ab', 1, 1);
    """)
    conn.commit()
    conn.close()
    s = DocumentStore(str(db))
    key = s.get_key("acct:x", "key_1")
    assert key["name"] == "telephone" and key["limits"] == {} and key["expires_at"] is None
    s.close()


def test_failed_step_rolls_back_and_blocks(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.commit()

    def broken(c):
        c.execute("ALTER TABLE t ADD COLUMN b TEXT")
        raise RuntimeError("panne au milieu")

    steps = [Step(1, "colonne c", add_columns([("t", "c", "TEXT")])), Step(2, "cassée", broken)]
    with pytest.raises(MigrationError) as e:
        migrate(conn, steps)
    assert "Migration 2" in str(e.value) and "schéma 1" in str(e.value)
    assert migrations.columns(conn, "t") == {"a", "c"}  # étape 2 annulée en entier
    assert migrations.current_version(conn) == 1


def test_rebuild_table_changes_a_column_without_losing_data(tmp_path):
    conn = sqlite3.connect(tmp_path / "r.db")
    conn.executescript("""CREATE TABLE notes (id TEXT PRIMARY KEY, score TEXT, owner TEXT);
                          CREATE INDEX notes_owner ON notes(owner);
                          INSERT INTO notes VALUES ('n1', '12', 'a'), ('n2', '7', 'b');""")
    conn.commit()
    step = Step(
        1,
        "score en entier",
        lambda c: rebuild_table(
            c,
            "notes",
            "CREATE TABLE notes (id TEXT PRIMARY KEY, score INTEGER NOT NULL DEFAULT 0, owner TEXT)",
            {"id": "id", "score": "CAST(score AS INTEGER)", "owner": "owner"},
        ),
    )
    assert migrate(conn, [step]) == [1]
    assert conn.execute("SELECT id, score, typeof(score) FROM notes ORDER BY id").fetchall() == [
        ("n1", 12, "integer"),
        ("n2", 7, "integer"),
    ]
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='notes_owner'").fetchone()


def test_newer_schema_is_left_untouched(tmp_path, caplog):
    conn = sqlite3.connect(tmp_path / "n.db")
    migrations.current_version(conn)
    conn.execute("INSERT INTO schema_version VALUES (99, 0, 'venue du futur')")
    conn.commit()
    assert migrate(conn) == []
    assert "plus récent" in caplog.text


def test_steps_numbering_is_checked():
    with pytest.raises(MigrationError):
        migrate(sqlite3.connect(":memory:"), [Step(2, "a", lambda c: None), Step(1, "b", lambda c: None)])
    assert [s.version for s in migrations.STEPS] == list(range(1, len(migrations.STEPS) + 1))


def make():
    app = create_app(
        [ProviderConfig("openai", "openai", "http://openai/v1", "cloud", enabled=False)],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )
    return TestClient(app)


def git_repo(path, *commits):
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)  # noqa: E731
    path.mkdir()
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.org")
    run("config", "user.name", "test")
    for msg in commits:
        (path / "f.txt").write_text(msg, encoding="utf-8")
        run("add", "f.txt")
        run("commit", "-q", "-m", msg)
    return run


@pytest.mark.skipif(shutil.which("git") is None, reason="git absent")
def test_update_check_reports_new_commits(tmp_path, monkeypatch):
    origin_run = git_repo(tmp_path / "origin", "v1", "v2")
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin"), str(tmp_path / "local")], check=True)
    origin_run("commit", "-q", "--allow-empty", "-m", "Nouvelle fonction")
    monkeypatch.setattr(updates, "ROOT", tmp_path / "local")
    with make() as c:
        assert c.get("/v1/admin/update").status_code in (401, 403)
        st = c.get("/v1/admin/update", headers=MASTER).json()
        assert st["installed"]["source"] == "git" and st["installed"]["commit"] and st["enabled"] is False
        assert st["last"] is None and st["command"] == ".\\lancer.ps1 -Update"
        assert "update" not in c.get("/v1/me", headers=MASTER).json()
        r = c.post("/v1/admin/update/check", headers=MASTER).json()["last"]
        assert r["available"] and r["behind"] == 1 and r["commits"][0]["subject"] == "Nouvelle fonction"
        assert c.get("/v1/me", headers=MASTER).json()["update"] == {
            "available": True,
            "behind": 1,
            "version": None,  # copie git : pas de version signée, mise à jour par lancer.ps1 -Update
            "installable": False,
        }
    assert (tmp_path / "local" / "f.txt").read_text(encoding="utf-8") == "v2"  # rien d'installé


def test_update_check_without_git_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "ROOT", tmp_path)  # image Docker, archive
    with make() as c:
        st = c.get("/v1/admin/update", headers=MASTER).json()
        assert st["installed"] == {"version": st["installed"]["version"], "commit": None, "source": "archive"}
        assert st["can_check"] is False
        assert "Pas une copie git" in c.post("/v1/admin/update/check", headers=MASTER).json()["last"]["error"]


def test_update_check_enabled_by_setting(monkeypatch):
    assert not updates.check_enabled()
    monkeypatch.setenv("GATEWAY_UPDATE_CHECK", "on")
    assert updates.check_enabled()
