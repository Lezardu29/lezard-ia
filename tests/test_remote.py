import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.remote import RemoteAccess, TunnelError
from gateway.store import DocumentStore

FAKE = str(Path(__file__).parent / "fake_tunnel.py")
MASTER_KEY = "gw-master-0123456789abcdefghij"  # >= 24 caractères : exposition permise
ACCOUNT_KEY = "ct-compte-0123456789abcdefghij"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}


def fake(kind):
    return [sys.executable, FAKE, kind]


def bins(**over):
    b = {"cloudflare": fake("cloudflared"), "ngrok": fake("ngrok"), "tailscale": fake("tailscale")}
    b.update(over)
    return b


# ---------- gestionnaire de tunnels ----------


def test_cloudflare_quick_tunnel_start_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_TUNNEL_LOG", str(tmp_path / "calls.log"))

    async def scenario():
        r = RemoteAccess(port=8123, binaries=bins())
        st = await r.start("cloudflare")
        assert st["state"] == "running" and st["public_url"] == "https://fluffy-test-tunnel.trycloudflare.com"
        assert st["mode"] == "public" and r.active
        proc = r._proc
        with pytest.raises(TunnelError) as e:
            await r.start("ngrok")
        assert e.value.status == 409  # un seul tunnel à la fois
        st = await r.stop()
        assert st["state"] == "stopped" and st["public_url"] is None and proc.returncode is not None

    asyncio.run(scenario())
    call = json.loads((tmp_path / "calls.log").read_text().splitlines()[0])
    assert call == ["cloudflared", "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8123"]


def test_ngrok_url_from_json_logs(monkeypatch):
    monkeypatch.setenv("NGROK_DOMAIN", "mon-domaine.ngrok.app")

    async def scenario():
        r = RemoteAccess(port=8000, binaries=bins())
        st = await r.start("ngrok")
        assert st["public_url"] == "https://abc.ngrok-free.app"
        await r.stop()

    asyncio.run(scenario())


def test_tailscale_serve_private_and_funnel(tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_TUNNEL_LOG", str(log))

    async def scenario():
        r = RemoteAccess(port=8000, binaries=bins())
        st = await r.start("tailscale")
        assert st["mode"] == "private" and st["public_url"] == "https://ma-machine.tail1234.ts.net"
        await r.stop()
        st = await r.start("tailscale", "funnel")
        assert st["mode"] == "public"
        await r.stop()
        with pytest.raises(TunnelError):
            await r.start("tailscale", "partout")

    asyncio.run(scenario())
    calls = [json.loads(l)[1:] for l in log.read_text().splitlines()]
    assert ["serve", "--bg", "8000"] in calls and ["funnel", "--bg", "8000"] in calls
    assert calls.count(["serve", "reset"]) == 2  # arrêt propre


def flaky(tmp_path, monkeypatch, plan, url=None):
    monkeypatch.setenv("FAKE_FLAKY_COUNTER", str(tmp_path / "runs"))
    monkeypatch.setenv("FAKE_FLAKY_PLAN", plan)
    if url:
        monkeypatch.setenv("FAKE_FLAKY_URL", url)
    r = RemoteAccess(port=8000, binaries=bins(cloudflare=fake("flaky")))
    r.retry_min, r.retry_max = 0.1, 0.4
    return r


async def wait_for(cond, seconds=10.0):
    for _ in range(int(seconds / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("délai dépassé")


def test_tunnel_restarts_after_crash_and_reports_new_url(tmp_path, monkeypatch):
    """12.4 : tunnel coupé -> relancé automatiquement (attente progressive), changement d'adresse signalé."""
    changes = []

    async def scenario():
        r = flaky(tmp_path, monkeypatch, "die,crash,crash,ok")
        r.on_url_change = lambda old, new: changes.append((old, new))
        st = await r.start("cloudflare")
        assert st["public_url"] == "https://run-0.trycloudflare.com" and st["stable_url"] is False
        await wait_for(lambda: r.status.state == "reconnecting")
        assert r.active  # la clé reste exigée pendant la coupure
        await wait_for(lambda: r.status.state == "running" and r.status.restarts == 1)
        assert r.status.public_url == "https://run-3.trycloudflare.com" and r.status.error is None
        assert any("Reconnexion impossible" in line for line in r.status.logs)  # 2 échecs avant succès
        await r.stop()

    asyncio.run(scenario())
    assert changes == [("https://run-0.trycloudflare.com", "https://run-3.trycloudflare.com")]


def test_stable_url_not_reported_and_stop_cancels_reconnect(tmp_path, monkeypatch):
    changes = []

    async def scenario():
        r = flaky(tmp_path, monkeypatch, "die,ok", url="https://toujours-la-meme.trycloudflare.com")
        r.on_url_change = lambda old, new: changes.append(new)
        await r.start("cloudflare")
        await wait_for(lambda: r.status.restarts == 1 and r.status.state == "running")
        assert r.status.public_url == "https://toujours-la-meme.trycloudflare.com"
        await r.stop()
        # arrêt volontaire : aucune reconnexion
        (tmp_path / "b").mkdir()
        r2 = flaky(tmp_path / "b", monkeypatch, "die,ok")
        r2.retry_min = 5  # reconnexion lente : stop() l'annule
        await r2.start("cloudflare")
        await wait_for(lambda: r2.status.state == "reconnecting")
        st = await r2.stop()
        assert st["state"] == "stopped" and not r2.active
        await asyncio.sleep(0.2)
        assert r2.status.state == "stopped" and r2.status.restarts == 0

    asyncio.run(scenario())
    assert changes == []  # même adresse : rien à signaler


def test_stable_url_detection(monkeypatch):
    from gateway.remote import stable_url

    for k in ("CLOUDFLARE_TUNNEL_TOKEN", "CLOUDFLARE_TUNNEL_HOSTNAME", "NGROK_DOMAIN"):
        monkeypatch.delenv(k, raising=False)
    assert not stable_url("cloudflare") and not stable_url("ngrok") and stable_url("tailscale")
    monkeypatch.setenv("NGROK_DOMAIN", "mon.ngrok.app")
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_HOSTNAME", "gw.example.com")
    assert stable_url("ngrok") and stable_url("cloudflare")


def test_url_change_notifies_admin(tmp_path, monkeypatch):
    remote = flaky(tmp_path, monkeypatch, "die,ok")
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY, require_key=True),
        store=DocumentStore(":memory:"),
        remote=remote,
    )
    with TestClient(app, client=("127.0.0.1", 1)) as c:
        c.post("/v1/admin/remote/start", headers=MASTER, json={"provider": "cloudflare"})
        import time

        for _ in range(200):
            if remote.status.restarts:
                break
            time.sleep(0.05)
        notes = c.get("/v1/notifications", headers=MASTER).json()["data"]
        assert notes[0]["title"] == "Adresse d'accès distant modifiée"
        assert "https://run-1.trycloudflare.com" in notes[0]["message"]


def test_failures(monkeypatch):
    monkeypatch.setenv("REMOTE_URL_TIMEOUT", "1.5")

    async def scenario():
        r = RemoteAccess(binaries=bins(cloudflare=fake("crash"), ngrok=fake("silent")))
        with pytest.raises(TunnelError) as e:
            await r.start("cloudflare")
        assert e.value.status == 502 and "edge" in e.value.message and r.status.state == "error"
        with pytest.raises(TunnelError) as e:
            await r.start("ngrok")
        assert e.value.status == 504 and not r.active  # processus tué après le délai
        with pytest.raises(TunnelError) as e:
            await RemoteAccess(binaries={}).start("inconnu")
        assert e.value.status == 400

    asyncio.run(scenario())
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("CLOUDFLARED_BIN", raising=False)
    r = RemoteAccess()
    assert r.available()["cloudflare"] is False
    with pytest.raises(TunnelError) as e:
        asyncio.run(r.start("cloudflare"))
    assert e.value.status == 424


# ---------- API & sécurité ----------


def make(require_key, keys=None, master=MASTER_KEY, peer="127.0.0.1"):
    remote = RemoteAccess(port=8000, binaries=bins())
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))),
        plans=PlanResolver(
            {ACCOUNT_KEY: "pro"} if keys is None else keys, "free", master_key=master, require_key=require_key
        ),
        store=DocumentStore(":memory:"),
        remote=remote,
    )
    return TestClient(app, client=(peer, 50000)), remote


def probe(c, headers) -> int:
    """Identifiant invalide présenté depuis l'adresse vue par la gateway : 401 la 1re fois, puis 429 une fois cette
    adresse bloquée (AUTH_MAX_FAILURES=1). Montre quelle adresse la gateway attribue au visiteur."""
    return c.get("/v1/plan", headers={"Authorization": "Bearer jeton-invalide-de-test", **headers}).status_code


def test_admin_only_and_key_required_while_exposed():
    c, remote = make(require_key=False)
    with c:
        assert c.get("/v1/admin/remote", headers={"Authorization": f"Bearer {ACCOUNT_KEY}"}).status_code == 403
        st = c.get("/v1/admin/remote", headers=MASTER).json()
        assert st["state"] == "stopped" and st["available"] == {"cloudflare": True, "ngrok": True, "tailscale": True}
        assert st["require_key"] is False and c.get("/v1/me", headers=MASTER).json()["is_admin"] is True
        assert c.get("/v1/plan").status_code == 200  # local, sans tunnel : sans clé
        # tunnel public ouvert sans GATEWAY_REQUIRE_KEY : la clé devient obligatoire automatiquement (12.2)
        r = c.post("/v1/admin/remote/start", headers=MASTER, json={"provider": "cloudflare"})
        assert r.status_code == 200 and r.json()["state"] == "running"
        assert c.get("/v1/admin/remote", headers=MASTER).json()["anonymous"] is False
        r = c.get("/v1/plan")
        assert r.status_code == 401 and "accès distant" in r.json()["error"]["message"]
        assert c.get("/v1/plan", headers={"Authorization": "Bearer jeton-au-hasard"}).status_code == 401
        assert c.get("/v1/plan", headers={"Authorization": f"Bearer {ACCOUNT_KEY}"}).status_code == 200
        c.post("/v1/admin/remote/stop", headers=MASTER)
        assert c.get("/v1/plan").status_code == 200  # tunnel arrêté : retour à la normale
        # tailnet privé : clé requise aussi
        assert (
            c.post("/v1/admin/remote/start", headers=MASTER, json={"provider": "tailscale"}).json()["mode"] == "private"
        )
        assert c.get("/v1/plan").status_code == 401
        c.post("/v1/admin/remote/stop", headers=MASTER)
        # dérogation explicite : accès sans clé à travers le tunnel
        r = c.post("/v1/admin/remote/start", headers=MASTER, json={"provider": "cloudflare", "allow_anonymous": True})
        assert r.status_code == 200 and c.get("/v1/admin/remote", headers=MASTER).json()["anonymous"] is True
        assert c.get("/v1/plan").status_code == 200
        proc = remote._proc
    assert remote.status.state == "stopped" and proc.returncode is not None  # arrêté avec la gateway


def test_anonymous_only_from_this_machine(monkeypatch):
    monkeypatch.delenv("GATEWAY_ANONYMOUS_FROM", raising=False)  # défaut : 127.0.0.1 / ::1
    c, _ = make(require_key=False)
    with c:
        assert c.get("/v1/plan").status_code == 200
    c, _ = make(require_key=False, peer="192.168.1.50")  # autre appareil du réseau
    with c:
        assert c.get("/v1/plan").status_code == 401
        assert c.get("/v1/plan", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401  # en-tête ignoré
        assert c.get("/v1/plan", headers={"Authorization": f"Bearer {ACCOUNT_KEY}"}).status_code == 200
    monkeypatch.setenv("GATEWAY_ANONYMOUS_FROM", "192.168.1.0/24")  # réseau déclaré de confiance
    c, _ = make(require_key=False, peer="192.168.1.50")
    with c:
        assert c.get("/v1/plan").status_code == 200


def test_weak_tokens_block_public_exposure():
    # compte de test au jeton devinable (ancien défaut de lancer.ps1)
    c, remote = make(require_key=True, keys={"compte-test": "free"})
    with c:
        r = c.post("/v1/admin/remote/start", headers=MASTER, json={"provider": "cloudflare"})
        msg = r.json()["error"]["message"]
        assert r.status_code == 409 and "GATEWAY_KEYS (compte free)" in msg and "compte-test" not in msg
        # allow_anonymous ne lève pas ce refus ; le tailnet privé reste permis
        assert (
            c.post(
                "/v1/admin/remote/start", headers=MASTER, json={"provider": "ngrok", "allow_anonymous": True}
            ).status_code
            == 409
        )
        assert (
            c.post("/v1/admin/remote/start", headers=MASTER, json={"provider": "tailscale"}).json()["mode"] == "private"
        )
        c.post("/v1/admin/remote/stop", headers=MASTER)
    # clé maître trop courte
    c, remote = make(require_key=True, master="master")
    with c:
        r = c.post(
            "/v1/admin/remote/start", headers={"Authorization": "Bearer master"}, json={"provider": "cloudflare"}
        )
        assert r.status_code == 409 and "GATEWAY_MASTER_KEY" in r.json()["error"]["message"]
        assert remote.status.state == "stopped"


def test_weak_tokens_listed():
    r = PlanResolver({"compte-test": "free", "x" * 24: "pro"}, "free", master_key="court", require_key=True)
    assert r.weak_tokens() == ["GATEWAY_KEYS (compte free)", "GATEWAY_MASTER_KEY"]
    assert PlanResolver({}, "free", master_key=MASTER_KEY).weak_tokens() == []


def test_admin_through_public_tunnel_needs_totp():
    """Sans double authentification, la clé maître n'administre pas depuis Internet : une fuite de la clé ne
    donne pas tout. Le navigateur de ce PC et un tailnet privé restent permis ; le reste de l'API est servi."""
    c, remote = make(require_key=True)
    with c:
        c.post("/v1/admin/remote/start", headers=MASTER, json={"provider": "cloudflare"})
        visitor = {**MASTER, "CF-Connecting-IP": "8.8.8.8"}
        r = c.get("/v1/admin/remote", headers=visitor)
        assert r.status_code == 403 and "double authentification" in r.json()["error"]["message"]
        assert c.post("/v1/admin/totp/setup", headers=visitor).status_code == 403  # ni l'activer d'Internet
        assert c.get("/v1/me", headers=visitor).json()["admin_session"]["blocked"] is True
        assert c.get("/v1/models", headers=visitor).status_code == 200
        assert c.get("/v1/admin/remote", headers=MASTER).status_code == 200  # navigateur de ce PC
        assert c.get("/v1/me", headers=MASTER).json()["admin_session"]["blocked"] is False
        c.post("/v1/admin/remote/stop", headers=MASTER)
        c.post("/v1/admin/remote/start", headers=MASTER, json={"provider": "tailscale"})  # tailnet privé
        assert c.get("/v1/admin/remote", headers={**MASTER, "X-Forwarded-For": "100.101.102.103"}).status_code == 200
        c.post("/v1/admin/remote/stop", headers=MASTER)


def test_real_client_ip_behind_tunnel(monkeypatch):
    monkeypatch.setenv("AUTH_MAX_FAILURES", "1")  # 1 identifiant invalide : adresse bloquée (sonde par IP)
    c, remote = make(require_key=True)
    with c:
        c.post("/v1/admin/remote/start", headers=MASTER, json={"provider": "cloudflare"})
        # deux visiteurs distincts relayés par le démon local : blocages par IP séparés
        assert probe(c, {"CF-Connecting-IP": "198.51.100.1"}) == 401
        assert probe(c, {"CF-Connecting-IP": "198.51.100.2"}) == 401
        assert probe(c, {"CF-Connecting-IP": "198.51.100.1"}) == 429
        c.post("/v1/admin/remote/stop", headers=MASTER)
        # tunnel arrêté : l'en-tête n'est plus cru (sinon n'importe qui pourrait usurper une IP)
        assert probe(c, {"CF-Connecting-IP": "198.51.100.9"}) == 401
        assert probe(c, {"CF-Connecting-IP": "198.51.100.10"}) == 429


def test_tunnel_not_started_by_the_gateway_is_not_this_pc(monkeypatch):
    """cloudflared installé en service Windows, tunnel resté ouvert après un arrêt brutal… : il relaie depuis
    127.0.0.1 alors que la gateway croit n'avoir aucun tunnel. Ses visiteurs ne passent pas pour ce PC."""
    monkeypatch.delenv("GATEWAY_ANONYMOUS_FROM", raising=False)
    monkeypatch.delenv("GATEWAY_TRUST_PROXY", raising=False)
    c, remote = make(require_key=False)
    visitor = {"CF-Connecting-IP": "8.8.8.8", "CF-Ray": "8a1b2c3d4e5f-CDG"}
    with c:
        assert not remote.active
        assert c.get("/v1/plan").status_code == 200  # navigateur de ce PC : sans clé
        assert c.get("/v1/plan", headers=visitor).status_code == 401  # visiteur du tunnel : clé requise
        assert c.get("/v1/plan", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 401
        # ni administration sans double authentification, ni actions réservées à ce PC
        assert c.get("/v1/admin/remote", headers=MASTER).status_code == 200
        r = c.get("/v1/admin/remote", headers={**MASTER, **visitor})
        assert r.status_code == 403 and "double authentification" in r.json()["error"]["message"]
        r = c.post("/v1/admin/encryption-key/export", headers={**MASTER, **visitor}, json={"passphrase": "x" * 20})
        assert r.status_code == 403
        assert c.get("/docs").status_code == 200 and c.get("/docs", headers=visitor).status_code == 404
        # en-tête forgé pour se faire passer pour ce PC : refusé aussi
        spoof = {"CF-Connecting-IP": "127.0.0.1", "CF-Ray": "x"}
        assert c.get("/v1/plan", headers=spoof).status_code == 401
        # l'IP transmise compte pour la force brute (pas celle de ce PC, jamais bloquée)
        for _ in range(25):
            c.get("/v1/plan", headers={**visitor, "Authorization": "Bearer jeton-au-hasard"})
        assert c.get("/v1/plan", headers={**visitor, "Authorization": f"Bearer {ACCOUNT_KEY}"}).status_code == 429
        assert c.get("/v1/plan", headers={"Authorization": f"Bearer {ACCOUNT_KEY}"}).status_code == 200


def test_lan_mode_requires_key_from_other_devices(monkeypatch):
    """12.1 : en mode réseau local, les autres appareils présentent une clé ; le PC lui-même non."""
    monkeypatch.setenv("GATEWAY_BIND", "0.0.0.0")
    monkeypatch.setenv("GATEWAY_ANONYMOUS_FROM", "172.16.0.0/12")  # ex. pont Docker : ignoré en mode réseau local
    local, _ = make(require_key=False)
    with local:
        phone = TestClient(local.app, client=("192.168.1.50", 1))
        docker_seen = TestClient(local.app, client=("172.17.0.1", 1))  # appareil vu via Docker Desktop
        assert local.get("/v1/plan").status_code == 200
        assert phone.get("/v1/plan").status_code == 401
        assert docker_seen.get("/v1/plan").status_code == 401
        assert phone.get("/v1/plan", headers={"Authorization": f"Bearer {ACCOUNT_KEY}"}).status_code == 200
        st = local.get("/v1/admin/remote", headers=MASTER).json()["lan"]
        assert st["enabled"] is True and all(u.startswith("http://") and u.endswith(":8000") for u in st["urls"])
    # dérogation explicite : réseau de confiance accepté sans clé
    monkeypatch.setenv("GATEWAY_LAN_ANONYMOUS", "true")
    monkeypatch.setenv("GATEWAY_ANONYMOUS_FROM", "192.168.1.0/24")
    local, _ = make(require_key=False)
    with local:
        assert TestClient(local.app, client=("192.168.1.50", 1)).get("/v1/plan").status_code == 200


def test_lan_mode_refused_with_weak_tokens(monkeypatch):
    monkeypatch.setenv("GATEWAY_BIND", "0.0.0.0")
    c, _ = make(require_key=True, keys={"compte-test": "free"})
    with pytest.raises(RuntimeError) as e:
        with c:
            pass
    assert "Mode réseau local refusé" in str(e.value) and "GATEWAY_KEYS (compte free)" in str(e.value)


def test_lan_addresses(monkeypatch):
    from gateway.remote import lan_addresses

    assert lan_addresses("192.168.1.20") == ["192.168.1.20"]  # écoute sur une seule interface
    assert all(not ip.startswith("127.") for ip in lan_addresses("0.0.0.0"))
    monkeypatch.setenv("GATEWAY_BIND", "192.168.1.20")
    c, _ = make(require_key=True)
    with c:
        assert c.get("/v1/admin/remote", headers=MASTER).json()["lan"] == {
            "enabled": True,
            "urls": ["http://192.168.1.20:8000"],
            "tls": {"enabled": False, "port": None, "fingerprint": None, "ca_url": None},
        }
    monkeypatch.delenv("GATEWAY_BIND")
    c, _ = make(require_key=True)
    with c:
        assert c.get("/v1/admin/remote", headers=MASTER).json()["lan"] == {
            "enabled": False,
            "urls": [],
            "tls": {"enabled": False, "port": None, "fingerprint": None, "ca_url": None},
        }


def test_spoofed_ip_headers_ignored(monkeypatch):
    """8.2 : un visiteur ne peut pas choisir son IP (blocage anti force brute par IP, ip_allow des clés)."""
    monkeypatch.setenv("AUTH_MAX_FAILURES", "1")  # 1 identifiant invalide : adresse bloquée (sonde par IP)
    c, remote = make(require_key=True)
    with c:
        # ngrok : CF-Connecting-IP fourni par le client -> ignoré ; X-Forwarded-For lu par la DROITE
        c.post("/v1/admin/remote/start", headers=MASTER, json={"provider": "ngrok"})
        visitor = {"X-Forwarded-For": "198.51.100.7"}
        assert probe(c, visitor) == 401
        for forged in (
            {"X-Forwarded-For": "1.2.3.4, 198.51.100.7"},  # préfixe inventé
            {"X-Forwarded-For": "198.51.100.7", "CF-Connecting-IP": "5.6.7.8"},
        ):
            assert probe(c, forged) == 429  # même visiteur
        assert probe(c, {"X-Forwarded-For": "198.51.100.8"}) == 401
        c.post("/v1/admin/remote/stop", headers=MASTER)
        # Cloudflare : seul CF-Connecting-IP compte, X-Forwarded-For ne permet pas de changer d'identité
        c.post("/v1/admin/remote/start", headers=MASTER, json={"provider": "cloudflare"})
        h = {"CF-Connecting-IP": "203.0.113.1"}
        assert probe(c, h) == 401
        assert probe(c, {**h, "X-Forwarded-For": "9.9.9.9"}) == 429
        # valeur qui n'est pas une IP : on retombe sur l'adresse du pair
        assert probe(c, {"CF-Connecting-IP": "pas-une-ip"}) == 401
        assert probe(c, {"CF-Connecting-IP": "autre-texte"}) == 429


def test_trusted_proxy_hops(monkeypatch):
    monkeypatch.setenv("AUTH_MAX_FAILURES", "1")  # 1 identifiant invalide : adresse bloquée (sonde par IP)
    monkeypatch.setenv("GATEWAY_TRUST_PROXY", "true")
    c, _ = make(require_key=True)
    with c:
        # 1 proxy de confiance : la dernière adresse est celle qu'il a vue
        assert probe(c, {"X-Forwarded-For": "6.6.6.6, 198.51.100.20"}) == 401
        assert probe(c, {"X-Forwarded-For": "7.7.7.7, 198.51.100.20"}) == 429
    monkeypatch.setenv("GATEWAY_TRUSTED_HOPS", "2")  # CDN + reverse proxy
    c, _ = make(require_key=True)
    with c:
        h = {"X-Forwarded-For": "6.6.6.6, 198.51.100.30, 10.0.0.2"}
        assert probe(c, h) == 401
        h2 = {"X-Forwarded-For": "8.8.8.8, 198.51.100.30, 10.0.0.3"}
        assert probe(c, h2) == 429


def test_brute_force_blocks_ip(monkeypatch):
    """12.3 : trop d'identifiants invalides -> IP bloquée, même avec une bonne clé ; l'admin peut débloquer."""
    monkeypatch.setenv("AUTH_MAX_FAILURES", "3")
    local, _ = make(require_key=True)  # le propriétaire, sur sa machine
    with local:
        attacker = TestClient(local.app, client=("203.0.113.50", 1))
        other = TestClient(local.app, client=("203.0.113.51", 1))
        for token in ("devine-1", "sk-app-devine-2", "devine-3"):
            assert attacker.get("/v1/plan", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        r = attacker.get("/v1/plan", headers={"Authorization": f"Bearer {ACCOUNT_KEY}"})
        assert r.status_code == 429 and r.json()["error"]["provider"] == "limits:auth"
        assert 800 <= int(r.headers["Retry-After"]) <= 900
        # les autres visiteurs ne sont pas touchés ; une requête SANS jeton n'est pas un échec compté
        for _ in range(5):
            assert other.get("/v1/plan").status_code == 401
        assert other.get("/v1/plan", headers={"Authorization": f"Bearer {ACCOUNT_KEY}"}).status_code == 200
        # visible et débloquable par l'admin
        st = local.get("/v1/admin/remote", headers=MASTER).json()["auth"]
        assert [b["ip"] for b in st["blocked"]] == ["203.0.113.50"] and st["failures_total"] == 3
        assert local.delete("/v1/admin/security/blocks/203.0.113.50", headers=MASTER).status_code == 200
        assert local.delete("/v1/admin/security/blocks/203.0.113.50", headers=MASTER).status_code == 404
        assert attacker.get("/v1/plan", headers={"Authorization": f"Bearer {ACCOUNT_KEY}"}).status_code == 200
        # le propriétaire en local (sans tunnel) n'est jamais bloqué
        for _ in range(5):
            assert local.get("/v1/plan", headers={"Authorization": "Bearer faute-de-frappe"}).status_code == 401
        assert local.get("/v1/plan", headers=MASTER).status_code == 200


def test_brute_force_on_bridge_tokens(monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("AUTH_MAX_FAILURES", "2")
    c, _ = make(require_key=True)
    with c:
        attacker = TestClient(c.app, client=("203.0.113.60", 1))
        codes = []
        for _ in range(3):
            with attacker.websocket_connect("/v1/bridge/connect", headers={"Authorization": "Bearer gwb_faux"}) as ws:
                with pytest.raises(WebSocketDisconnect) as e:
                    ws.receive_text()
                codes.append(e.value.code)
        assert codes == [4401, 4401, 4429]  # 3e tentative : IP bloquée (non fatal)


def test_auth_throttle_expiry():
    from gateway.limits import AuthThrottle, RateLimited

    now = [1000.0]
    t = AuthThrottle(clock=lambda: now[0])
    t.max_failures, t.window, t.block_seconds = 2, 60, 300
    t.fail("1.2.3.4")
    now[0] += 61  # hors fenêtre : le compteur repart
    t.fail("1.2.3.4")
    t.check("1.2.3.4")
    t.fail("1.2.3.4")
    with pytest.raises(RateLimited):
        t.check("1.2.3.4")
    now[0] += 301  # fin du blocage
    t.check("1.2.3.4")
    assert t.status()["blocked"] == []


def test_auto_connect_on_startup(monkeypatch):
    monkeypatch.setenv("REMOTE_ACCESS", "ngrok")
    c, remote = make(require_key=True)
    with c:
        for _ in range(100):
            if remote.status.state == "running":
                break
            import time

            time.sleep(0.05)
        assert c.get("/v1/admin/remote", headers=MASTER).json()["public_url"] == "https://abc.ngrok-free.app"
    assert remote.status.state == "stopped"
    # démarrage automatique sans GATEWAY_REQUIRE_KEY : le tunnel démarre, mais la clé est exigée
    monkeypatch.setenv("REMOTE_ACCESS", "cloudflare")
    c, remote = make(require_key=False)
    with c:
        import time

        for _ in range(100):
            if remote.status.state == "running":
                break
            time.sleep(0.05)
        assert remote.status.state == "running" and c.get("/v1/plan").status_code == 401
    # jeton devinable : pas de démarrage
    c, remote = make(require_key=True, keys={"compte-test": "free"})
    with c:
        time.sleep(0.3)
        assert remote.status.state == "stopped"


def test_remote_panel_asset():
    c, _ = make(require_key=True)
    with c:
        assert c.get("/static/remote-panel.js").status_code == 200
        assert 'id="remote-btn"' in c.get("/app?frame=1").text


def test_tunnel_served_in_plain_http_is_redirected_to_https():
    """Cloudflare sans « Always Use HTTPS » : la page de connexion et la clé passeraient en clair jusqu'au tunnel."""
    c, remote = make(require_key=True)
    with c:
        c.post("/v1/admin/remote/start", headers=MASTER, json={"provider": "cloudflare"})
        http = {"CF-Connecting-IP": "8.8.8.8", "CF-Visitor": '{"scheme":"http"}', "Host": "ia.exemple.fr"}
        r = c.get("/app?x=1", headers=http, follow_redirects=False)
        assert r.status_code == 308 and r.headers["location"] == "https://ia.exemple.fr/app?x=1"
        r = c.get("/v1/models", headers={**http, "Authorization": f"Bearer {ACCOUNT_KEY}"})
        assert r.status_code == 403 and "HTTPS" in r.json()["error"]["message"]
        assert c.post("/v1/chat/completions", headers=http, json={}).status_code == 403
        https = {**http, "CF-Visitor": '{"scheme":"https"}'}
        r = c.get("/v1/models", headers={**https, "Authorization": f"Bearer {ACCOUNT_KEY}"})
        assert r.status_code == 200 and r.headers["strict-transport-security"].startswith("max-age=")
        local = c.get("/health")  # navigateur de ce PC, en http://127.0.0.1 : rien ne change
        assert local.status_code == 200 and "strict-transport-security" not in local.headers
        c.post("/v1/admin/remote/stop", headers=MASTER)
        c.post("/v1/admin/remote/start", headers=MASTER, json={"provider": "ngrok"})
        r = c.get("/", headers={"X-Forwarded-For": "8.8.8.8", "X-Forwarded-Proto": "http"}, follow_redirects=False)
        assert r.status_code == 308 and r.headers["location"].startswith("https://")
        # en production, uvicorn (proxy_headers) a déjà remplacé 127.0.0.1 par l'IP du visiteur
        visitor = TestClient(c.app, client=("8.8.8.8", 1))
        r = visitor.get("/app", headers={"X-Forwarded-Proto": "http"}, follow_redirects=False)
        assert r.status_code == 308
        c.post("/v1/admin/remote/stop", headers=MASTER)
