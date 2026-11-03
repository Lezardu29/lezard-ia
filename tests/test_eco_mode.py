"""Mode économie : les IA locales consomment le moins possible de ressources de ce PC pendant une durée."""

import json

import httpx
from fastapi.testclient import TestClient

from gateway import power
from gateway.config import ProviderConfig
from gateway.local_gpu import LocalGpu, params_b
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
ACCOUNT = {"Authorization": "Bearer compte-0123456789abcdef"}


class Upstream:
    def __init__(self):
        self.chats: list[dict] = []
        self.api: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}, {"id": "qwen2.5:32b"}]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:32b", "size": 2e10, "size_vram": 1e10}]})
        body = json.loads(request.content)
        if path == "/api/show":  # nombre de couches : num_gpu en mode économie (18.7)
            return httpx.Response(200, json={"model_info": {"llama.block_count": 31 if "8b" in body["model"] else 63}})
        if path == "/api/chat":  # mode économie : API native, vue ici comme une requête OpenAI
            n = body["options"].get("num_predict")
            self.chats.append({"model": body["model"], **({"max_tokens": n} if n else {}), "native": body})
            return httpx.Response(
                200,
                json={
                    "message": {"role": "assistant", "content": "ok"},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 10,
                    "eval_count": 5,
                },
            )
        if path.startswith("/api/"):
            self.api.append(f"{path} {body['model']} keep_alive={body.get('keep_alive')}")
            return httpx.Response(200, json={"done": True})
        self.chats.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )


class FakePower:
    """Aucun vrai processus n'est touché : Ollama simulé (PID 4242, priorité normale)."""

    def __init__(self, monkeypatch):
        self.priority = {4242: power.NORMAL}
        self.throttled: list = []
        monkeypatch.setattr(power, "find_processes", lambda names: list(self.priority))
        monkeypatch.setattr(power, "get_priority", lambda pid: self.priority.get(pid))
        monkeypatch.setattr(power, "set_priority", self.set_priority)
        monkeypatch.setattr(
            power, "set_power_throttling", lambda on, pid=None: self.throttled.append((pid, on)) or True
        )

    def set_priority(self, pid, value):
        self.priority[pid] = value
        return True


def make(up, store=None):
    return create_app(
        [ProviderConfig("ollama", "openai", "http://127.0.0.1:11434/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({"compte-0123456789abcdef": "free"}, "free", master_key=MASTER_KEY),
        store=store or DocumentStore(":memory:"),
    )


def chat(c, model="auto", **extra):
    return c.post(
        "/v1/chat/completions",
        headers=MASTER,
        json={"model": model, "messages": [{"role": "user", "content": "Bonjour"}], **extra},
    )


HARD = {"reasoning_effort": "high"}  # sans mode économie, le routeur prend le gros modèle


def test_eco_mode_lightens_local_ai_then_restores(monkeypatch):
    fake, up = FakePower(monkeypatch), Upstream()
    with TestClient(make(up)) as c:
        chat(c, "auto", **HARD)
        assert up.chats[-1]["model"] == "qwen2.5:32b" and "max_tokens" not in up.chats[-1]
        assert c.post("/v1/admin/gpu/eco", headers=ACCOUNT, json={"on": True}).status_code == 403
        r = c.post("/v1/admin/gpu/eco", headers=MASTER, json={"on": True, "minutes": 60})
        eco = r.json()["eco"]
        assert eco["on"] and eco["until"] and eco["settings"]["max_concurrent"] == 1
        assert "/api/generate qwen2.5:32b keep_alive=0" in up.api  # modèle chargé libéré au début
        assert (
            fake.priority[4242] == power.low_value()
            and (4242, True) in fake.throttled
            and (None, True) in fake.throttled
        )
        r = chat(c, "auto", **HARD)
        assert up.chats[-1]["model"] == "llama3.1:8b"  # routage automatique : le modèle léger
        assert up.chats[-1]["max_tokens"] == 1024 and r.json()["x_gateway"]["eco"] is True
        chat(c, "ollama/qwen2.5:32b", max_tokens=300)  # modèle demandé : servi, demande plus courte gardée
        assert up.chats[-1]["model"] == "qwen2.5:32b" and up.chats[-1]["max_tokens"] == 300
        chat(c, "ollama/qwen2.5:32b", max_completion_tokens=5000)
        assert up.chats[-1]["max_tokens"] == 1024 and "max_completion_tokens" not in up.chats[-1]
        native = up.chats[-1]["native"]  # Ollama de ce PC : chargement bridé (18.7), libéré après 60 s
        assert (
            native["keep_alive"] == 60 and native["options"]["num_gpu"] == 32 and native["options"]["num_ctx"] == 4096
        )
        assert not up.api[-1].endswith("keep_alive=60")  # pas de rappel sans options : Ollama ne recharge pas
        st = c.get("/v1/admin/gpu", headers=MASTER).json()
        assert st["eco"]["on"] and st["eco"]["lowered_processes"] == 1 and st["servers"][0]["max_concurrent"] == 1
        r = c.post("/v1/admin/gpu/eco", headers=MASTER, json={"on": False})
        assert r.json()["eco"]["on"] is False
        assert fake.priority[4242] == power.NORMAL and (4242, None) in fake.throttled  # rendu à Windows
        chat(c, "auto", **HARD)
        assert up.chats[-1]["model"] == "qwen2.5:32b" and "max_tokens" not in up.chats[-1]
        entries = c.get("/v1/admin/audit", headers=MASTER).json()["data"]
        assert [e["status"] for e in entries if e["action"] == "gpu.eco"] == [200, 200, 403]  # refus compris


def test_eco_settings_validation_and_options(monkeypatch):
    FakePower(monkeypatch)
    up = Upstream()
    with TestClient(make(up)) as c:
        for bad in (
            {"eco": {"max_concurrent": 0}},
            {"eco": {"max_tokens": "1"}},
            {"eco": {"low_priority": 1}},
            {"eco": {"inconnu": True}},
            {"eco": []},
        ):
            assert c.put("/v1/admin/gpu/settings", headers=MASTER, json=bad).status_code == 400, bad
        for bad in ({"on": "oui"}, {"on": True, "minutes": 0}, {"on": True, "minutes": 99999}):
            assert c.post("/v1/admin/gpu/eco", headers=MASTER, json=bad).status_code == 400, bad
        r = c.put(
            "/v1/admin/gpu/settings",
            headers=MASTER,
            json={"eco": {"max_tokens": 0, "max_params_b": 0, "unload": False, "low_priority": False}},
        )
        assert r.json()["eco"]["settings"]["max_tokens"] == 0
        c.post("/v1/admin/gpu/eco", headers=MASTER, json={"on": True})
        assert not up.api  # rien déchargé
        chat(c, "auto", **HARD)
        assert up.chats[-1]["model"] == "qwen2.5:32b" and "max_tokens" not in up.chats[-1]  # sans plafond ni filtre


def test_eco_expires_and_restart_restores_priorities(monkeypatch, tmp_path):
    fake = FakePower(monkeypatch)
    now = [1000.0]
    mgr = LocalGpu(
        {
            "ollama": type(
                "P",
                (),
                {
                    "hosting": "local",
                    "name": "ollama",
                    "gate": object(),
                    "cfg": type("C", (), {"base_url": "http://127.0.0.1:11434/v1"}),
                },
            )()
        },
        clock=lambda: now[0],
    )
    store = DocumentStore(str(tmp_path / "g.db"))
    mgr.attach(store)
    import asyncio

    asyncio.run(mgr.set_eco(True, 30))
    assert fake.priority[4242] == power.low_value() and mgr.eco_on()
    # la gateway redémarre pendant le mode, qui se termine avant le premier tick : priorité rendue quand même
    now[0] += 31 * 60
    again = LocalGpu(mgr.providers, clock=lambda: now[0])
    again.attach(store)
    assert again.lowered == {"4242": power.NORMAL} and not again.eco_on()
    ended = []
    again.on_eco_change = lambda on, st: ended.append(on)
    asyncio.run(again.tick())
    assert fake.priority[4242] == power.NORMAL and again.lowered == {} and ended == []
    store.close()


def test_eco_filter_keeps_something_and_params():
    assert params_b("llama3.1:8b") == 8 and params_b("gemma4:e4b") == 4 and params_b("bge-m3") is None
    mgr = LocalGpu({})
    mgr.eco_manual = {"at": 0, "until": None}
    M = type("M", (), {})

    def m(i, hosting="local"):
        x = M()
        x.id, x.hosting = i, hosting
        return x

    big, small, cloud = m("qwen2.5:32b"), m("llama3.1:8b"), m("gpt-4o", "cloud")
    assert mgr.eco_filter([big, small, cloud]) == [small, cloud]
    assert mgr.eco_filter([big, cloud]) == [big, cloud]  # aucun léger : on garde ce qu'il y a


class WithEmbeddings(Upstream):
    def __call__(self, request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}, {"id": "bge-m3:latest"}]})
        if request.url.path.endswith("/embeddings"):
            n = len(json.loads(request.content)["input"])
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(n)]})
        return super().__call__(request)


def test_document_indexing_paused_during_eco(monkeypatch):
    FakePower(monkeypatch)
    with TestClient(make(WithEmbeddings())) as c:
        c.post("/v1/admin/gpu/eco", headers=MASTER, json={"on": True})
        doc = c.post("/v1/documents", headers=MASTER, json={"filename": "a.txt", "content": "Bonjour"}).json()
        state = lambda: c.get(f"/v1/documents/{doc['id']}", headers=MASTER).json()["vectors"]["status"]  # noqa: E731
        assert state() == "paused"
        c.post("/v1/admin/gpu/eco", headers=MASTER, json={"on": False})
        for _ in range(200):  # la vectorisation reprend (sondage court, réglé plus bas)
            if state() == "ready":
                break
            __import__("time").sleep(0.02)
        assert state() == "ready"
