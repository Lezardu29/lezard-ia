"""Webhooks d'alerte : pas de DNS rebinding (9.4)."""

import asyncio
import socket

import httpx
import pytest

from gateway.alerts import AlertEngine, pinned_request, resolve_webhook

PUBLIC, PUBLIC6 = "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"


class RebindingDNS:
    """DNS malveillant : une adresse publique à la 1re question, puis l'adresse des métadonnées cloud."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def __call__(self, host, port, *a, **kw):
        ip = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self, status=200):
        self.requests: list[httpx.Request] = []
        self.status = status

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(self.status)


def engine(transport):
    e = AlertEngine(
        store=None,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: pytest.fail("le client partagé ne doit pas servir aux webhooks publics")
            )
        ),
    )
    e.webhook_transport = transport
    return e


@pytest.fixture(autouse=True)
def no_private(monkeypatch):
    monkeypatch.delenv("ALERT_ALLOW_PRIVATE_WEBHOOKS", raising=False)


def test_connection_goes_to_the_checked_address(monkeypatch):
    dns = RebindingDNS([PUBLIC, "169.254.169.254"])
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    t = Recorder()
    ok = asyncio.run(engine(t).send("https://hooks.example.com:8443/x?y=1", "s", {"title": "T", "message": "M"}))
    assert ok and dns.calls == 1  # une seule résolution : pas de seconde réponse
    r = t.requests[0]
    assert r.url.host == PUBLIC and r.url.port == 8443 and r.url.path == "/x" and r.url.query == b"y=1"
    assert r.headers["host"] == "hooks.example.com:8443"
    assert r.extensions["sni_hostname"] == "hooks.example.com"  # certificat vérifié pour le vrai nom
    assert r.headers["x-gateway-signature"].startswith("sha256=")


def test_private_answer_is_refused_before_any_connection(monkeypatch):
    for bad in ("127.0.0.1", "169.254.169.254", "10.1.2.3", "::1", "fd00::1"):
        monkeypatch.setattr(socket, "getaddrinfo", RebindingDNS([bad]))
        t = Recorder()
        assert not asyncio.run(engine(t).send("https://hooks.example.com/x", None, {"title": "T", "message": "M"}))
        assert t.requests == []


def test_mixed_answers_are_refused(monkeypatch):
    def dns(host, port, *a, **kw):  # une adresse publique ET une interne
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    t = Recorder()
    assert not asyncio.run(engine(t).send("https://hooks.example.com/x", None, {"title": "T", "message": "M"}))
    assert t.requests == []


def test_retry_uses_another_checked_address(monkeypatch):
    def dns(host, port, *a, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (PUBLIC6, port, 0, 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    t = Recorder(status=500)
    assert not asyncio.run(engine(t).send("https://hooks.example.com/x", None, {"title": "T", "message": "M"}))
    assert [r.url.host for r in t.requests] == [PUBLIC, PUBLIC6]


def test_redirects_are_not_followed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", RebindingDNS([PUBLIC]))

    class Redirect(Recorder):
        async def handle_async_request(self, request):
            self.requests.append(request)
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

    t = Redirect()
    assert not asyncio.run(engine(t).send("https://hooks.example.com/x", None, {"title": "T", "message": "M"}))
    assert all(r.url.host == PUBLIC for r in t.requests)


def test_pinned_request_shapes(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", RebindingDNS([PUBLIC6]))
    u, ips = resolve_webhook("https://user:pw@hooks.example.com/a/b")
    url, host, ext = pinned_request(u, ips[0])
    assert url == f"https://user:pw@[{PUBLIC6}]/a/b"  # identifiants d'authentification basique conservés
    assert host == {"Host": "hooks.example.com"} and ext == {"sni_hostname": "hooks.example.com"}
    monkeypatch.setenv("ALERT_ALLOW_PRIVATE_WEBHOOKS", "true")
    assert resolve_webhook("http://192.168.1.10/hook")[1] == []  # dérogation : aucune adresse imposée
