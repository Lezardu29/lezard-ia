"""Sauvegarde et restauration locales (18.2)."""

import base64
import datetime as dt
import json
import secrets
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import backup
from gateway.backup import BackupError, BackupManager, apply_pending_restore, inspect_file, pending_path
from gateway.config import ProviderConfig
from gateway.crypto import Cipher, CryptoError, derive_key, unwrap_key, wrap_key
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
OPENAI_SECRET = "sk-openai-secret-de-test-1234"
THIS_PC, ELSEWHERE = ("127.0.0.1", 50000), ("203.0.113.5", 50000)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "data" / "gateway.db"
    monkeypatch.setenv("GATEWAY_DB", str(path))
    monkeypatch.delenv("GATEWAY_BACKUP_DIR", raising=False)
    monkeypatch.setenv("GATEWAY_BACKUP", "off")  # sauvegarde automatique : testée à part
    return path


def fill(store: DocumentStore, n: int = 3) -> None:
    for i in range(n):
        store.set_setting(f"essai-{i}", {"i": i})


# ---------- sauvegarde ----------


def test_hot_backup_is_consistent_and_standalone(db):
    store = DocumentStore(str(db))
    fill(store)
    m = BackupManager(store)
    assert m.dir == db.parent / "backups"
    info = m.create()
    fill(store, 5)  # écrit après : absent de la sauvegarde
    path = m.dir / info["name"]
    assert info["label"] == "manuelle" and info["size_bytes"] > 0
    assert not Path(str(path) + "-wal").exists() and not list(m.dir.glob("*.tmp"))
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert con.execute("SELECT COUNT(*) FROM settings WHERE name LIKE 'essai-%'").fetchone()[0] == 3
    con.close()
    report = inspect_file(path)
    assert report["ok"] and report["keys"] == 0 and report["credentials_other_key"] == []
    store.close()


def test_rotation_keeps_7_daily_and_4_weekly(db):
    store = DocumentStore(str(db))
    start = dt.datetime(2026, 1, 1, 3, 0).timestamp()
    clock = {"now": start}
    m = BackupManager(store, clock=lambda: clock["now"])
    for day in range(60):
        for hour in (0, 5):  # deux par jour : seule la plus récente du jour reste
            clock["now"] = start + day * 86400 + hour * 3600
            m.create("auto")
    kept = [dt.datetime.fromtimestamp(b["created_at"]) for b in m.list()]
    days = {k.date() for k in kept}
    assert len(kept) == len(days)  # une par jour au plus
    last = dt.datetime.fromtimestamp(clock["now"]).date()
    assert {last - dt.timedelta(days=i) for i in range(7)} <= days
    assert len({k.isocalendar()[:2] for k in kept}) == 4
    assert len(kept) <= 7 + 4 and all(k.hour == 8 for k in kept)
    store.close()


def test_rotation_spares_manual_and_pre_restore_backups(db):
    """Une sauvegarde manuelle ou automatique du même jour n'efface pas la copie « avant-restauration » (seul
    moyen de revenir en arrière) ; les non automatiques sont plafonnées à part (10)."""
    store = DocumentStore(str(db))
    clock = {"now": dt.datetime(2026, 5, 4, 9).timestamp()}
    m = BackupManager(store, clock=lambda: clock["now"])
    safety = m.create("avant-restauration")["name"]
    for label in ("manuelle", "auto", "manuelle", "auto"):
        clock["now"] += 60
        m.create(label)
    names = [b["name"] for b in m.list()]
    assert safety in names and sum("-manuelle" in n for n in names) == 2 and sum("-auto" in n for n in names) == 1
    for _ in range(15):
        clock["now"] += 60
        m.create("manuelle")
    others = [b for b in m.list() if b["label"] != "auto"]
    assert len(others) == 10 and safety not in {b["name"] for b in others}  # la plus ancienne part en dernier
    store.close()


def test_due_once_a_day(db):
    store = DocumentStore(str(db))
    clock = {"now": dt.datetime(2026, 3, 1, 12).timestamp()}
    m = BackupManager(store, clock=lambda: clock["now"])
    assert m.due()
    m.create("auto")
    clock["now"] += 23 * 3600
    assert not m.due()
    clock["now"] += 3600
    assert m.due()
    store.close()


def test_backup_dir_from_env_and_memory_store(db, tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_BACKUP_DIR", str(tmp_path / "ailleurs"))
    store = DocumentStore(str(db))
    m = BackupManager(store)
    assert m.dir == tmp_path / "ailleurs" and m.create()["name"] in {b["name"] for b in m.list()}
    store.close()
    mem = BackupManager(DocumentStore(":memory:"))
    assert not mem.available and mem.list() == [] and mem.pending() is None
    with pytest.raises(BackupError) as e:
        mem.create()
    assert e.value.status == 409


def test_names_never_become_paths(db):
    store = DocumentStore(str(db))
    m = BackupManager(store)
    m.create()
    for bad in ("../gateway.db", "..\\gateway.db", "C:\\x.db", "gateway.db", "gateway-20260101-000000-../x.db"):
        with pytest.raises(BackupError) as e:
            m.inspect(bad)
        assert e.value.status == 400, bad
    with pytest.raises(BackupError) as e:
        m.inspect("gateway-19990101-000000.db")
    assert e.value.status == 404
    store.close()


# ---------- restauration ----------


def test_stage_and_apply_restore(db):
    store = DocumentStore(str(db))
    store.set_setting("etat", {"v": "ancien"})
    m = BackupManager(store)
    name = m.create()["name"]
    store.set_setting("etat", {"v": "nouveau"})
    report = m.stage_restore(name)
    assert report["restart_required"] and m.pending()["source"] == name
    assert store.get_setting("etat") == {"v": "nouveau"}  # rien de changé à chaud
    store.close()
    result = apply_pending_restore(str(db))
    assert result["source"] == name and "avant-restauration" in result["safety_backup"]
    assert not pending_path(str(db)).exists() and apply_pending_restore(str(db)) is None
    store = DocumentStore(str(db))
    assert store.get_setting("etat") == {"v": "ancien"}
    safety = BackupManager(store).dir / result["safety_backup"]
    con = sqlite3.connect(safety)
    assert json.loads(con.execute("SELECT value FROM settings WHERE name='etat'").fetchone()[0]) == {"v": "nouveau"}
    con.close()
    store.close()


def test_cancel_restore(db):
    store = DocumentStore(str(db))
    m = BackupManager(store)
    m.stage_restore(m.create()["name"])
    assert m.cancel_restore() and m.pending() is None and not m.cancel_restore()
    store.close()
    assert apply_pending_restore(str(db)) is None


def test_restore_refuses_broken_or_foreign_files(db, tmp_path):
    store = DocumentStore(str(db))
    m = BackupManager(store)
    m.dir.mkdir(parents=True)
    (m.dir / "gateway-20260101-000000-corrompue.db").write_bytes(b"pas une base SQLite" * 100)
    other = sqlite3.connect(m.dir / "gateway-20260101-000001-autre.db")
    other.execute("CREATE TABLE t (x)")
    other.commit()
    other.close()
    for name, words in (
        ("gateway-20260101-000000-corrompue.db", "illisible"),
        ("gateway-20260101-000001-autre.db", "Tables manquantes"),
    ):
        with pytest.raises(BackupError) as e:
            m.stage_restore(name)
        assert e.value.status == 422 and words in e.value.message
    assert m.pending() is None
    missing = m.dir / "absent.db"
    assert "introuvable" in inspect_file(missing)["problem"] and not missing.exists()  # rien de créé
    store.close()


def test_backup_with_other_encryption_key_is_flagged(db):
    store = DocumentStore(str(db))
    old = Cipher(secrets.token_bytes(32))
    store.set_credential("openai", old.encrypt(OPENAI_SECRET, "openai"), "…1234", old.fingerprint)
    m = BackupManager(store)
    name = m.create()["name"]
    assert m.inspect(name, old)["credentials_other_key"] == []
    assert m.inspect(name, Cipher(secrets.token_bytes(32)))["credentials_other_key"] == ["openai"]
    store.close()


# ---------- clé de chiffrement ----------


def test_key_export_roundtrip():
    key = secrets.token_bytes(32)
    data = wrap_key(key, "une phrase assez longue")
    assert json.dumps(data) and base64.b64encode(key).decode() not in json.dumps(data)
    assert unwrap_key(data, "une phrase assez longue") == key
    with pytest.raises(CryptoError, match="incorrecte"):
        unwrap_key(data, "une autre phrase longue")
    with pytest.raises(CryptoError, match="trop courte"):
        wrap_key(key, "courte")
    with pytest.raises(CryptoError, match="Paramètres"):
        unwrap_key({**data, "n": 2**30}, "une phrase assez longue")  # calcul démesuré refusé
    with pytest.raises(CryptoError):
        unwrap_key({"format": "autre"}, "x")


def test_cli_import_key(tmp_path, monkeypatch, capsys):
    key = secrets.token_bytes(32)
    export = tmp_path / "cle.json"
    export.write_text(json.dumps(wrap_key(key, "une phrase assez longue")), encoding="utf-8")
    target = tmp_path / "data" / "encryption.key"
    monkeypatch.setenv("GATEWAY_ENCRYPTION_KEY_FILE", str(target))
    monkeypatch.delenv("GATEWAY_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(backup.getpass, "getpass", lambda prompt="": "une phrase assez longue")
    backup.main(["import-key", str(export)])
    assert derive_key(target.read_text(encoding="ascii")) == key and "importée" in capsys.readouterr().out
    backup.main(["import-key", str(export)])
    assert "déjà en place" in capsys.readouterr().out
    other = tmp_path / "autre.json"
    other.write_text(json.dumps(wrap_key(secrets.token_bytes(32), "une phrase assez longue")), encoding="utf-8")
    with pytest.raises(SystemExit, match="AUTRE clé"):
        backup.main(["import-key", str(other)])
    assert derive_key(target.read_text(encoding="ascii")) == key  # rien d'écrasé
    backup.main(["import-key", str(other), "--force"])
    assert derive_key(target.read_text(encoding="ascii")) != key
    assert len(list(target.parent.glob("encryption.key.old-*"))) == 1  # ancienne clé mise de côté


# ---------- API et critère d'acceptation ----------


class Upstream:
    def __init__(self):
        self.auth: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            self.auth.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]})
        return httpx.Response(404)


def make(up: Upstream, peer=THIS_PC) -> TestClient:
    app = create_app(
        [ProviderConfig("openai", "openai", "http://openai/v1", "cloud", enabled=False)],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
    )
    return TestClient(app, client=peer)


def test_backup_lose_database_restore(db):
    """Critère d'acceptation : sauvegarde → base supprimée → restauration → clés virtuelles et clés API des
    fournisseurs à nouveau déchiffrées et utilisables."""
    up = Upstream()
    with make(up) as c:
        secret = c.post("/v1/keys", headers=MASTER, json={"name": "telephone"}).json()["key"]
        assert c.put("/v1/admin/credentials/openai", headers=MASTER, json={"api_key": OPENAI_SECRET}).status_code == 200
        name = c.post("/v1/admin/backups", headers=MASTER).json()["name"]
    for suffix in ("", "-wal", "-shm"):  # le disque lâche : la base disparaît
        Path(str(db) + suffix).unlink(missing_ok=True)
    with make(up) as c:
        assert c.get("/v1/me", headers={"Authorization": f"Bearer {secret}"}).status_code == 401
        listing = c.get("/v1/admin/backups", headers=MASTER).json()
        assert listing["this_pc"] and [b["name"] for b in listing["data"]] == [name]
        assert c.post(f"/v1/admin/backups/{name}/restore", headers=MASTER, json={}).status_code == 400
        r = c.post(f"/v1/admin/backups/{name}/restore", headers=MASTER, json={"confirm": name})
        assert r.status_code == 200 and r.json()["restart_required"] and r.json()["keys"] == 1
        assert r.json()["credentials_other_key"] == []
        assert c.get("/v1/admin/backups", headers=MASTER).json()["pending"]["source"] == name
    up.auth.clear()
    with make(up) as c:  # redémarrage : restauration appliquée
        me = c.get("/v1/me", headers={"Authorization": f"Bearer {secret}"})
        assert me.status_code == 200 and me.json()["type"] == "virtual_key"
        assert f"Bearer {OPENAI_SECRET}" in up.auth  # clé du fournisseur déchiffrée et envoyée
        listing = c.get("/v1/admin/backups", headers=MASTER).json()
        assert listing["pending"] is None and listing["last_restore"]["source"] == name
        assert any(b["label"] == "avant-restauration" for b in listing["data"])
        notes = c.get("/v1/notifications", headers=MASTER).json()["data"]
        assert any(n["title"] == "Sauvegarde restaurée" for n in notes)


def test_daily_backup_at_startup(db, monkeypatch):
    monkeypatch.setenv("GATEWAY_BACKUP", "on")
    with make(Upstream()) as c:
        for _ in range(100):  # tâche de fond, dans un thread
            data = c.get("/v1/admin/backups", headers=MASTER).json()
            if data["data"]:
                break
            time.sleep(0.05)
        assert data["auto"] and [b["label"] for b in data["data"]] == ["auto"]


def test_restore_and_key_export_from_this_pc_only(db):
    up = Upstream()
    with make(up, peer=ELSEWHERE) as c:
        assert c.get("/v1/admin/backups").status_code in (401, 403)  # clé maître requise
        listing = c.get("/v1/admin/backups", headers=MASTER).json()
        assert listing["this_pc"] is False
        name = c.post("/v1/admin/backups", headers=MASTER).json()["name"]  # sauvegarder : permis à distance
        r = c.post(f"/v1/admin/backups/{name}/restore", headers=MASTER, json={"confirm": name})
        assert r.status_code == 403 and "ce PC" in r.text
        r = c.post("/v1/admin/encryption-key/export", headers=MASTER, json={"passphrase": "une phrase assez longue"})
        assert r.status_code == 403
        assert c.get("/v1/admin/backups", headers=MASTER).json()["pending"] is None


def test_key_export_endpoint(db):
    with make(Upstream()) as c:
        assert c.get("/v1/admin/backups", headers=MASTER).json()["key"]["exported"] is None
        assert (
            c.post("/v1/admin/encryption-key/export", headers=MASTER, json={"passphrase": "courte"}).status_code == 400
        )
        r = c.post("/v1/admin/encryption-key/export", headers=MASTER, json={"passphrase": "une phrase assez longue"})
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        assert r.headers["cache-control"] == "no-store"
        key = unwrap_key(r.json(), "une phrase assez longue")
        fingerprint = c.get("/v1/admin/backups", headers=MASTER).json()["key"]
        assert Cipher(key).fingerprint == fingerprint["fingerprint"] == fingerprint["exported"]["fingerprint"]
