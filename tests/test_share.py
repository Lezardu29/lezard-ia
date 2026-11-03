"""Partage d'un accès en un clic (12.6)."""

from urllib.parse import unquote

import httpx
import qrcode
import qrcode.image.svg
from fastapi.testclient import TestClient

from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.remote import RemoteAccess, TunnelState
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}


def make(remote=None, **kw):
    return create_app(
        [],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
        remote=remote or RemoteAccess(),
        **kw,
    )


def test_access_urls_and_permissions(monkeypatch):
    monkeypatch.delenv("GATEWAY_BIND", raising=False)
    remote = RemoteAccess()
    with TestClient(make(remote)) as c:
        urls = c.get("/v1/access", headers=MASTER).json()["urls"]
        assert urls == [{"kind": "current", "url": "http://testserver", "label": "Adresse actuelle"}]
        with TestClient(make(), base_url="http://127.0.0.1:8000") as local:
            assert local.get("/v1/access", headers=MASTER).json()["urls"][0]["kind"] == "local"
        # tunnel rapide actif : l'adresse Internet passe en premier, signalée comme temporaire
        remote.status = TunnelState(
            provider="cloudflare", mode="public", state="running", public_url="https://abc.trycloudflare.com"
        )
        urls = c.get("/v1/access", headers=MASTER).json()["urls"]
        assert urls[0] == {
            "kind": "internet",
            "url": "https://abc.trycloudflare.com",
            "label": "Internet (de partout)",
            "stable": False,
        }
        remote.status = TunnelState(
            provider="tailscale", mode="private", state="running", public_url="https://pc.tailnet.ts.net"
        )
        assert c.get("/v1/access", headers=MASTER).json()["urls"][0]["kind"] == "tailscale"
        remote.status = TunnelState()  # (état simulé : rien à arrêter à la fermeture)
        # une clé virtuelle ne peut pas lister les accès ni fabriquer de QR
        k = c.post("/v1/keys", headers=MASTER, json={"name": "k"}).json()
        vk = {"Authorization": f"Bearer {k['key']}"}
        assert c.get("/v1/access", headers=vk).status_code == 403
        assert c.post("/v1/access/qr", headers=vk, json={"url": "http://x", "key": "k"}).status_code == 403


def test_lan_addresses_listed(monkeypatch):
    monkeypatch.setenv("GATEWAY_BIND", "192.168.1.50")
    with TestClient(make()) as c:
        urls = c.get("/v1/access", headers=MASTER).json()["urls"]
        assert {
            "kind": "lan",
            "url": "http://192.168.1.50:8000",
            "label": "Réseau local (même Wi-Fi)",
            "secure": False,
        } in urls


def test_qr_code_is_a_direct_login_link():
    with TestClient(make()) as c:
        k = c.post("/v1/keys", headers=MASTER, json={"name": "téléphone"}).json()
        r = c.post("/v1/access/qr", headers=MASTER, json={"url": "https://abc.trycloudflare.com/", "key": k["key"]})
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        link = r.json()["link"]
        assert link == f"https://abc.trycloudflare.com/#key={k['key']}"
        assert unquote(link.split("#key=")[1]) == k["key"]
        # le QR encode exactement ce lien (même générateur, mêmes réglages)
        ref = qrcode.make(link, image_factory=qrcode.image.svg.SvgPathFillImage, border=2).to_string(encoding="unicode")
        assert r.json()["svg"] == ref and r.json()["svg"].startswith("<svg")
        # la page servie à la racine (lien du QR) est bien la connexion, qui lit la clé dans le fragment
        assert "login.js" in c.get("/").text
        assert "fromLink" in c.get("/static/login.js").text
        for bad in (
            {"url": "javascript:alert(1)", "key": "k"},
            {"url": "https://x", "key": "a b"},
            {"url": "https://x", "key": ""},
            {"url": "https://x"},
            {"url": "https://x", "key": "k" * 300},
        ):
            assert c.post("/v1/access/qr", headers=MASTER, json=bad).status_code == 400
