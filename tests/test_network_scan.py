"""Modèles des autres machines du réseau (12.5)."""

import asyncio
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

import gateway.network_scan as ns
from gateway.main import create_app
from gateway.network_scan import ScanError, default_targets, expand, suggest_name
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
OPEN = {("192.168.1.20", 11434), ("192.168.1.30", 8000), ("192.168.1.40", 8080)}


def network(request: httpx.Request) -> httpx.Response:
    """Machines simulées : Ollama sur .20, vLLM protégé par clé sur .30, simple site web sur .40."""
    host, port = request.url.host, request.url.port
    if (host, port) == ("192.168.1.20", 11434):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}, {"id": "qwen2.5:7b"}]})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "depuis le PC 2"}}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            },
        )
    if (host, port) == ("192.168.1.30", 8000):
        return httpx.Response(401, json={"error": "unauthorized"})
    if (host, port) == ("192.168.1.40", 8080):
        return httpx.Response(200, text="<html>imprimante</html>")
    return httpx.Response(404)


@pytest.fixture
def fake_tcp(monkeypatch):
    async def opener(host, port, sem):
        return (host, port) in OPEN

    monkeypatch.setattr(ns, "_open", opener)
    for where in ("gateway.api.admin", "gateway.core"):  # recherche réseau et adresses affichées
        monkeypatch.setattr(f"{where}.lan_addresses", lambda bind="0.0.0.0": ["192.168.1.197"])


def test_expand_targets(monkeypatch):
    own = ["192.168.1.197"]
    assert len(expand(["192.168.1.0/24"], own)) == 253  # 254 hôtes moins cette machine
    assert expand(["192.168.1.20", "192.168.1.20", "10.0.0.5"], own) == ["192.168.1.20", "10.0.0.5"]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("192.168.1.42", 0))])
    assert expand(["pc-gpu.local"], own) == ["192.168.1.42"]
    # Internet, cette machine, réseau trop grand, mauvais type : refusés
    for bad in (["8.8.8.8"], ["1.0.0.0/24"], ["127.0.0.1"], ["10.0.0.0/16"], "192.168.1.1"):
        with pytest.raises(ScanError):
            expand(bad, own)

    def unknown(*a, **k):
        raise OSError("nom inconnu")

    monkeypatch.setattr(socket, "getaddrinfo", unknown)
    with pytest.raises(ScanError, match="invalide"):
        expand(["machine-inexistante"], own)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    with pytest.raises(ScanError, match="pas une adresse privée"):  # nom public : refusé
        expand(["exemple.com"], own)


def test_default_targets(monkeypatch):
    monkeypatch.delenv("GATEWAY_NETWORK_HOSTS", raising=False)
    assert default_targets(["192.168.1.197", "10.8.0.3"]) == ["10.8.0.0/24", "192.168.1.0/24"]
    monkeypatch.setenv("GATEWAY_NETWORK_HOSTS", "192.168.1.20, pc-gpu.local")
    assert default_targets(["192.168.1.197"]) == ["192.168.1.20", "pc-gpu.local"]


def test_suggest_name():
    assert suggest_name("ollama", "192.168.1.20", set()) == "ollama-192-168-1-20"
    assert suggest_name("ollama", "192.168.1.20", {"ollama-192-168-1-20"}) == "ollama-192-168-1-20-2"
    assert len(suggest_name("textgen", "192.168.100.200", set())) <= 31


def test_real_tcp_probe():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    sem = asyncio.Semaphore(4)
    try:
        assert asyncio.run(ns._open("127.0.0.1", port, sem)) is True
    finally:
        srv.close()
    assert asyncio.run(ns._open("127.0.0.1", port, asyncio.Semaphore(4))) is False


def make(monkeypatch=None):
    return create_app(
        [],
        httpx.AsyncClient(transport=httpx.MockTransport(network)),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )


def test_scan_find_add_and_use(fake_tcp, monkeypatch):
    monkeypatch.delenv("GATEWAY_NETWORK_HOSTS", raising=False)
    with TestClient(make()) as c:
        r = c.post("/v1/admin/network/scan", headers=MASTER, json={})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["targets"] == ["192.168.1.0/24"] and out["scanned"] == 253
        by_host = {f["host"]: f for f in out["found"]}
        assert set(by_host) == {"192.168.1.20", "192.168.1.30"}  # le site web de .40 est ignoré
        ol = by_host["192.168.1.20"]
        assert ol["server"] == "ollama" and ol["models"] == ["llama3.1:8b", "qwen2.5:7b"]
        assert ol["suggested_name"] == "ollama-192-168-1-20" and ol["provider"] is None
        assert by_host["192.168.1.30"]["needs_key"] is True
        # ajout en un clic : modèle local, 0 €
        add = c.post(
            "/v1/admin/providers",
            headers=MASTER,
            json={"name": ol["suggested_name"], "base_url": ol["base_url"], "kind": "openai", "hosting": "local"},
        )
        assert add.status_code == 201 and add.json()["models"] == ["llama3.1:8b", "qwen2.5:7b"]
        chat = c.post(
            "/v1/chat/completions",
            headers=MASTER,
            json={"model": "ollama-192-168-1-20/llama3.1:8b", "messages": [{"role": "user", "content": "Bonjour"}]},
        )
        assert chat.status_code == 200 and chat.json()["x_gateway"]["cost_usd"] == 0
        assert chat.json()["x_gateway"]["hosting"] == "local"
        # nouvelle recherche : signalé comme déjà ajouté
        again = {
            f["host"]: f
            for f in c.post("/v1/admin/network/scan", headers=MASTER, json={"hosts": ["192.168.1.20"]}).json()["found"]
        }
        assert again["192.168.1.20"]["provider"] == "ollama-192-168-1-20"
        # validation et droits
        assert c.post("/v1/admin/network/scan", headers=MASTER, json={"hosts": ["8.8.8.8"]}).status_code == 400
        k = c.post("/v1/keys", headers=MASTER, json={"name": "k"}).json()
        assert (
            c.post("/v1/admin/network/scan", headers={"Authorization": f"Bearer {k['key']}"}, json={}).status_code
            == 403
        )


def test_scan_refused_on_hosted_instance(fake_tcp, monkeypatch):
    monkeypatch.setenv("GATEWAY_HOSTED", "true")
    monkeypatch.setenv("GATEWAY_ENCRYPTION_KEY", "q0Zc7oV2m1kq8bX4n9yL3pR5tW6uA2sD1fG7hJ8kL0M=")
    with TestClient(make()) as c:
        r = c.post("/v1/admin/network/scan", headers=MASTER, json={})
        assert r.status_code == 409 and "hébergée" in r.json()["error"]["message"]
