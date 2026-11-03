"""Accès à distance automatique : « Créer mon compte web » sans accès à distance demande un tunnel à lezard-ia.fr,
télécharge cloudflared (vérifié) et le démarre ; le tunnel est gardé (jeton chiffré) et réutilisé."""

import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import auto_tunnel, cloudflared
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.remote import TunnelState
from gateway.store import DocumentStore
from gateway.totp import code_at

from .fake_remote import StubRemote
from .test_permissions import Upstream


class FakeRemote(StubRemote):
    def __init__(self):
        super().__init__(port=8000)
        self.started = []

    @property
    def active(self):
        return self.status.state == "running"

    def available(self):
        return {"cloudflare": True, "ngrok": False, "tailscale": False}

    async def start(self, provider, mode=None, managed=None):
        self.started.append(managed)
        self.status = TunnelState(provider=provider, mode="public", state="running",
                                  public_url=f"https://{managed['hostname']}")  # fmt: skip
        return self.status.to_dict()

    async def stop(self):
        self.status = TunnelState()
        return self.status.to_dict()


@pytest.fixture
def env(monkeypatch):
    created = []

    async def fake_create(client, instance, name, port):
        created.append((instance, name, port))
        return {"hostname": "mon-pc-7f3a1c.lezard-ia.fr", "token": "JETON-TUNNEL-SECRET", "proof": "p" * 64}

    async def fake_ensure(client):
        return "cloudflared.exe"

    monkeypatch.setattr(auto_tunnel, "create", fake_create)
    monkeypatch.setattr(cloudflared, "ensure", fake_ensure)
    monkeypatch.delenv("REMOTE_ACCESS", raising=False)
    remote = FakeRemote()
    store = DocumentStore(":memory:")
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(Upstream())),
        plans=PlanResolver({}, "free", master_key="master-0123456789abcdefghij"),
        store=store,
        remote=remote,
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c, remote, store, created


ADMIN = {"Authorization": "Bearer master-0123456789abcdefghij"}


def enable_totp(c):
    import time

    secret = c.post("/v1/admin/totp/setup", headers=ADMIN).json()["secret"]
    assert (
        c.post(
            "/v1/admin/totp/enable", headers=ADMIN, json={"code": code_at(secret, int(time.time() // 30))}
        ).status_code
        == 200
    )


def test_web_account_creates_the_remote_access_by_itself(env):
    c, remote, store, created = env
    assert c.post("/v1/admin/web-account", headers=ADMIN).json()["error"]["provider"] == "totp_setup"
    assert not created  # rien de créé tant que la 2FA n'est pas active
    enable_totp(c)
    r = c.post("/v1/admin/web-account", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://mon-pc-7f3a1c.lezard-ia.fr" and r.json()["stable"] is True
    assert len(created) == 1 and created[0][2] == 8000
    assert remote.started[-1] == {"hostname": "mon-pc-7f3a1c.lezard-ia.fr", "token": "JETON-TUNNEL-SECRET"}
    saved = store.get_setting("managed_tunnel")
    assert saved["auto"] is True and saved["hostname"] == "mon-pc-7f3a1c.lezard-ia.fr"
    assert "JETON-TUNNEL-SECRET" not in str(saved) and "p" * 64 not in str(saved)  # jeton et preuve chiffrés


def test_saved_tunnel_is_reused_not_recreated(env):
    c, remote, store, created = env
    enable_totp(c)
    assert c.post("/v1/admin/web-account", headers=ADMIN).status_code == 200
    import asyncio

    asyncio.run(remote.stop())
    assert c.post("/v1/admin/web-account", headers=ADMIN).status_code == 200
    assert len(created) == 1 and len(remote.started) == 2  # même tunnel, relancé


def test_errors_are_explained(env, monkeypatch):
    c, remote, store, created = env
    enable_totp(c)

    async def full(client, instance, name, port):
        raise auto_tunnel.AutoTunnelError(503, "Plus de place pour de nouveaux accès à distance pour le moment.")

    monkeypatch.setattr(auto_tunnel, "create", full)
    r = c.post("/v1/admin/web-account", headers=ADMIN)
    assert r.status_code == 503 and "Plus de place" in r.json()["error"]["message"]
    assert not remote.started
    monkeypatch.setenv("REMOTE_ACCESS", "cloudflare")  # tunnel du propriétaire : jamais remplacé
    r = c.post("/v1/admin/web-account", headers=ADMIN)
    assert r.status_code == 409 and "REMOTE_ACCESS" in r.json()["error"]["message"]


@pytest.mark.parametrize("good", [True, False])
def test_cloudflared_download_is_verified(tmp_path, monkeypatch, good):
    payload = b"MZ faux cloudflared"
    monkeypatch.setenv("GATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CLOUDFLARED_BIN", raising=False)
    monkeypatch.setattr(cloudflared.shutil, "which", lambda name: None)
    monkeypatch.setattr(cloudflared.sys, "platform", "win32")
    monkeypatch.setattr(cloudflared, "SIZE", len(payload))
    monkeypatch.setattr(cloudflared, "SHA256", hashlib.sha256(payload if good else b"autre").hexdigest())
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=payload)))
    import asyncio

    if good:
        path = asyncio.run(cloudflared.ensure(client))
        assert path == str(tmp_path / "bin" / "cloudflared.exe") and cloudflared.find() == path
    else:
        with pytest.raises(cloudflared.CloudflaredError, match="ne correspond pas"):
            asyncio.run(cloudflared.ensure(client))
        assert not list((tmp_path / "bin").iterdir())  # rien gardé


def test_tunnel_launcher_finds_the_downloaded_cloudflared(tmp_path, monkeypatch):
    from gateway.tunnel import RemoteAccess

    monkeypatch.setenv("GATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CLOUDFLARED_BIN", raising=False)
    monkeypatch.setattr(cloudflared.shutil, "which", lambda name: None)
    assert RemoteAccess().command("cloudflare") is None
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "cloudflared.exe").write_bytes(b"x")
    assert RemoteAccess().command("cloudflare") == [str(tmp_path / "bin" / "cloudflared.exe")]


def test_orphan_cloudflared_is_stopped_only_if_it_is_cloudflared(tmp_path, monkeypatch):
    """Un cloudflared survit à une gateway arrêtée brutalement : arrêté au démarrage suivant, mais jamais un autre
    programme qui aurait repris son numéro."""
    from gateway import tunnel

    monkeypatch.setenv("GATEWAY_DATA_DIR", str(tmp_path))
    killed = []
    monkeypatch.setattr(tunnel.os, "kill", lambda pid, sig: killed.append(pid))
    (tmp_path / "cloudflared.pid").write_text("4242", encoding="ascii")
    monkeypatch.setattr(tunnel, "_is_cloudflared", lambda pid: False)
    tunnel._stop_orphan()
    assert killed == [] and not (tmp_path / "cloudflared.pid").exists()
    (tmp_path / "cloudflared.pid").write_text("4243", encoding="ascii")
    monkeypatch.setattr(tunnel, "_is_cloudflared", lambda pid: True)
    tunnel._stop_orphan()
    assert killed == [4243]
