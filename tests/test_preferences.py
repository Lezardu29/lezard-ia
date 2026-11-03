import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.preferences import PreferenceError, normalize_color, validate_chat, validate_theme
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer master"}


def test_normalize_color():
    assert normalize_color("#ABC") == "#aabbcc" and normalize_color("12AB34") == "#12ab34"
    assert normalize_color("rgb(255, 0, 10)") == "#ff000a" and normalize_color("rgba(1,2,3,0.5)") == "#010203"
    for bad in ("rgb(300,0,0)", "bleu", "#12345", 42, "<script>"):
        with pytest.raises(PreferenceError):
            normalize_color(bad)


def test_validate_theme():
    assert validate_theme({}) == {"mode": "auto", "preset": "default"}
    assert validate_theme({"preset": "indigo"})["preset"] == "indigo"  # ancien thème par défaut, toujours au choix
    t = validate_theme({"mode": "dark", "preset": "custom", "custom": {"accent": "#F00", "border": None}})
    assert t == {"mode": "dark", "preset": "custom", "custom": {"accent": "#ff0000"}}
    for bad in (
        {"mode": "nuit"},
        {"preset": "rainbow"},
        {"custom": {"font": "#fff"}},
        {"custom": {"accent": "url(javascript:x)"}},
        {"extra": 1},
        [],
    ):
        with pytest.raises(PreferenceError):
            validate_theme(bad)


@pytest.fixture
def client():
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))),
        plans=PlanResolver({}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        yield c


def test_theme_roundtrip_and_scopes(client):
    assert client.get("/v1/preferences/theme", headers=MASTER).json() == {"theme": None, "updated_at": None}
    r = client.put(
        "/v1/preferences/theme",
        headers=MASTER,
        json={"mode": "dark", "preset": "emerald", "custom": {"bubble": "rgb(10,20,30)"}},
    ).json()
    assert r["theme"] == {"mode": "dark", "preset": "emerald", "custom": {"bubble": "#0a141e"}} and r["updated_at"]
    assert client.get("/v1/preferences/theme", headers=MASTER).json()["theme"]["preset"] == "emerald"
    # chaque clé virtuelle (appareil/appli) a son propre thème
    k = client.post("/v1/keys", headers=MASTER, json={"name": "mobile"}).json()
    hk = {"Authorization": f"Bearer {k['key']}"}
    assert client.get("/v1/preferences/theme", headers=hk).json()["theme"] is None
    client.put("/v1/preferences/theme", headers=hk, json={"mode": "light", "preset": "cyber"})
    assert client.get("/v1/preferences/theme", headers=hk).json()["theme"]["preset"] == "cyber"
    assert client.get("/v1/preferences/theme", headers=MASTER).json()["theme"]["preset"] == "emerald"
    # mise à jour = remplacement + nouvelle date
    first = client.get("/v1/preferences/theme", headers=hk).json()["updated_at"]
    r = client.put("/v1/preferences/theme", headers=hk, json={"mode": "auto"}).json()
    assert r["theme"] == {"mode": "auto", "preset": "default"} and r["updated_at"] >= first
    assert client.delete("/v1/preferences/theme", headers=hk).json()["deleted"]
    assert client.get("/v1/preferences/theme", headers=hk).json()["theme"] is None


def test_theme_errors(client):
    assert client.put("/v1/preferences/theme", headers=MASTER, json={"mode": "nuit"}).status_code == 400
    assert client.put("/v1/preferences/theme", json={"mode": "dark"}).status_code == 400  # anonyme
    assert client.get("/v1/preferences/theme").json()["theme"] is None


def test_pages_load_theme_engine_first(client):
    for path in ("/", "/app?frame=1"):
        html = client.get(path).text
        assert html.index('src="/static/theme.js"') < html.index('type="module"')  # avant le rendu
    assert client.get("/static/theme.js").status_code == 200
    assert client.get("/static/theme.css").status_code == 200


def test_chat_forced_model():
    """Modèle imposé dans le chat : « fournisseur/modèle » enregistré, vide = routage automatique."""
    assert validate_chat({"model": "ollama/gemma3:12b"})["model"] == "ollama/gemma3:12b"
    assert "model" not in validate_chat({"model": ""})
    for bad in ("gemma3", "ollama/", "ollama/a b", 42, "x" * 300):
        with pytest.raises(PreferenceError):
            validate_chat({"model": bad})
