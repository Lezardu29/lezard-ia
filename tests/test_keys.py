import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.keys import VirtualKeyError, hash_secret, validate_key
from gateway.main import create_app
from gateway.plans import PlanError, PlanResolver
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer master-secret"}
FREE_ACCOUNT = {"Authorization": "Bearer free-account"}


class Upstream:
    def __init__(self):
        self.bodies = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
        self.bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )


def make(require_key=False):
    up = Upstream()
    store = DocumentStore(":memory:")
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({"free-account": "free"}, "free", master_key="master-secret", require_key=require_key),
        store=store,
    )
    return TestClient(app), up, store


def bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


CHAT = {"model": "ollama/llama3.1:8b", "messages": [{"role": "user", "content": "yo"}]}


# ---------- unités ----------


def test_validate_key():
    d = validate_key({"name": " Bot Slack ", "metadata": {"env": "prod", "v": 2}}, "pro")
    assert d == {"name": "Bot Slack", "metadata": {"env": "prod", "v": 2}, "plan": "pro"}
    assert validate_key({"name": "x", "plan": "test"}, "free")["plan"] == "free"  # ancien nom : lu comme free
    for bad, status in [
        ({}, 400),
        ({"name": "x", "plan": "pro"}, 403),
        ({"name": "x", "metadata": {"a": {"nested": 1}}}, 400),
        ({"name": "x", "metadata": {str(i): i for i in range(21)}}, 400),
    ]:
        with pytest.raises(VirtualKeyError) as e:
            validate_key(bad, "free")
        assert e.value.status == status


def test_resolver_identity_is_hashed_and_require_key():
    r = PlanResolver({"tok": "pro"}, "free", master_key="")
    ctx = r.resolve("tok", "1.2.3.4", None)
    assert ctx.can_manage and ctx.plan == "pro" and "tok" not in ctx.identity
    assert r.resolve(None, "1.2.3.4", None).identity == "ip:1.2.3.4"
    strict = PlanResolver({}, "free", master_key="", require_key=True)
    with pytest.raises(PlanError) as e:
        strict.resolve(None, "x", None)
    assert e.value.status == 401


# ---------- API ----------


def test_create_use_and_secret_never_stored():
    c, up, store = make()
    with c:
        r = c.post(
            "/v1/keys",
            headers=MASTER,
            json={"name": "App mobile", "description": "iOS", "metadata": {"team": "mobile"}},
        )
        assert r.status_code == 201
        k = r.json()
        secret = k["key"]
        assert secret.startswith("sk-app-") and len(secret) > 40
        assert k["key_prefix"] == f"{secret[:11]}…{secret[-4:]}" and k["plan"] == "pro"
        assert k["status"] == "active" and k["last_used_at"] is None
        # le secret n'est stocké nulle part en clair
        dump = "\n".join(store._db.iterdump())
        assert secret not in dump and hash_secret(secret) in dump
        # ni renvoyé ensuite
        listed = c.get("/v1/keys", headers=MASTER).json()["data"]
        assert len(listed) == 1 and "key" not in listed[0] and "key_hash" not in listed[0]

        r = c.post("/v1/chat/completions", headers=bearer(secret), json=CHAT).json()
        assert r["x_gateway"]["key"] == {"id": k["id"], "name": "App mobile"}
        assert r["x_gateway"]["plan"] == "pro"
        assert c.get(f"/v1/keys/{k['id']}", headers=MASTER).json()["last_used_at"] is not None
        me = c.get("/v1/me", headers=bearer(secret)).json()
        assert me["type"] == "virtual_key" and me["key"]["name"] == "App mobile" and not me["can_manage_keys"]
        assert c.get("/v1/me", headers=MASTER).json()["type"] == "account"


def test_invalid_or_unknown_virtual_key_is_rejected():
    c, _, _ = make()
    with c:
        r = c.post("/v1/chat/completions", headers=bearer("sk-app-inventee"), json=CHAT)
        assert r.status_code == 401  # jamais de repli sur le plan par défaut
        assert c.get("/v1/me", headers=bearer("sk-app-x")).status_code == 401


def test_virtual_key_shares_account_data_and_cannot_manage_keys():
    c, _, _ = make()
    with c:
        proj = c.post("/v1/projects", headers=MASTER, json={"name": "Support"}).json()
        doc = c.post(
            "/v1/documents", headers=MASTER, json={"filename": "faq.txt", "content": "Retour sous 30 jours."}
        ).json()
        secret = c.post("/v1/keys", headers=MASTER, json={"name": "bot"}).json()["key"]
        h = bearer(secret)
        assert c.get(f"/v1/projects/{proj['id']}", headers=h).status_code == 200  # même compte
        assert c.get(f"/v1/documents/{doc['id']}", headers=h).status_code == 200
        for method, url in [("post", "/v1/keys"), ("get", "/v1/keys")]:
            r = getattr(c, method)(url, headers=h, **({"json": {"name": "escalade"}} if method == "post" else {}))
            assert r.status_code == 403
        # un autre compte ne voit ni les clés ni les données
        assert c.get("/v1/keys", headers=FREE_ACCOUNT).json()["data"] == []
        assert c.get(f"/v1/projects/{proj['id']}", headers=FREE_ACCOUNT).status_code == 404


def test_key_default_project_and_plan_cap():
    c, up, _ = make()
    with c:
        proj = c.post(
            "/v1/projects", headers=MASTER, json={"name": "Juridique", "system_prompt": "Tu es juriste."}
        ).json()
        k = c.post("/v1/keys", headers=MASTER, json={"name": "legal", "project": proj["id"], "plan": "free"}).json()
        assert k["project_id"] == proj["id"] and k["plan"] == "free"
        r = c.post(
            "/v1/chat/completions", headers=bearer(k["key"]), json={"messages": [{"role": "user", "content": "?"}]}
        ).json()  # pas de modèle : celui du projet
        assert up.bodies[-1]["messages"][0]["content"] == "Tu es juriste."
        assert r["x_gateway"]["project"]["id"] == proj["id"] and r["x_gateway"]["plan"] == "free"
        # la clé ne peut pas dépasser son plan
        assert (
            c.post("/v1/chat/completions", headers={**bearer(k["key"]), "X-Gateway-Plan": "pro"}, json=CHAT).status_code
            == 403
        )
        # un compte free ne peut pas créer de clé pro ; projet d'un autre compte refusé
        assert c.post("/v1/keys", headers=FREE_ACCOUNT, json={"name": "x", "plan": "pro"}).status_code == 403
        assert c.post("/v1/keys", headers=FREE_ACCOUNT, json={"name": "x", "project": proj["id"]}).status_code == 404
        # supprimer le projet détache la clé sans la casser
        c.delete(f"/v1/projects/{proj['id']}", headers=MASTER)
        assert c.get(f"/v1/keys/{k['id']}", headers=MASTER).json()["project_id"] is None
        assert c.post("/v1/chat/completions", headers=bearer(k["key"]), json=CHAT).status_code == 200


def test_update_rotate_delete():
    c, _, _ = make()
    with c:
        k = c.post("/v1/keys", headers=MASTER, json={"name": "ci"}).json()
        upd = c.patch(
            f"/v1/keys/{k['id']}", headers=MASTER, json={"name": "CI GitHub", "metadata": {"repo": "gateway"}}
        ).json()
        assert upd["name"] == "CI GitHub" and upd["metadata"] == {"repo": "gateway"} and upd["plan"] == "pro"
        new = c.post(f"/v1/keys/{k['id']}/rotate", headers=MASTER).json()
        assert new["key"] != k["key"] and new["id"] == k["id"]
        assert c.post("/v1/chat/completions", headers=bearer(k["key"]), json=CHAT).status_code == 401
        assert c.post("/v1/chat/completions", headers=bearer(new["key"]), json=CHAT).status_code == 200
        assert c.delete(f"/v1/keys/{k['id']}", headers=MASTER).json()["deleted"]
        assert c.post("/v1/chat/completions", headers=bearer(new["key"]), json=CHAT).status_code == 401
        assert c.get(f"/v1/keys/{k['id']}", headers=MASTER).status_code == 404


def test_anonymous_cannot_manage_and_require_key_mode():
    c, _, _ = make()
    with c:
        assert c.post("/v1/keys", json={"name": "x"}).status_code == 403
        assert c.post("/v1/chat/completions", json=CHAT).status_code == 200  # anonyme toléré
    c, _, _ = make(require_key=True)
    with c:
        assert c.post("/v1/chat/completions", json=CHAT).status_code == 401
        secret = c.post("/v1/keys", headers=MASTER, json={"name": "seule voie"}).json()["key"]
        assert c.post("/v1/chat/completions", headers=bearer(secret), json=CHAT).status_code == 200
        assert (
            c.post(
                "/v1/messages",
                headers={"x-api-key": secret},
                json={"model": "ollama/llama3.1:8b", "max_tokens": 5, "messages": [{"role": "user", "content": "yo"}]},
            ).status_code
            == 200
        )  # en-tête Anthropic
