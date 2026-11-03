"""Installation locale : dictée transcrite dans la gateway (faster-whisper, simulé ici) et recherche des IA de ce PC
pour la fenêtre « Ajouter un modèle »."""

from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from gateway import local_discovery
from gateway.api import admin as admin_api
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.providers import local_whisper
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 4000


class FakeWhisper:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **options):
        self.calls.append(options)
        segs = [
            SimpleNamespace(start=0.0, end=1.5, text=" Bonjour"),
            SimpleNamespace(start=1.5, end=3.2, text=" à tous."),
        ]
        return iter(segs), SimpleNamespace(language=options.get("language", "fr"), duration=3.2)


def cloud_upstream(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}, {"id": "whisper-1"}]})
        return httpx.Response(200, json={"text": "cloud", "duration": 3.0})

    return handler


def make(calls, monkeypatch, fake):
    monkeypatch.setattr(local_whisper, "enabled", lambda: True)
    monkeypatch.setattr(local_whisper.LocalWhisperProvider, "_load", lambda self: fake)
    return create_app(
        [ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="sk-test-123456")],
        httpx.AsyncClient(transport=httpx.MockTransport(cloud_upstream(calls))),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )


def post(c, headers=MASTER, **fields):
    return c.post(
        "/v1/audio/transcriptions",
        headers=headers,
        files={"file": ("dictee.webm", WEBM, "audio/webm")},
        data={"model": "auto", **fields},
    )


def test_dictation_transcribed_on_this_pc_before_any_cloud(monkeypatch):
    calls, fake = [], FakeWhisper()
    with TestClient(make(calls, monkeypatch, fake)) as c:
        assert c.get("/v1/audio/status", headers=MASTER).json()["local"] is True
        r = post(c, language="fr")
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["text"] == "Bonjour à tous." and out["x_gateway"]["provider"] == "whisper-local"
        assert out["x_gateway"]["hosting"] == "local" and out["x_gateway"]["cost_eur"] == 0
        assert out["x_gateway"]["audio_seconds"] == 3.2
        assert fake.calls[-1]["language"] == "fr" and fake.calls[-1]["vad_filter"] is True
        assert not any(p.endswith("/audio/transcriptions") for p in calls)  # rien envoyé au cloud
        srt = post(c, response_format="srt").text
        assert "00:00:00,000 --> 00:00:01,500" in srt and "Bonjour" in srt
        # mode privé : toujours la transcription locale
        r = post(c, headers={**MASTER, "X-Gateway-Private": "true"})
        assert r.json()["x_gateway"]["provider"] == "whisper-local"
        # jamais proposé comme modèle de chat
        chat = c.post(
            "/v1/chat/completions",
            headers=MASTER,
            json={"model": "whisper-local/whisper-small", "messages": [{"role": "user", "content": "x"}]},
        )
        assert chat.status_code >= 400


def test_discover_this_pc(monkeypatch):
    calls = []

    async def servers(client, instance_id):
        return [
            {
                "server": "ollama",
                "label": "Ollama",
                "port": 11434,
                "base_url": "http://localhost:11434/v1",
                "models": ["llama3.1:8b"],
                "needs_key": False,
            },
            {
                "server": "lmstudio",
                "label": "LM Studio",
                "port": 1234,
                "base_url": "http://localhost:1234/v1",
                "models": ["qwen2.5-7b"],
                "needs_key": False,
            },
        ]

    monkeypatch.setattr(admin_api, "find_servers", servers)
    monkeypatch.setattr(admin_api, "ollama_executable", lambda: None)
    app = create_app(
        [ProviderConfig("lmstudio", "openai", "http://127.0.0.1:1234/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(cloud_upstream(calls))),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        out = c.get("/v1/admin/local/discover", headers=MASTER).json()
        by = {s["server"]: s for s in out["servers"]}
        assert by["lmstudio"]["provider"] == "lmstudio"  # déjà ajouté (127.0.0.1 = localhost)
        assert by["ollama"]["provider"] is None and by["ollama"]["suggested_name"] == "ollama"
        assert out["ollama"] == {
            "installed": True,
            "running": True,
            "provider": None,
            "suggested_name": "ollama",
            "base_url": "http://localhost:11434/v1",
            "download_url": local_discovery.OLLAMA_DOWNLOAD,
        }
        assert out["catalog"]["models"] and "best" in out["catalog"]
        installed = {m["model"] for m in out["catalog"]["models"] if m["installed"]}
        assert installed <= {"llama3.1:8b"}  # modèles d'Ollama marqués installés
        assert all(not m["installed"] for m in out["catalog"]["models"] if m["model"] != "llama3.1:8b")
        assert c.get("/v1/admin/local/discover").status_code in (401, 403)
        # Ollama absent : pas de démarrage possible
        assert c.post("/v1/admin/local/ollama/start", headers=MASTER).status_code == 404


def test_recommendations_see_running_ollama_not_yet_added(monkeypatch):
    """Widget « Matériel » : Ollama tourne sur ce PC sans être ajouté → « Ajouter Ollama », et non « démarrez-le »."""

    async def servers(client, instance_id):
        return [
            {
                "server": "ollama",
                "label": "Ollama",
                "port": 11434,
                "base_url": "http://localhost:11434/v1",
                "models": ["gemma3:12b"],
                "needs_key": False,
            }
        ]

    monkeypatch.setattr(admin_api, "find_servers", servers)
    app = create_app(
        [],
        httpx.AsyncClient(transport=httpx.MockTransport(cloud_upstream([]))),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        out = c.get("/v1/admin/models/recommendations", headers=MASTER).json()
        assert out["ollama"] is None and out["ollama_running"] is True and out["ollama_installed"] is True
        assert [m["model"] for m in out["models"] if m["installed"]] in ([], ["gemma3:12b"])


def test_find_servers_skips_this_gateway(monkeypatch):
    async def scan(addresses, client, ports):
        assert addresses == ["127.0.0.1"]
        return [
            {
                "host": "127.0.0.1",
                "port": 8000,
                "server": "vllm",
                "base_url": "http://127.0.0.1:8000/v1",
                "models": ["auto"],
                "needs_key": False,
                "served_by": "moi",
            },
            {
                "host": "127.0.0.1",
                "port": 11434,
                "server": "ollama",
                "base_url": "http://127.0.0.1:11434/v1",
                "models": ["llama3.1:8b"],
                "needs_key": False,
                "served_by": None,
            },
        ]

    monkeypatch.setattr(local_discovery, "scan", scan)
    monkeypatch.delenv("GATEWAY_LOCAL_HOST", raising=False)
    import asyncio

    found = asyncio.run(local_discovery.find_servers(None, "moi"))
    assert [f["server"] for f in found] == ["ollama"] and found[0]["base_url"] == "http://localhost:11434/v1"
