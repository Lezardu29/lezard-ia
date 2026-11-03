import base64
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.crypto import Cipher, CryptoError, derive_key, load_key, main as crypto_cli
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer master"}
ACCOUNT = {"Authorization": "Bearer compte"}
REAL_SECRET = "sk-proj-VRAIE-CLE-OPENAI-1234567890abcd"


# ---------- chiffrement ----------


def test_aes_gcm_roundtrip_and_tamper_detection():
    c = Cipher(derive_key("phrase de passe"))
    token = c.encrypt(REAL_SECRET, "openai")
    assert token.startswith("v1:") and REAL_SECRET not in token
    assert c.encrypt(REAL_SECRET, "openai") != token  # nonce aléatoire
    assert c.decrypt(token, "openai") == REAL_SECRET
    with pytest.raises(CryptoError):
        c.decrypt(token, "anthropic")  # AAD : lié au provider
    with pytest.raises(CryptoError):
        Cipher(derive_key("autre phrase")).decrypt(token, "openai")  # mauvaise clé
    raw = bytearray(base64.b64decode(token[3:]))
    raw[-1] ^= 1
    with pytest.raises(CryptoError):
        c.decrypt("v1:" + base64.b64encode(bytes(raw)).decode(), "openai")  # altération


def test_key_material_formats(tmp_path, monkeypatch):
    raw = bytes(range(32))
    assert derive_key(base64.b64encode(raw).decode()) == raw
    assert derive_key(raw.hex()) == raw
    assert len(derive_key("une phrase")) == 32 and derive_key("une phrase") != derive_key("autre")
    monkeypatch.delenv("GATEWAY_ENCRYPTION_KEY")
    monkeypatch.setenv("GATEWAY_ENCRYPTION_KEY_FILE", str(tmp_path / "k" / "enc.key"))
    k1 = load_key()
    assert len(k1) == 32 and load_key() == k1  # généré une fois, puis relu


# ---------- API ----------


class Upstream:
    def __init__(self):
        self.auth: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "openai":
            self.auth.append(request.headers.get("authorization", ""))
            if request.headers.get("authorization") != f"Bearer {REAL_SECRET}":
                return httpx.Response(401, text="bad key")
        if request.url.path.endswith("/models"):
            ids = {"ollama": ["llama3.1:8b"], "openai": ["gpt-4o-mini"]}[request.url.host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )


def build(store, up, cipher):
    return TestClient(
        create_app(
            [
                ProviderConfig("ollama", "openai", "http://ollama/v1", "local"),
                ProviderConfig("openai", "openai", "http://openai/v1", "cloud", "OPENAI_KEY_ABSENTE", enabled=False),
            ],
            httpx.AsyncClient(transport=httpx.MockTransport(up)),
            plans=PlanResolver({"compte": "pro"}, "free", master_key="master"),
            store=store,
            cipher=cipher,
        )
    )


CHAT = {"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "yo"}]}


def test_provider_credentials_encrypted_at_rest(tmp_path):
    db, up = str(tmp_path / "gw.db"), Upstream()
    cipher = Cipher(derive_key("cle-instance"))
    with build(DocumentStore(db), up, cipher) as c:
        assert c.post("/v1/chat/completions", json=CHAT).status_code == 400  # provider désactivé
        assert (
            c.put("/v1/admin/credentials/openai", headers=ACCOUNT, json={"api_key": REAL_SECRET}).status_code == 403
        )  # compte ≠ admin
        r = c.put("/v1/admin/credentials/openai", headers=MASTER, json={"api_key": REAL_SECRET}).json()
        assert r == {"provider": "openai", "hint": "…abcd", "enabled": True, "online": True, "error": None}
        assert c.post("/v1/chat/completions", json=CHAT).status_code == 200
        assert up.auth[-1] == f"Bearer {REAL_SECRET}"
        listed = c.get("/v1/admin/credentials", headers=MASTER).json()
        assert listed["data"][0]["hint"] == "…abcd" and REAL_SECRET not in json.dumps(listed)
        assert listed["encryption_key"] == cipher.fingerprint
        assert c.put("/v1/admin/credentials/inconnu", headers=MASTER, json={"api_key": "x" * 10}).status_code == 404
    # le fichier SQLite ne contient pas le secret en clair
    assert REAL_SECRET.encode() not in open(db, "rb").read()

    # redémarrage : le secret est déchiffré au démarrage
    with build(DocumentStore(db), up, cipher) as c:
        assert c.post("/v1/chat/completions", json=CHAT).status_code == 200
    # mauvaise clé de chiffrement : démarrage OK, provider non utilisable
    with build(DocumentStore(db), up, Cipher(derive_key("mauvaise"))) as c:
        assert c.post("/v1/chat/completions", json=CHAT).status_code == 400
        assert c.post("/v1/chat/completions", json={**CHAT, "model": "ollama/llama3.1:8b"}).status_code == 200
    # suppression : le provider redevient indisponible
    with build(DocumentStore(db), up, cipher) as c:
        assert c.delete("/v1/admin/credentials/openai", headers=MASTER).json() == {
            "provider": "openai",
            "deleted": True,
            "enabled": False,
        }
        assert c.post("/v1/chat/completions", json=CHAT).status_code == 400


def test_encryption_key_rotation_cli(tmp_path, capsys):
    db = str(tmp_path / "gw.db")
    store = DocumentStore(db)
    old = Cipher(derive_key("ancienne"))
    store.set_credential("openai", old.encrypt(REAL_SECRET, "openai"), "…abcd", old.fingerprint)
    crypto_cli(["rotate", "--old", "ancienne", "--new", "nouvelle", "--db", db])
    assert "1 secret(s) rechiffré(s)" in capsys.readouterr().out
    store = DocumentStore(db)
    new = Cipher(derive_key("nouvelle"))
    assert new.decrypt(store.get_credential("openai"), "openai") == REAL_SECRET
    assert store.list_credentials()[0]["encryption_key"] == new.fingerprint
    with pytest.raises(CryptoError):
        old.decrypt(store.get_credential("openai"), "openai")
    # mauvaise ancienne clé : rien n'est modifié (transaction)
    with pytest.raises(CryptoError):
        crypto_cli(["rotate", "--old", "fausse", "--new", "x", "--db", db])
    assert new.decrypt(DocumentStore(db).get_credential("openai"), "openai") == REAL_SECRET


# ---------- révocation / suspension ----------


@pytest.fixture
def api(tmp_path):
    with build(DocumentStore(":memory:"), Upstream(), Cipher(derive_key("k"))) as c:

        def key():
            r = c.post("/v1/keys", headers=MASTER, json={"name": "app"}).json()
            return r["id"], {"Authorization": f"Bearer {r['key']}"}

        yield c, key


LOCAL = {"model": "ollama/llama3.1:8b", "messages": [{"role": "user", "content": "yo"}]}


def test_suspend_resume_revoke_are_instant(api):
    c, key = api
    kid, h = key()
    assert c.post("/v1/chat/completions", headers=h, json=LOCAL).status_code == 200
    s = c.post(f"/v1/keys/{kid}/suspend", headers=MASTER, json={"reason": "fuite suspectée"}).json()
    assert s["status"] == "suspended" and s["status_reason"] == "fuite suspectée"
    r = c.post("/v1/chat/completions", headers=h, json=LOCAL)
    assert r.status_code == 403 and "suspendue (fuite suspectée)" in r.json()["error"]["message"]
    assert c.post(f"/v1/keys/{kid}/resume", headers=MASTER).json()["status"] == "active"
    assert c.post("/v1/chat/completions", headers=h, json=LOCAL).status_code == 200
    rv = c.post(f"/v1/keys/{kid}/revoke", headers=MASTER, json={"reason": "départ client"}).json()
    assert rv["status"] == "revoked" and rv["revoked_at"]
    r = c.post("/v1/chat/completions", headers=h, json=LOCAL)
    assert r.status_code == 401 and "révoquée" in r.json()["error"]["message"]
    assert c.post(f"/v1/keys/{kid}/resume", headers=MASTER).status_code == 409  # définitif
    assert c.patch(f"/v1/keys/{kid}", headers=MASTER, json={"status": "active"}).json()["status"] == "revoked"
    events = [e["action"] for e in c.get(f"/v1/keys/{kid}/events", headers=MASTER).json()["data"]]
    assert events == ["created", "suspended", "resumed", "revoked"]


def test_timed_suspension_auto_resumes(api, monkeypatch):
    c, key = api
    kid, h = key()
    c.post(f"/v1/keys/{kid}/suspend", headers=MASTER, json={"duration": "1h", "reason": "pause"})
    r = c.post("/v1/chat/completions", headers=h, json=LOCAL)
    assert r.status_code == 403 and "jusqu'au" in r.json()["error"]["message"]
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3700)
    assert c.post("/v1/chat/completions", headers=h, json=LOCAL).status_code == 200
    assert c.get(f"/v1/keys/{kid}", headers=MASTER).json()["status"] == "active"
    assert c.get(f"/v1/keys/{kid}/events", headers=MASTER).json()["data"][-1]["reason"] == "fin de suspension [auto]"


def test_bulk_incident_response(api):
    c, key = api
    keys = [key() for _ in range(3)]
    c.post(f"/v1/keys/{keys[2][0]}/revoke", headers=MASTER)
    r = c.post("/v1/keys/bulk", headers=MASTER, json={"action": "suspend", "reason": "incident"}).json()
    assert r["count"] == 2 and keys[2][0] not in r["updated"]  # révoquée ignorée
    assert all(c.post("/v1/chat/completions", headers=h, json=LOCAL).status_code == 403 for _, h in keys[:2])
    r = c.post("/v1/keys/bulk", headers=MASTER, json={"action": "resume", "ids": [keys[0][0]]}).json()
    assert r["updated"] == [keys[0][0]]
    assert c.post("/v1/chat/completions", headers=keys[0][1], json=LOCAL).status_code == 200
    assert c.post("/v1/keys/bulk", headers=MASTER, json={"action": "detruire"}).status_code == 400
    assert c.post("/v1/keys/bulk", headers=MASTER, json={"action": "revoke", "ids": ["key_x"]}).status_code == 404
    # une clé virtuelle ne peut pas agir sur les clés, un autre compte ne voit pas les siennes
    assert c.post(f"/v1/keys/{keys[0][0]}/suspend", headers=keys[0][1]).status_code == 403
    assert c.post(f"/v1/keys/{keys[0][0]}/suspend", headers=ACCOUNT).status_code == 404
    assert c.post("/v1/keys/bulk", headers=ACCOUNT, json={"action": "revoke"}).json()["count"] == 0
