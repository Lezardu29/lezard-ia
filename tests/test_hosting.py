"""Instance hébergée (7.4) : tout est inclus, sans abonnement ; secrets forts exigés ; ponts vers les modèles locaux."""

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig, load_providers
from gateway.main import create_app
from gateway.plans import PlanResolver, account_identity
from gateway.store import DocumentStore

ACCOUNT_KEY, MASTER_KEY = "ct-compte-0123456789abcdefghij", "gw-master-0123456789abcdefghijklmnop"
ACCOUNT, MASTER = {"Authorization": f"Bearer {ACCOUNT_KEY}"}, {"Authorization": f"Bearer {MASTER_KEY}"}
OWNER = account_identity(ACCOUNT_KEY)
ENC_KEY = "q0Zc7oV2m1kq8bX4n9yL3pR5tW6uA2sD1fG7hJ8kL0M="  # 32 octets en base64 (instance hébergée, 9.2)


class Upstream:
    """Modèle cloud factice : liste de modèles et réponse courte."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]})
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GATEWAY_HOSTED", "true")
    monkeypatch.setenv("GATEWAY_ENCRYPTION_KEY", ENC_KEY)
    store = DocumentStore(":memory:")
    app = create_app(
        [ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k")],
        httpx.AsyncClient(transport=httpx.MockTransport(Upstream())),
        plans=PlanResolver({ACCOUNT_KEY: "free"}, "free", master_key=MASTER_KEY, require_key=True),
        store=store,
    )
    with TestClient(app) as c:
        yield c, store


def test_managed_instance_includes_everything(env):
    """Plus d'option payante : un compte utilise les modèles et crée ses ponts sans abonnement."""
    c, store = env
    assert chat(c, ACCOUNT).status_code == 200
    assert c.post("/v1/bridges", headers=ACCOUNT, json={"name": "maison"}).status_code == 201
    me = c.get("/v1/me", headers=ACCOUNT).json()
    assert me["hosting"] == {"instance": "managed", "bridges": True} and "upgrade" not in me
    assert c.get("/v1/billing", headers=ACCOUNT).status_code == 404  # plus de facturation


def chat(c, headers):
    return c.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "Bonjour"}]},
    )


def test_self_hosted_instance_is_not_gated(monkeypatch):
    monkeypatch.delenv("GATEWAY_HOSTED", raising=False)
    app = create_app(
        [ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="k")],
        httpx.AsyncClient(transport=httpx.MockTransport(Upstream())),
        plans=PlanResolver({ACCOUNT_KEY: "free"}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        assert chat(c, ACCOUNT).status_code == 200
        assert c.post("/v1/bridges", headers=ACCOUNT, json={"name": "maison"}).status_code == 201
        assert c.get("/v1/me", headers=ACCOUNT).json()["hosting"]["instance"] == "self-hosted"


def hosted_app(master: str, keys: dict | None = None):
    return create_app(
        [],
        httpx.AsyncClient(transport=httpx.MockTransport(Upstream())),
        plans=PlanResolver(keys or {}, "free", master_key=master, require_key=True),
        store=DocumentStore(":memory:"),
    )


@pytest.mark.parametrize(
    "enc, master, keys, expected",
    [
        (None, MASTER_KEY, {}, "GATEWAY_ENCRYPTION_KEY absente"),
        ("change-moi", MASTER_KEY, {}, "32 octets"),  # valeur de deploy/.env.example
        ("une phrase de passe assez longue", MASTER_KEY, {}, "32 octets"),  # phrase : refusée en hébergé
        (ENC_KEY, "change-moi-cle-maitre-longue-et-aleatoire", {}, "GATEWAY_MASTER_KEY"),
        (ENC_KEY, "trop-courte-0123456789", {}, "GATEWAY_MASTER_KEY"),
        (ENC_KEY, "", {}, "GATEWAY_MASTER_KEY absente"),
        (ENC_KEY, MASTER_KEY, {"compte-test": "free"}, "GATEWAY_KEYS (compte free)"),
    ],
)
def test_hosted_instance_refuses_weak_secrets(monkeypatch, tmp_path, enc, master, keys, expected):
    """9.2 : une instance hébergée ne démarre pas avec des secrets faibles ou d'exemple."""
    monkeypatch.setenv("GATEWAY_HOSTED", "true")
    monkeypatch.setenv("GATEWAY_ENCRYPTION_KEY_FILE", str(tmp_path / "enc.key"))
    if enc is None:
        monkeypatch.delenv("GATEWAY_ENCRYPTION_KEY", raising=False)
    else:
        monkeypatch.setenv("GATEWAY_ENCRYPTION_KEY", enc)
    with pytest.raises(RuntimeError) as e:
        with TestClient(hosted_app(master, keys)):
            pass
    assert "Démarrage refusé" in str(e.value) and expected in str(e.value)
    assert not (tmp_path / "enc.key").exists()  # aucun fichier de clé créé à côté de la base


def test_hosted_instance_starts_with_strong_secrets(monkeypatch):
    monkeypatch.setenv("GATEWAY_HOSTED", "true")
    monkeypatch.setenv("GATEWAY_ENCRYPTION_KEY", ENC_KEY)
    with TestClient(hosted_app(MASTER_KEY, {ACCOUNT_KEY: "free"})) as c:
        assert c.get("/health").status_code == 200
    # PC personnel : mêmes valeurs faibles tolérées (simple avertissement)
    monkeypatch.setenv("GATEWAY_HOSTED", "false")
    monkeypatch.setenv("GATEWAY_ENCRYPTION_KEY", "change-moi")
    with TestClient(hosted_app("master")) as c:
        assert c.get("/health").status_code == 200


def test_placeholder_master_key_blocks_exposure():
    """La clé maître d'exemple est longue (41 caractères) mais publique : elle compte comme devinable."""
    r = PlanResolver({}, "free", master_key="change-moi-cle-maitre-longue-et-aleatoire")
    assert r.weak_tokens() == ["GATEWAY_MASTER_KEY"]


def test_managed_instance_skips_container_localhost(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_AUTO_DETECT", "true")  # ancien comportement : IA courantes déclarées
    monkeypatch.setenv("GATEWAY_HOSTED", "true")
    names = {p.name for p in load_providers(str(tmp_path / "absent.json"))}
    assert "ollama" not in names and "openai" in names
    monkeypatch.setenv("GATEWAY_HOSTED", "false")
    assert "ollama" in {p.name for p in load_providers(str(tmp_path / "absent.json"))}
    # gateway en conteneur sur la machine de l'utilisateur : Ollama de l'hôte
    monkeypatch.setenv("GATEWAY_LOCAL_HOST", "host.docker.internal")
    urls = {p.name: p.base_url for p in load_providers(str(tmp_path / "absent.json"))}
    assert urls["ollama"] == "http://host.docker.internal:11434/v1" and urls["openai"].startswith("https://api.openai")
