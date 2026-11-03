"""Journal d'audit de l'administration (20.2)."""

import json
import secrets

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import audit, totp
from gateway.audit import safe_url
from gateway.config import ProviderConfig
from gateway.crypto import Cipher
from gateway.main import create_app
from gateway.plans import PlanResolver, account_identity
from gateway.store import DocumentStore
from gateway.totp import code_at

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
ACCOUNT_KEY = "ct-compte-0123456789abcdefghijklmn"
ACCOUNT = {"Authorization": f"Bearer {ACCOUNT_KEY}"}
THIS_PC, ELSEWHERE, OTHER = ("127.0.0.1", 50000), ("203.0.113.5", 50000), ("198.51.100.7", 50000)
PROVIDER_SECRET = "sk-proj-SECRET0123456789abcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def env():
    return {"store": DocumentStore(":memory:"), "cipher": Cipher(secrets.token_bytes(32))}


def make(env, peer=THIS_PC) -> TestClient:
    up = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]}))
    app = create_app(
        [ProviderConfig("openai", "openai", "http://openai/v1", "cloud", enabled=False)],
        httpx.AsyncClient(transport=up),
        store=env["store"],
        cipher=env["cipher"],
        plans=PlanResolver({ACCOUNT_KEY: "pro"}, "free", master_key=MASTER_KEY),
    )
    return TestClient(app, client=peer)


def entries(c, **params):
    r = c.get("/v1/admin/audit", headers=MASTER, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_key_lifecycle_is_logged_without_secrets(env):
    with make(env) as c:
        created = c.post("/v1/keys", headers=ACCOUNT, json={"name": "telephone"}).json()
        secret = created["key"]
        assert c.patch(f"/v1/keys/{created['id']}", headers=ACCOUNT, json={"name": "tel. perso"}).status_code == 200
        assert c.post(f"/v1/keys/{created['id']}/revoke", headers=ACCOUNT, json={"reason": "perdu"}).status_code == 200
        assert c.delete(f"/v1/keys/{created['id']}", headers=ACCOUNT).status_code == 200
        assert (
            c.put("/v1/admin/credentials/openai", headers=MASTER, json={"api_key": PROVIDER_SECRET}).status_code == 200
        )
        log = entries(c)
    actions = [e["action"] for e in reversed(log["data"])]
    assert actions == ["key.create", "key.update", "key.revoke", "key.delete", "credentials.set"]
    first = log["data"][-1]
    assert first["actor_kind"] == "account" and first["actor"] == f"compte {account_identity(ACCOUNT_KEY)}"
    assert first["target"] == created["id"] and first["detail"] == {"name": "telephone", "plan": "pro"}
    assert first["ip"] == "127.0.0.1" and first["status"] == 201 and first["label"] == "Clé virtuelle créée"
    assert log["data"][0]["actor"] == "clé maître" and log["data"][0]["target"] == "openai"
    dump = json.dumps(env["store"].query_audit(limit=None)[0])
    for s in (secret, PROVIDER_SECRET, MASTER_KEY, ACCOUNT_KEY):  # jamais un secret dans le journal
        assert s not in dump


def test_refused_actions_logged_but_not_unknown_credentials(env):
    with make(env, ELSEWHERE) as c:
        r = c.post("/v1/admin/backups/x.db/restore", headers=MASTER, json={"confirm": "x.db"})
        assert r.status_code in (403, 404)
        assert c.post(
            "/v1/keys", headers={"Authorization": "Bearer ct-inconnu-0123456789abcdefghij"}, json={"name": "x"}
        ).status_code in (401, 403)  # inconnu / anonyme : pas journalisé
        assert c.post("/v1/admin/remote/stop", headers=ACCOUNT).status_code == 403  # compte : pas l'admin
    with make(env) as c:
        log = entries(c)["data"]
    assert [(e["action"], e["actor_kind"], e["status"] >= 400) for e in log] == [
        ("remote.stop", "account", True),
        ("backup.restore", "master", True),
    ]
    assert all(e["ip"] == "203.0.113.5" for e in log)


def test_action_from_unknown_ip_raises_a_notification(env):
    owner = account_identity(MASTER_KEY)
    with make(env) as c:  # ce PC : jamais d'alerte
        c.post("/v1/admin/backups", headers=MASTER)
    assert env["store"].list_notifications(owner)[1] == 0
    with make(env, ELSEWHERE) as c:
        assert c.post("/v1/keys", headers=MASTER, json={"name": "a"}).status_code == 201
        assert c.post("/v1/keys", headers=MASTER, json={"name": "b"}).status_code == 201  # adresse connue désormais
    notes, total = env["store"].list_notifications(owner)
    assert total == 1 and notes[0]["data"]["ip"] == "203.0.113.5" and "Clé virtuelle créée" in notes[0]["message"]
    with make(env, OTHER) as c:
        assert c.post("/v1/admin/remote/stop", headers=ACCOUNT).status_code == 403  # échec : pas d'alerte
    assert env["store"].list_notifications(owner)[1] == 1


def test_filters_pagination_and_access(env):
    with make(env) as c:
        for i in range(5):
            c.post("/v1/keys", headers=ACCOUNT, json={"name": f"k{i}"})
        c.post("/v1/admin/backups", headers=MASTER)  # base en mémoire : refusée (échec journalisé)
        assert c.get("/v1/admin/audit", headers=ACCOUNT).status_code == 403
        assert entries(c, action="key")["total"] == 5
        assert entries(c, action="backup.create")["total"] == 1
        assert entries(c, actor_kind="master")["total"] == 1
        assert entries(c, result="fail")["total"] == 1 and entries(c, result="ok")["total"] == 5
        assert entries(c, q="127.0.0")["total"] == 6
        page = entries(c, limit=2, offset=2)
        assert page["total"] == 6 and len(page["data"]) == 2
        assert {"id": "totp.enable", "label": "Double authentification activée"} in page["actions"]
        assert c.get("/v1/admin/audit", headers=MASTER, params={"action": "nimporte"}).status_code == 400


def test_csv_export_neutralises_formulas(env):
    with make(env) as c:
        c.delete("/v1/admin/credentials/=HYPERLINK(1)", headers=MASTER)  # cible saisie : une « formule »
        r = c.get("/v1/admin/audit/export", headers=MASTER)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    import csv

    rows = list(csv.reader(r.text.lstrip(chr(0xFEFF)).splitlines(), delimiter=";"))
    assert rows[0][:5] == ["date_utc", "acteur", "type_acteur", "ip", "action"]
    assert rows[1][6] == "'=HYPERLINK(1)" and rows[1][8] == "refusé"
    assert not any(cell[:1] in ("=", "+", "-", "@") for row in rows[1:] for cell in row)


def test_retention_setting_and_purge(env, monkeypatch):
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "90")
    assert audit.retention_days(env["store"]) == 90
    with make(env) as c:
        assert c.put("/v1/admin/audit/settings", headers=MASTER, json={"retention_days": -1}).status_code == 400
        r = c.put("/v1/admin/audit/settings", headers=MASTER, json={"retention_days": 30})
        assert r.status_code == 200 and entries(c)["retention_days"] == 30
        assert entries(c, action="audit.settings")["data"][0]["detail"] == {"retention_days": 30}
    assert audit.retention_days(env["store"]) == 30
    env["store"].add_audit(
        {"at": 1.0, "actor_kind": "master", "actor": "clé maître", "ip": "x", "action": "key.create", "status": 201}
    )
    assert env["store"].purge_audit(1000.0) == 1 and env["store"].query_audit()[1] == 2  # les 2 réglages restent


def test_lan_mode_change_is_logged(env, monkeypatch):
    with make(env) as c:
        assert entries(c)["total"] == 0  # premier démarrage, local : rien
    monkeypatch.setenv("GATEWAY_BIND", "0.0.0.0")
    with make(env) as c:
        pass
    with make(env) as c:  # pas de nouvel événement sans changement
        pass
    monkeypatch.delenv("GATEWAY_BIND")
    with make(env) as c:
        log = entries(c, action="lan")["data"]
    assert [(e["action"], e["actor_kind"]) for e in log] == [("lan.disabled", "system"), ("lan.enabled", "system")]
    assert log[1]["detail"] == {"enabled": True, "bind": "0.0.0.0"}


def test_admin_session_and_totp_events(env):
    import time as _t

    with make(env) as local:
        secret = local.post("/v1/admin/totp/setup", headers=MASTER).json()["secret"]
        assert (
            local.post(
                "/v1/admin/totp/enable", headers=MASTER, json={"code": code_at(secret, int(_t.time() // 30))}
            ).status_code
            == 200
        )
    t = totp.AdminTotp(env["store"], env["cipher"])
    with make(env, ELSEWHERE) as c:
        assert c.post("/v1/admin/session", headers=MASTER, json={"code": "000000"}).status_code == 401
        code = code_at(secret, int(_t.time() // 30) + 1)
        assert c.post("/v1/admin/session", headers=MASTER, json={"code": code}).status_code == 200
        session_id = c.get("/v1/admin/totp", headers=MASTER).json()["sessions"][0]["id"]
        assert c.put("/v1/admin/audit/settings", headers=MASTER, json={"retention_days": 400}).status_code == 200
        log = entries(c)["data"]
    assert t.enabled()
    assert [e["action"] for e in reversed(log)] == [
        "totp.enable",
        "admin_session.open",
        "admin_session.open",
        "audit.settings",
    ]
    failed, opened, backup = log[2], log[1], log[0]
    assert failed["status"] == 401 and opened["detail"] == {"via": "totp"}
    assert backup["actor"] == f"clé maître (session {session_id[:8]})"


def test_safe_url_strips_credentials():
    assert safe_url("http://admin:pass@192.168.1.20:11434/v1?key=abc") == "http://192.168.1.20:11434/v1"
    assert safe_url("pas une url") is None


def test_every_audited_route_exists(env):
    """Une faute de frappe dans la table laisserait une action hors du journal sans que rien ne le signale."""
    import re

    def walk(routes):  # FastAPI récent : routeurs inclus imbriqués ; ancien : liste à plat
        for r in routes:
            if hasattr(r, "original_router"):
                yield from walk(r.original_router.routes)
            elif getattr(r, "methods", None):
                yield r

    routes = [(m, re.sub(r"\{[^}]+\}", "x", r.path)) for r in walk(make(env).app.routes) for m in r.methods]
    for method, pattern, action, _ in audit.ACTIONS:
        assert any(m == method and pattern.match(p) for m, p in routes), action
