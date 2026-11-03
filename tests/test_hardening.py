"""Petits durcissements (9.6) et limites par fournisseur sous requêtes simultanées."""

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

from gateway import plans as plans_module
from gateway.config import ProviderConfig
from gateway.counters import SqliteCounters
from gateway.limits import RateLimited
from gateway.main import create_app
from gateway.plans import PlanResolver, account_identity
from gateway.provider_rules import ProviderGuard, validate_rules
from gateway.remote import RemoteAccess
from gateway.store import DocumentStore

ROOT = Path(__file__).parent.parent
MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
ACCOUNT_KEY = "ct-compte-0123456789abcdefghijklmno"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
ACCOUNT = {"Authorization": f"Bearer {ACCOUNT_KEY}"}


# ---------- jetons de compte comparés en temps constant ----------


def test_account_tokens_are_compared_in_constant_time(monkeypatch):
    calls = []
    real = plans_module.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(plans_module.hmac, "compare_digest", spy)
    r = PlanResolver({ACCOUNT_KEY: "free"}, "free", master_key=MASTER_KEY)
    ctx = r.resolve(ACCOUNT_KEY, "1.2.3.4", None)
    assert ctx.identity == account_identity(ACCOUNT_KEY) and ctx.can_manage and not ctx.is_admin
    assert (ACCOUNT_KEY.encode(), ACCOUNT_KEY.encode()) in calls  # comparaison finale en temps constant
    assert r.resolve(MASTER_KEY, "1.2.3.4", None).is_admin
    for wrong in (ACCOUNT_KEY[:-1], ACCOUNT_KEY + "x", ACCOUNT_KEY.upper(), ""):
        assert r.account_token(wrong) is None
    # le dictionnaire n'est plus indexé par le jeton lui-même
    assert ACCOUNT_KEY not in r._by_digest and all(isinstance(k, bytes) for k in r._by_digest)


# ---------- /docs et /openapi.json ----------


def docs_app():
    return create_app(
        [],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        plans=PlanResolver({ACCOUNT_KEY: "free"}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )


async def docs_codes(ip: str, headers=None) -> dict:
    app = docs_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(ip, 5000)), base_url="http://t"
        ) as c:
            out = {p: (await c.get(p, headers=headers or {})).status_code for p in ("/docs", "/openapi.json", "/redoc")}
            spec = await c.get("/openapi.json", headers=headers or {})
            if spec.status_code == 200:
                out["paths"] = "/v1/chat/completions" in spec.json()["paths"]
            return out


def test_api_docs_reserved_to_this_machine_or_admin(monkeypatch):
    monkeypatch.delenv("GATEWAY_DOCS", raising=False)
    monkeypatch.delenv("GATEWAY_HOSTED", raising=False)
    local = asyncio.run(docs_codes("127.0.0.1"))
    assert local == {"/docs": 200, "/openapi.json": 200, "/redoc": 404, "paths": True}
    lan = asyncio.run(docs_codes("192.168.1.50"))  # autre appareil, sans clé
    assert lan == {"/docs": 404, "/openapi.json": 404, "/redoc": 404}
    assert asyncio.run(docs_codes("192.168.1.50", ACCOUNT))["/openapi.json"] == 404  # compte non admin
    admin = asyncio.run(docs_codes("203.0.113.9", MASTER))
    assert admin["/openapi.json"] == 200 and admin["paths"]
    # instance hébergée : jamais en accès libre, même depuis la machine
    monkeypatch.setenv("GATEWAY_HOSTED", "true")
    monkeypatch.setenv("GATEWAY_REQUIRE_KEY", "true")
    monkeypatch.setenv("GATEWAY_ENCRYPTION_KEY", "q0Zc7oV2m1kq8bX4n9yL3pR5tW6uA2sD1fG7hJ8kL0M=")
    assert asyncio.run(docs_codes("127.0.0.1"))["/openapi.json"] == 404
    assert asyncio.run(docs_codes("127.0.0.1", MASTER))["/openapi.json"] == 200
    monkeypatch.setenv("GATEWAY_DOCS", "public")
    assert asyncio.run(docs_codes("203.0.113.9"))["/docs"] == 200
    monkeypatch.setenv("GATEWAY_DOCS", "off")
    monkeypatch.delenv("GATEWAY_HOSTED")
    assert asyncio.run(docs_codes("127.0.0.1"))["/docs"] == 404


# ---------- secrets hors de la ligne de commande ----------

FAKE = [sys.executable, str(Path(__file__).parent / "fake_tunnel.py")]


@pytest.mark.parametrize(
    "provider,var,secret_env",
    [("ngrok", "NGROK_AUTHTOKEN", "NGROK_AUTHTOKEN"), ("cloudflare", "CLOUDFLARE_TUNNEL_TOKEN", "TUNNEL_TOKEN")],
)
def test_tunnel_secrets_go_through_environment(tmp_path, monkeypatch, provider, var, secret_env):
    secret = "secret-de-tunnel-0123456789"
    monkeypatch.setenv(var, secret)
    monkeypatch.setenv("FAKE_TUNNEL_LOG", str(tmp_path / "argv.log"))
    monkeypatch.setenv("FAKE_TUNNEL_ENV_LOG", str(tmp_path / "env.log"))

    async def scenario():
        r = RemoteAccess(port=8000, binaries={"ngrok": FAKE + ["ngrok"], "cloudflare": FAKE + ["cloudflared"]})
        await r.start(provider)
        await r.stop()

    asyncio.run(scenario())
    argv = (tmp_path / "argv.log").read_text()
    assert secret not in argv and "--authtoken" not in argv and "--token" not in argv
    assert json.loads((tmp_path / "env.log").read_text().splitlines()[0])[secret_env] == secret


def test_launcher_never_prints_or_passes_secrets():
    text = (ROOT / "lancer.ps1").read_text(encoding="utf-8-sig")
    assert "--api-key" not in text  # aucune clé en argument de commande (visible de tous)
    shown = [l for l in text.splitlines() if re.search(r"^\s*(Write-Host|Ok|Warn)\b", l)]
    assert shown and not any("$env:GATEWAY_MASTER_KEY" in l or "$testToken" in l for l in shown)


def test_origins_restriction_is_documented_as_browser_only():
    doc = (ROOT / "gateway" / "permissions.py").read_text(encoding="utf-8")
    assert "forge Origin/Referer" in doc
    assert "l'en-tête Origin se falsifie" in (ROOT / "gateway" / "web" / "admin.html").read_text(encoding="utf-8")


# ---------- limites par fournisseur : appels simultanés ----------


def test_guard_admit_counts_calls_in_flight():
    store, now = DocumentStore(":memory:"), [1000.0]
    g = ProviderGuard(store, lambda: now[0], SqliteCounters(store))
    g.set("groq", validate_rules({"rpm": 2, "tpm": 1000, "budget": {"amount": 1, "currency": "USD", "period": "day"}}))
    assert g.admit("lms", 10**6, 99.0) is None  # sans règle : rien à réserver
    a = g.admit("groq", 400, 0.4)
    b = g.admit("groq", 400, 0.4)
    with pytest.raises(RateLimited) as e:  # 2 appels en cours = rpm 2 atteint
        g.admit("groq", 10, 0.0)
    assert e.value.limit == "provider_rpm" and e.value.retry_after == 5
    assert g.saturation("groq")  # le routage auto l'évite aussi
    b.release()
    with pytest.raises(RateLimited, match="tokens/minute"):  # 400 en cours + 700 > 1000
        g.admit("groq", 700, 0.0)
    with pytest.raises(RateLimited, match="budget day"):  # 0,4 $ en cours + 0,7 $ > 1 $
        g.admit("groq", 10, 0.7)
    c = g.admit("groq", 500, 0.5)
    a.release()
    c.release()
    g.record("groq", 900, 0.5)  # usage réel enregistré
    assert g.status("groq")["rpm"]["used"] == 1
    g.admit("groq", 50, 0.1).release()
    with pytest.raises(RateLimited, match="tokens/minute"):
        g.admit("groq", 200, 0.0)
    # une requête seule plus grosse que la limite passe si rien d'autre n'est en cours
    now[0] += 120
    g.admit("groq", 5000, 0.0).release()


class SlowUpstream:
    def __init__(self):
        self.go = asyncio.Event()
        self.sent = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": {"ollama": "llama3.1:8b", "lms": "phi-4"}[host]}]})
        self.sent.append(host)
        await self.go.wait()
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )


async def parallel(rules: dict, bodies: list[dict]):
    up = SlowUpstream()
    app = create_app(
        [
            ProviderConfig("ollama", "openai", "http://ollama:11434/v1", "local"),
            ProviderConfig("lms", "openai", "http://lms:1234/v1", "local"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({ACCOUNT_KEY: "free"}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            for name, r in rules.items():
                assert (await c.put(f"/v1/admin/providers/{name}/rules", headers=MASTER, json=r)).status_code == 200
            tasks = [asyncio.create_task(c.post("/v1/chat/completions", headers=ACCOUNT, json=b)) for b in bodies]
            for _ in range(200):
                await asyncio.sleep(0.01)
                if len(up.sent) + sum(t.done() for t in tasks) >= len(bodies):
                    break
            up.go.set()
            res = await asyncio.gather(*tasks)
            usage = {p["name"]: p["usage"] for p in (await c.get("/v1/admin/providers", headers=MASTER)).json()["data"]}
            return res, up.sent, usage


def body(model, **extra):
    return {"model": model, "messages": [{"role": "user", "content": "Bonjour"}], **extra}


def test_parallel_requests_respect_provider_rpm():
    res, sent, usage = asyncio.run(parallel({"ollama": {"rpm": 2}}, [body("ollama/llama3.1:8b")] * 5))
    codes = sorted(r.status_code for r in res)
    assert codes == [200, 200, 429, 429, 429] and len(sent) == 2  # avant : 5 appels partaient
    refused = next(r for r in res if r.status_code == 429)
    assert refused.headers["Retry-After"] == "5" and "ollama" in refused.json()["error"]["message"]
    assert usage["ollama"]["rpm"]["used"] == 2  # réglés : les réservations sont rendues


def test_saturated_provider_falls_back_while_calls_are_in_flight():
    # auto : chaque IA (rpm 1) prend une requête, la 3e est refusée
    res, sent, _ = asyncio.run(parallel({"ollama": {"rpm": 1}, "lms": {"rpm": 1}}, [body("auto")] * 3))
    assert sorted(r.status_code for r in res) == [200, 200, 429]
    assert sorted(sent) == ["lms", "ollama"]
    # modèle explicite + secours : la 2e requête simultanée part sur l'IA de secours
    res, sent, _ = asyncio.run(
        parallel({"ollama": {"rpm": 1}}, [body("ollama/llama3.1:8b", fallbacks=["lms/phi-4"])] * 2)
    )
    assert [r.status_code for r in res] == [200, 200] and sorted(sent) == ["lms", "ollama"]
    used = sorted(r.json()["x_gateway"]["provider"] for r in res)
    assert used == ["lms", "ollama"]
    fb = next(r.json()["x_gateway"] for r in res if r.json()["x_gateway"]["provider"] == "lms")
    assert fb["fallback"]["attempts"][0]["status"] == 429


def test_parallel_streams_respect_provider_rpm():
    res, sent, usage = asyncio.run(parallel({"lms": {"rpm": 1}}, [body("lms/phi-4", stream=True)] * 3))
    assert sorted(r.status_code for r in res) == [200, 429, 429] and sent == ["lms"]
    assert usage["lms"]["rpm"]["used"] == 1
