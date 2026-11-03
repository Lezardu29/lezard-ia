"""Double authentification de l'administration (20.1) : TOTP, codes de secours, sessions."""

import json
import secrets

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import totp
from gateway.config import ProviderConfig
from gateway.crypto import Cipher
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore
from gateway.totp import AdminTotp, TotpError, code_at

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
ACCOUNT_KEY = "ct-compte-0123456789abcdefghijklmn"
THIS_PC, ELSEWHERE, OTHER = ("127.0.0.1", 50000), ("203.0.113.5", 50000), ("198.51.100.7", 50000)
INTERNET, LAN = ("8.8.8.8", 50000), ("192.168.1.20", 50000)
T0 = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def env():
    return {"store": DocumentStore(":memory:"), "cipher": Cipher(secrets.token_bytes(32)), "clock": Clock()}


def make(env, peer=ELSEWHERE, master=MASTER_KEY) -> TestClient:
    up = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))
    app = create_app(
        [ProviderConfig("openai", "openai", "http://openai/v1", "cloud", enabled=False)],
        httpx.AsyncClient(transport=up),
        store=env["store"],
        cipher=env["cipher"],
        clock=env["clock"],
        plans=PlanResolver({ACCOUNT_KEY: "pro"}, "free", master_key=master),
    )
    return TestClient(app, client=peer)


def current(env, secret, offset=0):
    return code_at(secret, int(env["clock"].now // totp.STEP) + offset)


def enable(env, c) -> tuple[str, list[str]]:
    setup = c.post("/v1/admin/totp/setup", headers=MASTER).json()
    assert setup["uri"].startswith("otpauth://totp/") and "<svg" in setup["svg"]
    r = c.post("/v1/admin/totp/enable", headers=MASTER, json={"code": current(env, setup["secret"])})
    assert r.status_code == 200, r.text
    env["clock"].now += totp.STEP  # le code d'activation est consommé
    return setup["secret"], r.json()["recovery_codes"]


def test_rfc6238_vectors():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890" (RFC 6238, annexe B)
    assert [code_at(secret, int(t // 30)) for t in (59, 1111111109, 1234567890, 2000000000)] == [
        "287082",
        "081804",
        "005924",
        "279037",
    ]


def test_remote_admin_requires_totp_session(env):
    """Critère d'acceptation : depuis une IP non locale, /v1/admin/* refuse la clé maître sans session TOTP ; code
    rejoué refusé ; codes de secours à usage unique."""
    with make(env, THIS_PC) as local:
        secret, recovery = enable(env, local)
        assert len(recovery) == 10 and len(set(recovery)) == 10
        assert local.get("/v1/admin/backups", headers=MASTER).status_code == 200  # ce PC : clé maître seule
    with make(env) as c:
        r = c.get("/v1/admin/backups", headers=MASTER)
        assert r.status_code == 401 and r.json()["error"]["provider"] == "admin_session"
        me = c.get("/v1/me", headers=MASTER).json()
        assert me["is_admin"] and me["admin_session"] == {
            "totp": True,
            "required": True,
            "active": False,
            "blocked": False,
        }
        assert c.get("/v1/metrics/summary?scope=all", headers=MASTER).status_code == 401  # toute l'instance
        assert c.get("/v1/metrics/summary", headers=MASTER).status_code == 200  # son propre compte
        assert c.post("/v1/admin/session", headers=MASTER, json={"code": "000000"}).status_code == 401
        code = current(env, secret)
        r = c.post("/v1/admin/session", headers=MASTER, json={"code": code})
        assert r.status_code == 200, r.text
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=43200" in cookie
        assert c.get("/v1/admin/backups", headers=MASTER).status_code == 200
        assert c.get("/v1/me", headers=MASTER).json()["admin_session"]["active"] is True
        # le cookie seul ne vaut rien : la clé maître reste demandée
        assert c.get("/v1/admin/backups").status_code in (401, 403)
    with make(env, OTHER) as c2:  # code intercepté et rejoué ailleurs : refusé
        assert c2.post("/v1/admin/session", headers=MASTER, json={"code": code}).status_code == 401
        r = c2.post("/v1/admin/session", headers=MASTER, json={"code": recovery[0].upper()})
        assert r.status_code == 200 and "Code de secours utilisé" in r.json()["warning"]
        c2.cookies.clear()
        assert c2.post("/v1/admin/session", headers=MASTER, json={"code": recovery[0]}).status_code == 401
        assert c2.get("/v1/admin/totp", headers=MASTER).status_code == 401


def test_secret_encrypted_and_recovery_hashed(env):
    with make(env, THIS_PC) as c:
        secret, recovery = enable(env, c)
    raw = json.dumps(env["store"].get_setting(totp.SETTING))
    assert secret not in raw and not any(code in raw or code.replace("-", "") in raw for code in recovery)


def test_session_header_expiry_and_revocation(env):
    with make(env, THIS_PC) as local:
        secret, _ = enable(env, local)
    with make(env) as c:
        assert c.post("/v1/admin/session", headers=MASTER, json={"code": current(env, secret)}).status_code == 200
        token = c.cookies.get(totp.COOKIE)
        c.cookies.clear()
        hdr = {**MASTER, totp.SESSION_HEADER: token}  # scripts : en-tête au lieu du cookie
        assert c.get("/v1/admin/backups", headers=hdr).status_code == 200
        sessions = c.get("/v1/admin/totp", headers=hdr).json()["sessions"]
        assert len(sessions) == 1 and sessions[0]["ip"] == ELSEWHERE[0] and sessions[0]["via"] == "totp"
        assert token not in json.dumps(sessions)  # seul le haché est gardé
        env["clock"].now += 12 * 3600 + 1
        assert c.get("/v1/admin/backups", headers=hdr).status_code == 401  # 12 h : expirée
        assert c.post("/v1/admin/session", headers=MASTER, json={"code": current(env, secret)}).status_code == 200
        assert c.delete("/v1/admin/sessions", headers=MASTER).json()["revoked"] == 1  # tout fermer
        assert c.get("/v1/admin/backups", headers=MASTER).status_code == 401


def test_new_master_key_closes_sessions(env):
    with make(env, THIS_PC) as local:
        secret, _ = enable(env, local)
    with make(env) as c:
        assert c.post("/v1/admin/session", headers=MASTER, json={"code": current(env, secret)}).status_code == 200
        token = c.cookies.get(totp.COOKIE)
    other = "gw-autre-maitre-0123456789abcdefghijk"
    with make(env, master=other) as c:
        c.cookies.set(totp.COOKIE, token)
        assert c.get("/v1/admin/backups", headers={"Authorization": f"Bearer {other}"}).status_code == 401


def test_bad_codes_count_against_brute_force(env, monkeypatch):
    monkeypatch.setenv("AUTH_MAX_FAILURES", "3")
    with make(env, THIS_PC) as local:
        secret, _ = enable(env, local)
        for _ in range(5):  # ce PC : jamais bloqué
            local.post("/v1/admin/session", headers=MASTER, json={"code": "123456"})
    with make(env) as c:
        for _ in range(3):
            assert c.post("/v1/admin/session", headers=MASTER, json={"code": "123456"}).status_code == 401
        r = c.post("/v1/admin/session", headers=MASTER, json={"code": current(env, secret)})
        assert r.status_code == 429  # bloquée, même avec le bon code
        assert c.get("/v1/me", headers=MASTER).status_code == 429


def test_global_cap_across_addresses(env, monkeypatch):
    monkeypatch.setenv("TOTP_MAX_FAILURES_PER_HOUR", "4")
    with make(env, THIS_PC) as local:
        secret, _ = enable(env, local)
    for i in range(4):
        with make(env, (f"198.51.100.{i + 10}", 1)) as c:
            assert c.post("/v1/admin/session", headers=MASTER, json={"code": "123456"}).status_code == 401
    with make(env, ("198.51.100.99", 1)) as c:
        assert c.post("/v1/admin/session", headers=MASTER, json={"code": current(env, secret)}).status_code == 429
    with make(env, THIS_PC) as local:  # sur le PC : toujours possible
        assert local.get("/v1/admin/backups", headers=MASTER).status_code == 200


def test_account_and_virtual_keys_unaffected(env):
    with make(env, THIS_PC) as local:
        enable(env, local)
    with make(env) as c:
        r = c.post("/v1/admin/session", headers={"Authorization": f"Bearer {ACCOUNT_KEY}"}, json={"code": "000000"})
        assert r.status_code == 403
        me = c.get("/v1/me", headers={"Authorization": f"Bearer {ACCOUNT_KEY}"}).json()
        assert "admin_session" not in me
        assert c.post("/v1/keys", headers=MASTER, json={"name": "telephone"}).status_code in (200, 201)


def test_remote_enable_opens_session_and_disable_needs_code(env):
    with make(env) as c:
        assert c.get("/v1/me", headers=MASTER).json()["admin_session"] == {
            "totp": False,
            "required": False,
            "active": False,
            "blocked": False,
        }
        setup = c.post("/v1/admin/totp/setup", headers=MASTER).json()
        assert c.post("/v1/admin/totp/enable", headers=MASTER, json={"code": "000000"}).status_code == 401
        r = c.post("/v1/admin/totp/enable", headers=MASTER, json={"code": current(env, setup["secret"])})
        assert r.status_code == 200 and r.json()["session"]["via"] == "activation"
        assert c.get("/v1/admin/backups", headers=MASTER).status_code == 200  # pas enfermé dehors
        assert c.post("/v1/admin/totp/setup", headers=MASTER).status_code == 409
        assert c.post("/v1/admin/totp/disable", headers=MASTER, json={}).status_code == 400
        env["clock"].now += totp.STEP
        r = c.post("/v1/admin/totp/recovery-codes", headers=MASTER, json={"code": current(env, setup["secret"])})
        assert r.status_code == 200 and len(r.json()["recovery_codes"]) == 10
        env["clock"].now += totp.STEP
        r = c.post("/v1/admin/totp/disable", headers=MASTER, json={"code": current(env, setup["secret"])})
        assert r.status_code == 200 and r.json()["enabled"] is False
        assert env["store"].list_admin_sessions(env["clock"].now) == []
        assert c.get("/v1/admin/backups", headers=MASTER).status_code == 200  # clé maître seule à nouveau


def test_internet_admin_blocked_until_totp(env, monkeypatch):
    """Adresse publique, double authentification pas encore activée : administration refusée (pas même
    l'activation, sinon une clé maître volée suffirait encore). Réseau local : permis. Activée : code exigé."""
    with make(env, INTERNET) as web:
        r = web.get("/v1/admin/backups", headers=MASTER)
        assert r.status_code == 403 and r.json()["error"]["provider"] == "totp"
        assert web.post("/v1/admin/totp/setup", headers=MASTER).status_code == 403
        me = web.get("/v1/me", headers=MASTER).json()
        assert me["is_admin"] and me["admin_session"]["blocked"] is True
        assert web.get("/v1/models", headers=MASTER).status_code == 200  # chat et API servis
        monkeypatch.setenv("ADMIN_REMOTE_WITHOUT_TOTP", "true")  # dérogation explicite
        assert web.get("/v1/admin/backups", headers=MASTER).status_code == 200
        monkeypatch.delenv("ADMIN_REMOTE_WITHOUT_TOTP")
    with make(env, LAN) as lan:
        assert lan.get("/v1/admin/backups", headers=MASTER).status_code == 200
    with make(env, THIS_PC) as local:
        enable(env, local)
    with make(env, INTERNET) as web:
        r = web.get("/v1/admin/backups", headers=MASTER)
        assert r.status_code == 401 and r.json()["error"]["provider"] == "admin_session"
        assert web.get("/v1/me", headers=MASTER).json()["admin_session"]["blocked"] is False


def test_local_disable_without_code(env):
    with make(env, THIS_PC) as local:
        enable(env, local)
        assert local.post("/v1/admin/totp/disable", headers=MASTER).status_code == 200
        assert local.get("/v1/admin/totp", headers=MASTER).json()["enabled"] is False


def test_setup_expires_and_clock_drift(env):
    t = AdminTotp(env["store"], env["cipher"], env["clock"])
    setup = t.begin_setup("admin@test")
    env["clock"].now += totp.SETUP_TTL + 1
    with pytest.raises(TotpError) as e:
        t.enable(current(env, setup["secret"]))
    assert e.value.status == 409
    setup = t.begin_setup("admin@test")
    t.enable(current(env, setup["secret"], -1))  # téléphone en retard de 30 s : accepté
    assert t.verify(current(env, setup["secret"], 1)) == "totp"  # en avance de 30 s aussi
    with pytest.raises(TotpError):  # code plus ancien que le dernier accepté : refusé
        t.verify(current(env, setup["secret"]))
    with pytest.raises(TotpError):
        t.verify(current(env, setup["secret"], 2))  # trop loin


def test_cli_reset(tmp_path, monkeypatch, capsys):
    db = tmp_path / "gateway.db"
    monkeypatch.setenv("GATEWAY_DB", str(db))
    store, cipher, clock = DocumentStore(str(db)), Cipher(secrets.token_bytes(32)), Clock()
    t = AdminTotp(store, cipher, clock)
    t.enable(code_at(t.begin_setup("x")["secret"], int(clock.now // totp.STEP)))
    t.open_session(MASTER_KEY, "203.0.113.5", None, "totp")
    store.close()
    totp.main(["reset"])
    assert "désactivée, 1 session(s) fermée(s)" in capsys.readouterr().out
    totp.main(["status"])
    assert "désactivée" in capsys.readouterr().out
