"""Connexion avec un compte lezard-ia.fr sur la page de connexion de ce PC : compte d'abord, « sans compte » ensuite ;
relais des seuls appels de connexion ; le compte du propriétaire a les droits de la clé maître."""

import json
import re
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from gateway.api import site_account
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

WEB = Path(__file__).resolve().parents[1] / "gateway" / "web"
SITE_JS = Path(__file__).resolve().parents[1] / "site" / "assets" / "compte.js"


def relay_app(service):
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.host == "lezard-ia.fr":
            seen.append((request.url.path, json.loads(request.content)))
            return service(request)
        return httpx.Response(404, json={})

    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        plans=PlanResolver({}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    return app, seen


def test_relay_forwards_only_the_login_calls():
    app, seen = relay_app(lambda r: httpx.Response(200, json={"salt": "c2Fs", "iter": 600000}))
    with TestClient(app) as c:
        r = c.post("/v1/compte/sel", json={"user": "lea", "auth": "x", "vault": {"data": "?"}, "email": "a@b.c"})
        assert r.status_code == 200 and r.json() == {"salt": "c2Fs", "iter": 600000}
        assert seen == [("/api/compte/sel", {"user": "lea", "auth": "x"})]  # rien d'autre ne part
        for action in (
            "creer",
            "coffre",
            "supprimer",
            "oubli",
            "reinitialiser",
        ):  # écrire, effacer : sur le site seulement
            assert c.post(f"/v1/compte/{action}", json={"user": "lea"}).status_code == 404
        assert len(seen) == 1


def test_relay_keeps_the_service_answer_and_caps_attempts(monkeypatch):
    app, _ = relay_app(lambda r: httpx.Response(401, json={"error": "Identifiant ou mot de passe incorrect."}))
    monkeypatch.setattr(site_account, "RELAY_PER_MINUTE", 3)
    with TestClient(app) as c:
        for _ in range(3):
            r = c.post("/v1/compte/connexion", json={"user": "lea", "auth": "faux"})
            assert r.status_code == 401 and r.json()["error"] == "Identifiant ou mot de passe incorrect."
        r = c.post("/v1/compte/connexion", json={"user": "lea", "auth": "faux"})
        assert r.status_code == 429 and "une minute" in r.json()["error"]


def test_relay_says_when_the_service_is_unreachable():
    def down(request):
        raise httpx.ConnectError("hors ligne")

    app, _ = relay_app(down)
    with TestClient(app) as c:
        r = c.post("/v1/compte/sel", json={"user": "lea"})
        assert r.status_code == 502 and "injoignable" in r.json()["error"]


def test_login_page_asks_for_the_account_first():
    html = (WEB / "login.html").read_text(encoding="utf-8")
    assert html.index('id="account-form"') < html.index('id="no-account"') < html.index('id="login-form"')
    assert "Se connecter sans compte" in html and 'id="without-account" hidden' in html
    js = (WEB / "static" / "login.js").read_text(encoding="utf-8")
    assert 'location.hash === "#manuel"' in js  # « connexion sans compte » du site : la clé directement


def test_same_cryptography_as_the_site():
    """Le coffre est chiffré par le site (site/assets/compte.js) : la page de ce PC doit le lire à l'identique."""
    page = (WEB / "static" / "account-login.js").read_text(encoding="utf-8")
    assert "const ITER_MIN = 600000;" in page and 'hash: "SHA-256"' in page and "512" in page
    assert "bits.slice(0, 32)" in page and "bits.slice(32)" in page and "additionalData: enc.encode(user)" in page
    assert 'normalize("NFC")' in page and ".trim().toLowerCase()" in page
    if SITE_JS.is_file():  # dépôt privé : comparé au site lui-même
        site = SITE_JS.read_text(encoding="utf-8")
        assert (
            re.search(r"ITER_MIN = 600000", site)
            and "bits.slice(0, 32)" in site
            and "additionalData: enc.encode(user)" in site
        )
