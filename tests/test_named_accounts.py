"""Comptes nommés gérés depuis l'interface (20.3) : créés par la clé maître, jeton généré et haché, désactivation
(clés virtuelles comprises), nouveau jeton sans perte, suppression ; GATEWAY_KEYS toujours lu."""

import sqlite3

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import MIN_TOKEN_LENGTH, PlanResolver
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
ENV_KEY = "ct-compte-env-0123456789abcdefghij"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def make(store=None):
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local", enabled=False)],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))),
        plans=PlanResolver({ENV_KEY: "free"}, "free", master_key=MASTER_KEY, require_key=True),
        store=store or DocumentStore(":memory:"),
    )
    return TestClient(app)


def create(c, name="Alice", plan="pro"):
    r = c.post("/v1/admin/accounts", headers=MASTER, json={"name": name, "plan": plan})
    assert r.status_code == 201, r.text
    return r.json()


def test_master_creates_an_account_that_can_manage_its_keys(tmp_path):
    store = DocumentStore(str(tmp_path / "g.db"))
    with make(store) as c:
        acc = create(c)
        token = acc["token"]
        assert token.startswith("sk-acct-") and len(token) >= MIN_TOKEN_LENGTH + 20  # garde-fou 8.1
        assert acc["token_prefix"] == f"{token[:12]}…{token[-4:]}" and "token_hash" not in acc
        me = c.get("/v1/me", headers=bearer(token)).json()
        assert me["plan"] == "pro" and me["can_manage_keys"]
        key = c.post("/v1/keys", headers=bearer(token), json={"name": "téléphone"})
        assert key.status_code == 201
        assert c.get("/v1/me", headers=bearer(key.json()["key"])).status_code == 200
        listed = c.get("/v1/admin/accounts", headers=MASTER).json()
        assert [a["name"] for a in listed["data"]] == ["Alice"] and "token" not in listed["data"][0]
        assert listed["data"][0]["last_used_at"]
        assert [e["source"] for e in listed["env"]] == ["env"] and listed["env"][0]["plan"] == "free"
        assert c.get("/v1/me", headers=bearer(ENV_KEY)).status_code == 200  # GATEWAY_KEYS toujours lu
    store.close()
    dump = "\n".join(sqlite3.connect(tmp_path / "g.db").iterdump())
    assert token not in dump and token[8:] not in dump  # jeton jamais en clair


def test_only_the_master_key_manages_accounts():
    with make() as c:
        acc = create(c)
        for headers in (bearer(acc["token"]), bearer(ENV_KEY)):
            assert c.get("/v1/admin/accounts", headers=headers).status_code == 403
            assert c.post("/v1/admin/accounts", headers=headers, json={"name": "x"}).status_code == 403


def test_disabling_blocks_the_token_and_its_keys_immediately():
    with make() as c:
        acc = create(c)
        key = c.post("/v1/keys", headers=bearer(acc["token"]), json={"name": "k"}).json()["key"]
        r = c.patch(f"/v1/admin/accounts/{acc['id']}", headers=MASTER, json={"status": "disabled"})
        assert r.json()["status"] == "disabled"
        for token in (acc["token"], key):
            r = c.get("/v1/me", headers=bearer(token))
            assert r.status_code == 403 and "désactivé" in r.json()["error"]["message"]
        c.patch(f"/v1/admin/accounts/{acc['id']}", headers=MASTER, json={"status": "active", "plan": "free"})
        assert c.get("/v1/me", headers=bearer(acc["token"])).json()["plan"] == "free"  # plan changé à chaud
        assert c.get("/v1/me", headers=bearer(key)).status_code == 200


def test_new_token_keeps_everything_and_old_one_stops():
    with make() as c:
        acc = create(c)
        key = c.post("/v1/keys", headers=bearer(acc["token"]), json={"name": "k"}).json()["key"]
        new = c.post(f"/v1/admin/accounts/{acc['id']}/rotate", headers=MASTER).json()
        assert new["token"] != acc["token"] and new["identity"] == acc["identity"]
        assert c.get("/v1/me", headers=bearer(acc["token"])).status_code == 401
        assert c.get("/v1/keys", headers=bearer(new["token"])).json()["data"][0]["name"] == "k"  # ses clés
        assert c.get("/v1/me", headers=bearer(key)).status_code == 200


def test_delete_revokes_keys():
    with make() as c:
        acc = create(c)
        key = c.post("/v1/keys", headers=bearer(acc["token"]), json={"name": "k"}).json()["key"]
        r = c.delete(f"/v1/admin/accounts/{acc['id']}", headers=MASTER).json()
        assert r == {"deleted": True, "keys_revoked": 1}
        assert c.get("/v1/me", headers=bearer(acc["token"])).status_code == 401
        assert c.get("/v1/me", headers=bearer(key)).status_code == 401  # révoquée
        assert c.delete(f"/v1/admin/accounts/{acc['id']}", headers=MASTER).status_code == 404


def test_validation_and_audit():
    with make() as c:
        create(c, "Alice")
        for body, status in (
            ({"name": "alice"}, 409),
            ({"name": " "}, 400),
            ({"name": "B", "plan": "gold"}, 400),
            ({"name": "x" * 61}, 400),
        ):
            assert c.post("/v1/admin/accounts", headers=MASTER, json=body).status_code == status, body
        bob = create(c, "Bob", "free")
        assert c.patch(f"/v1/admin/accounts/{bob['id']}", headers=MASTER, json={"name": "ALICE"}).status_code == 409
        assert c.patch(f"/v1/admin/accounts/{bob['id']}", headers=MASTER, json={"status": "x"}).status_code == 400
        assert c.patch(f"/v1/admin/accounts/{bob['id']}", headers=MASTER, json={"token": "x"}).status_code == 400
        assert c.get("/v1/me", headers=bearer("sk-acct-invente-0123456789abcdefghijklmnop")).status_code == 401
        entries = c.get("/v1/admin/audit", headers=MASTER).json()["data"]
        assert {e["action"] for e in entries if e["status"] < 300} >= {"account.create"}
        assert any(e["target"] == "Bob" for e in entries if e["action"] == "account.create")
