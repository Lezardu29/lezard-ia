"""Modèles locaux au quotidien (18.4) : file d'attente équitable par serveur local, mode « PC occupé »,
keep_alive d'Ollama et déchargement des modèles."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import local_gpu
from gateway.config import ProviderConfig
from gateway.local_gpu import CALLER, Gate, LocalGpu, LocalGpuError, LocalQueueFull, scheduled_until, this_pc
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
ACCOUNT = {"Authorization": "Bearer compte-0123456789abcdef"}
OK = {
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


# ---------- file d'attente ----------


def fake_gate(limit=1, wait=5):
    mgr = LocalGpu({})
    mgr.config = {"max_concurrent": limit, "queue_wait": wait}
    p = SimpleNamespace(name="ollama", hosting="local", cfg=SimpleNamespace(base_url="http://127.0.0.1:11434/v1"))
    return Gate(mgr, p)


async def until(cond, seconds=10):
    """Attend qu'une condition soit vraie (machines lentes de la CI : jamais un nombre fixe d'itérations)."""
    for _ in range(int(seconds / 0.005)):
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition jamais atteinte")


def test_queue_is_fair_and_owner_goes_first():
    async def run():
        gate, order = fake_gate(limit=1), []
        await gate.acquire("alice", False)  # en cours

        async def ask(who, owner=False):
            await gate.acquire(who, owner)
            order.append(who)

        tasks = [asyncio.create_task(ask("alice")) for _ in range(3)]
        await until(lambda: len(gate.waiting) == 3)
        tasks.append(asyncio.create_task(ask("bob")))
        await until(lambda: len(gate.waiting) == 4)
        tasks.append(asyncio.create_task(ask("maitre", owner=True)))
        await until(lambda: len(gate.waiting) == 5)
        assert gate.status()["owner_waiting"] == 1
        holder = "alice"
        for n in range(1, 6):
            gate.release(holder)
            await until(lambda n=n: len(order) == n)
            holder = order[-1]
        await asyncio.gather(*tasks)
        return order

    # le propriétaire d'abord ; puis Bob, arrivé après la rafale d'Alice : elle a déjà eu son tour
    assert asyncio.run(run()) == ["maitre", "bob", "alice", "alice", "alice"]


def test_queue_limits_per_user_and_waiting_time(monkeypatch):
    monkeypatch.setenv("LOCAL_QUEUE_PER_USER", "2")

    async def run():
        gate = fake_gate(limit=1, wait=1)
        await gate.acquire("alice", False)
        waiting = [asyncio.create_task(gate.acquire("bob", False)) for _ in range(2)]
        await until(lambda: len(gate.waiting) == 2)
        with pytest.raises(LocalQueueFull) as full:  # 3e en attente pour Bob : refus immédiat
            await gate.acquire("bob", False)
        assert full.value.status == 429
        results = await asyncio.gather(*waiting, return_exceptions=True)
        assert all(isinstance(r, LocalQueueFull) and r.status == 503 for r in results)  # attente écoulée
        assert gate.status()["waiting"] == 0 and gate.total == 1
        gate.release("alice")
        assert gate.total == 0
        # abandon en attente (client déconnecté) : retiré de la file
        await gate.acquire("alice", False)
        t = asyncio.create_task(gate.acquire("bob", False))
        await until(lambda: len(gate.waiting) == 1)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        assert not gate.waiting
        gate.release("alice")
        assert gate.total == 0
        # abandon au moment même où la place est accordée : elle est rendue
        await gate.acquire("alice", False)
        t = asyncio.create_task(gate.acquire("bob", False))
        await until(lambda: len(gate.waiting) == 1)
        gate.release("alice")  # place accordée à Bob…
        t.cancel()  # …qui se déconnecte avant de l'utiliser
        await asyncio.gather(t, return_exceptions=True)
        if not t.cancelled():  # Python 3.11 : wait_for rend la place accordée malgré l'annulation
            assert gate.active == {"bob": 1}  # la requête continue et la rendra dans son finally
            gate.release("bob")
        assert gate.total == 0 and not gate.waiting

    asyncio.run(run())


class SlowLocal:
    """Ollama simulé : les réponses n'arrivent qu'une fois `go` déclenché ; compte les appels simultanés."""

    def __init__(self):
        self.go = asyncio.Event()
        self.running = self.peak = 0
        self.order: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
        if request.url.path.startswith("/api/"):
            return httpx.Response(404)
        body = json.loads(request.content)
        self.order.append(body["messages"][0]["content"])
        self.running += 1
        self.peak = max(self.peak, self.running)
        await self.go.wait()
        self.running -= 1
        return httpx.Response(200, json=OK)


def test_parallel_requests_to_a_local_server_are_queued():
    async def run():
        up = SlowLocal()
        app = create_app(
            [ProviderConfig("ollama", "openai", "http://127.0.0.1:11434/v1", "local")],
            httpx.AsyncClient(transport=httpx.MockTransport(up)),
            plans=PlanResolver({"compte-0123456789abcdef": "free"}, "free", master_key=MASTER_KEY),
            store=DocumentStore(":memory:"),
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                r = await c.put("/v1/admin/gpu/settings", headers=MASTER, json={"max_concurrent": 2})
                assert r.status_code == 200, r.text

                def send(text, h=ACCOUNT):
                    return asyncio.create_task(
                        c.post(
                            "/v1/chat/completions",
                            headers=h,
                            json={"model": "ollama/llama3.1:8b", "messages": [{"role": "user", "content": text}]},
                        )
                    )

                tasks = [send(f"compte {i}") for i in range(4)]
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if len(up.order) == 2:
                        break
                tasks.append(send("maitre", MASTER))
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    st = (await c.get("/v1/admin/gpu", headers=MASTER)).json()["servers"][0]
                    if st["waiting"] == 3:
                        break
                assert st["active"] == 2 and st["max_concurrent"] == 2 and st["owner_waiting"] == 1
                up.go.set()
                codes = [r.status_code for r in await asyncio.gather(*tasks)]
                return up, codes

    up, codes = asyncio.run(run())
    assert codes == [200] * 5
    assert up.peak == 2  # jamais plus de 2 à la fois
    assert up.order[2] == "maitre"  # le propriétaire passe en premier


# ---------- PC occupé ----------


class Upstream:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path.endswith("/models"):
            ids = {
                "127.0.0.1": ["llama3.1:8b"],
                "gemini": ["models/gemini-2.5-flash"],
                "openai": ["gpt-4o-mini"],
                "pc-salon": ["qwen2.5:7b"],
            }[host]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        if path == "/api/ps":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "llama3.1:8b",
                            "size": 5_000_000_000,
                            "size_vram": 4_800_000_000,
                            "expires_at": "2026-10-20T12:00:00+02:00",
                        }
                    ]
                },
            )
        body = json.loads(request.content)
        self.calls.append((host, path if path.startswith("/api/") else body["model"]))
        if path.startswith("/api/"):
            self.calls[-1] = (host, f"{path} {body['model']} keep_alive={body.get('keep_alive')}")
            return httpx.Response(200, json={"done": True})
        return httpx.Response(200, json=OK)


def make(up, store=None, lan=False):
    configs = [
        ProviderConfig("ollama", "openai", "http://127.0.0.1:11434/v1", "local"),
        ProviderConfig("gemini", "openai", "http://gemini/v1beta/openai", "cloud", api_key="g"),
        ProviderConfig("openai", "openai", "http://openai/v1", "cloud", api_key="o"),
    ]
    if lan:
        configs.append(ProviderConfig("salon", "openai", "http://pc-salon:11434/v1", "local"))
    return create_app(
        configs,
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({"compte-0123456789abcdef": "free"}, "free", master_key=MASTER_KEY),
        store=store or DocumentStore(":memory:"),
    )


def chat(c, h, model="auto/economy"):
    return c.post(
        "/v1/chat/completions", headers=h, json={"model": model, "messages": [{"role": "user", "content": "Bonjour"}]}
    )


def key(c, **perms):
    r = c.post("/v1/keys", headers=MASTER, json={"name": "k", "permissions": perms})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['key']}"}


def test_busy_mode_reserves_this_pc_to_the_owner():
    up = Upstream()
    with TestClient(make(up)) as c:
        assert c.put("/v1/admin/free-tiers/gemini", headers=MASTER, json={"enabled": True}).status_code == 200
        assert chat(c, ACCOUNT).json()["x_gateway"]["provider"] == "ollama"  # local d'abord
        free_key, cloud_key, refuse_key = key(c), key(c, when_busy="cloud"), key(c, when_busy="refuse")
        assert (
            c.post("/v1/keys", headers=MASTER, json={"name": "x", "permissions": {"when_busy": "jamais"}}).status_code
            == 400
        )
        assert c.post("/v1/admin/gpu/busy", headers=ACCOUNT, json={"on": True}).status_code == 403
        r = c.post("/v1/admin/gpu/busy", headers=MASTER, json={"on": True, "minutes": 90})
        assert r.status_code == 200 and r.json()["busy"]["on"] and r.json()["busy"]["providers"] == ["ollama"]
        assert any("keep_alive=0" in call for _, call in up.calls)  # VRAM libérée
        assert chat(c, MASTER).json()["x_gateway"]["provider"] == "ollama"  # le propriétaire garde son GPU
        assert chat(c, free_key).json()["x_gateway"]["provider"] == "gemini"  # défaut : offre gratuite
        assert chat(c, ACCOUNT).json()["x_gateway"]["provider"] == "gemini"
        assert chat(c, cloud_key, "auto/speed").json()["x_gateway"]["hosting"] == "cloud"
        r = chat(c, refuse_key)
        assert r.status_code == 503 and "PC occupé" in r.json()["error"]["message"]
        assert 5000 <= int(r.headers["retry-after"]) <= 5400
        r = chat(c, free_key, "ollama/llama3.1:8b")  # demandé explicitement
        assert r.status_code == 503 and r.json()["error"]["provider"] == "local_busy"
        assert c.post("/v1/admin/gpu/busy", headers=MASTER, json={"on": False}).json()["busy"]["on"] is False
        assert chat(c, refuse_key).json()["x_gateway"]["provider"] == "ollama"
        actions = [e["action"] for e in c.get("/v1/admin/audit", headers=MASTER).json()["data"] if e["status"] == 200]
        assert actions.count("gpu.busy") == 2


def test_busy_mode_spares_other_pcs_and_fallbacks_continue():
    up = Upstream()
    with TestClient(make(up, lan=True)) as c:
        c.post("/v1/admin/gpu/busy", headers=MASTER, json={"on": True})
        refuse = key(c, when_busy="refuse")
        assert chat(c, refuse).json()["x_gateway"]["provider"] == "salon"  # autre PC du réseau : permis
        r = c.post(
            "/v1/chat/completions",
            headers=ACCOUNT,
            json={
                "model": "ollama/llama3.1:8b",
                "fallbacks": ["openai/gpt-4o-mini"],
                "messages": [{"role": "user", "content": "Bonjour"}],
            },
        )
        assert r.status_code == 200 and r.json()["x_gateway"]["provider"] == "openai"
        assert r.json()["x_gateway"]["fallback"]["attempts"][0]["status"] == 503
        st = c.get("/v1/admin/gpu", headers=MASTER).json()
        servers = {s["name"]: s for s in st["servers"]}
        assert servers["ollama"]["this_pc"] and not servers["salon"]["this_pc"]
        assert servers["ollama"]["busy_reserved"] and not servers["salon"]["busy_reserved"]
        assert servers["ollama"]["loaded"][0]["model"] == "llama3.1:8b"
        # la liste des serveurs réservés est réglable
        r = c.put("/v1/admin/gpu/settings", headers=MASTER, json={"busy": {"providers": ["ollama", "salon"]}})
        assert r.json()["busy"]["providers"] == ["ollama", "salon"]
        assert chat(c, refuse).status_code == 503


def test_schedule_windows():
    sched = [
        {"days": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"},
        {"days": [6], "start": "22:00", "end": "02:00"},
    ]
    at = lambda *a: datetime(*a).timestamp()  # noqa: E731
    assert scheduled_until(sched, at(2026, 10, 19, 10, 0)) == at(2026, 10, 19, 18, 0)  # lundi
    assert scheduled_until(sched, at(2026, 10, 19, 18, 0)) is None
    assert scheduled_until(sched, at(2026, 10, 24, 10, 0)) is None  # samedi matin
    assert scheduled_until(sched, at(2026, 10, 24, 23, 0)) == at(2026, 10, 25, 2, 0)  # samedi soir
    assert scheduled_until(sched, at(2026, 10, 25, 1, 0)) == at(2026, 10, 25, 2, 0)  # nuit de samedi
    assert scheduled_until(sched, at(2026, 10, 26, 1, 0)) is None  # nuit de dimanche


def test_schedule_turns_busy_mode_on_and_off():
    up, now = Upstream(), [datetime(2026, 10, 19, 8, 59).timestamp()]
    with TestClient(make(up)) as c:
        r = c.put(
            "/v1/admin/gpu/settings",
            headers=MASTER,
            json={"busy": {"schedule": [{"days": [1], "start": "09:00", "end": "12:30"}], "unload": True}},
        )
        assert r.status_code == 200, r.text
        for bad in (
            {"busy": {"schedule": [{"days": [8], "start": "09:00", "end": "10:00"}]}},
            {"busy": {"schedule": [{"days": [1], "start": "9h", "end": "10:00"}]}},
            {"keep_alive": -5},
            {"max_concurrent": 0},
            {"servers": {"inconnu": {"max_concurrent": 2}}},
            {"bruit": 1},
        ):
            assert c.put("/v1/admin/gpu/settings", headers=MASTER, json=bad).status_code == 400, bad
        assert c.get("/v1/admin/gpu", headers=MASTER).json()["config"]["busy"]["schedule"][0]["end"] == "12:30"
    mgr = LocalGpu(
        {
            "ollama": SimpleNamespace(
                name="ollama", hosting="local", gate=object(), cfg=SimpleNamespace(base_url="http://localhost:11434/v1")
            )
        },
        clock=lambda: now[0],
    )
    mgr.config = local_gpu.validate(
        {"busy": {"schedule": [{"days": [1], "start": "09:00", "end": "12:30"}]}}, {"ollama"}
    )
    events = []
    mgr.on_change = lambda on, st: events.append((on, st["source"]))
    asyncio.run(mgr.tick())
    assert mgr.busy() == set()
    now[0] += 120
    assert mgr.busy() == {"ollama"} and mgr.busy_state()["until"] == datetime(2026, 10, 19, 12, 30).timestamp()

    async def no_unload(name, model=None):
        events.append(("unload", name))
        return []

    mgr.unload = no_unload
    asyncio.run(mgr.tick())
    now[0] = datetime(2026, 10, 19, 12, 31).timestamp()
    asyncio.run(mgr.tick())
    assert events == [(True, "schedule"), ("unload", "ollama"), (False, None)]


def test_manual_busy_expires():
    now = [1000.0]
    mgr = LocalGpu(
        {
            "ollama": SimpleNamespace(
                name="ollama", hosting="local", gate=object(), cfg=SimpleNamespace(base_url="http://127.0.0.1:11434/v1")
            )
        },
        clock=lambda: now[0],
    )
    mgr.config = {"busy": {"unload": False}}
    asyncio.run(mgr.set_busy(True, 30))
    assert mgr.blocks(owner=False) and not mgr.blocks(owner=True)
    now[0] += 30 * 60
    assert not mgr.blocks(owner=False) and mgr.manual is None


def test_this_pc():
    assert this_pc("http://127.0.0.1:11434/v1") and this_pc("http://localhost:1234/v1")
    assert this_pc("http://host.docker.internal:11434/v1") and this_pc("http://[::1]:11434/v1")
    assert not this_pc("http://192.168.1.20:11434/v1") and not this_pc("http://bridge-x.invalid/v1")


# ---------- keep_alive et déchargement ----------


def test_keep_alive_is_applied_after_each_request(monkeypatch):
    monkeypatch.delenv("GATEWAY_KEEP_ALIVE", raising=False)  # réglage par défaut : une minute
    up = Upstream()
    with TestClient(make(up)) as c:
        chat(c, MASTER, "ollama/llama3.1:8b")  # défaut : déchargé après une minute sans usage
        assert [call for _, call in up.calls if "keep_alive" in call] == ["/api/generate llama3.1:8b keep_alive=60"]
        r = c.put("/v1/admin/gpu/settings", headers=MASTER, json={"keep_alive": 300})
        assert r.json()["config"]["keep_alive"] == 300
        chat(c, MASTER, "ollama/llama3.1:8b")
        chat(c, MASTER, "openai/gpt-4o-mini")  # cloud : rien
        assert [call for _, call in up.calls if "keep_alive" in call][1:] == [
            "/api/generate llama3.1:8b keep_alive=300"
        ]


def test_unload_models_from_the_admin():
    up = Upstream()
    with TestClient(make(up, lan=True)) as c:
        assert c.post("/v1/admin/gpu/unload", headers=ACCOUNT, json={"provider": "ollama"}).status_code == 403
        r = c.post("/v1/admin/gpu/unload", headers=MASTER, json={"provider": "ollama"})
        assert r.json() == {"unloaded": ["llama3.1:8b"]}
        r = c.post("/v1/admin/gpu/unload", headers=MASTER, json={"provider": "ollama", "model": "qwen3:4b"})
        assert r.json() == {"unloaded": ["qwen3:4b"]}
        assert [call for _, call in up.calls if "keep_alive" in call] == [
            "/api/generate llama3.1:8b keep_alive=0",
            "/api/generate qwen3:4b keep_alive=0",
        ]
        assert c.post("/v1/admin/gpu/unload", headers=MASTER, json={"provider": "openai"}).status_code == 404
        assert c.post("/v1/admin/gpu/unload", headers=MASTER, json={"provider": "absent"}).status_code == 404
        entries = c.get("/v1/admin/audit", headers=MASTER).json()["data"]
        assert [e["target"] for e in entries if e["action"] == "gpu.unload" and e["status"] == 200] == [
            "ollama/qwen3:4b",
            "ollama",
        ]


def test_local_gpu_error_is_400_by_default():
    assert LocalGpuError("x").status == 400
    assert CALLER.get() == ("système", False)
