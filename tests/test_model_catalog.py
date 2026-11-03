"""Recommandation de modèles locaux selon le matériel (13.2) et contrôle du disque avant téléchargement (14.4)."""

import json
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from gateway import hardware
from gateway import core
from gateway.api import admin as admin_api
from gateway.config import ProviderConfig
from gateway.hardware import HardwareScanner
from gateway.main import create_app
from gateway.model_catalog import CATALOG, recommend
from gateway.plans import PlanResolver
from gateway.registry import infer_local
from gateway.router import CODE_MODELS
from gateway.store import DocumentStore

GIB = 1024**3
MASTER, ACCOUNT = {"Authorization": "Bearer master"}, {"Authorization": "Bearer compte"}


def hw(vram_total=None, vram_free=None, ram_available=16 * GIB, disk_free=500 * GIB, models_vram=0):
    gpus = (
        [{"name": "GPU test", "vram_total": vram_total, "vram_used": vram_total - vram_free, "vram_free": vram_free}]
        if vram_total
        else []
    )
    return {
        "gpus": gpus,
        "ram": {"total": 32 * GIB, "available": ram_available},
        "disk": {"free": disk_free},
        "models_vram": models_vram,
    }


def fits(r):
    return {m["model"]: m["fit"] for m in r["models"]}


def test_12_gb_vram_runs_7b_and_14b_in_q4():
    r = recommend(hw(12 * GIB, 11.5 * GIB), context=4096)
    f = fits(r)
    assert f["qwen2.5:7b"] == f["llama3.1:8b"] == f["qwen2.5:14b"] == f["qwen2.5-coder:14b"] == "vram"
    assert f["qwen2.5:32b"] == "offload" and f["llama3.3:70b"] == "too_big"
    assert r["best"] == {
        "chat": "qwen2.5:14b",
        "code": "qwen2.5-coder:14b",
        "vision": "gemma3:12b",
        "reasoning": "deepseek-r1:14b",
        "embeddings": "bge-m3",
    }  # 16.1


def test_under_8_gb_vram_runs_3b_and_7b_only():
    f = fits(recommend(hw(8 * GIB, 7 * GIB), context=4096))
    assert f["llama3.2:3b"] == f["qwen2.5:7b"] == f["qwen2.5-coder:7b"] == "vram"
    assert f["qwen2.5:14b"] == "offload"


def test_open_applications_do_not_downgrade_but_are_reported():
    busy = recommend(hw(12 * GIB, 3 * GIB), context=4096)  # 9 Gio pris par un jeu ouvert
    loaded = recommend(hw(12 * GIB, 3 * GIB, models_vram=8 * GIB), context=4096)  # dont 8 par des modèles chargés
    assert fits(busy)["qwen2.5:14b"] == fits(loaded)["qwen2.5:14b"] == "vram"  # capacité du PC
    assert busy["budget"]["vram_busy"] == 9 * GIB and loaded["budget"]["vram_busy"] == GIB


def test_larger_context_needs_more_memory():
    small, big = recommend(hw(12 * GIB, 11.5 * GIB), context=4096), recommend(hw(12 * GIB, 11.5 * GIB), context=65536)
    assert fits(small)["qwen2.5:14b"] == "vram" and fits(big)["qwen2.5:14b"] != "vram"


def test_without_gpu_prefers_the_smallest_model_in_ram():
    r = recommend(hw(ram_available=8 * GIB), context=4096)
    assert r["budget"]["gpu"] is None and "vram" not in fits(r).values()
    assert r["best"]["chat"] == "llama3.2:3b" and r["best"]["code"] == "qwen2.5-coder:7b"
    assert fits(r)["qwen2.5:32b"] == "too_big"
    assert all(m["vllm"] is None or not m["vllm"]["fits_vram"] for m in r["models"])


def test_installed_and_disk_flags():
    r = recommend(hw(12 * GIB, 11 * GIB, disk_free=6 * GIB), ["phi3.5:latest", "qwen2.5:7b"], context=4096)
    by = {m["model"]: m for m in r["models"]}
    assert by["phi3.5"]["installed"] and by["qwen2.5:7b"]["installed"] and not by["llama3.1:8b"]["installed"]
    assert by["llama3.1:8b"]["disk_ok"] and not by["qwen2.5:14b"]["disk_ok"]


def test_catalog_names_feed_specialty_routing():
    for m in CATALOG:
        info = infer_local("ollama", m.model)
        if "vision" in m.uses:
            assert info.vision, m.model
        if "code" in m.uses:
            assert any(k in m.model for k in CODE_MODELS), m.model
        if "reasoning" in m.uses:
            assert info.reasoning, m.model


class Upstream:
    def __init__(self):
        self.pulls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if path == "/api/pull":
            self.pulls.append((request.url.host, json.loads(request.content)["model"]))
            return httpx.Response(200, content=b'{"status":"success"}\n')
        return httpx.Response(404)


def make(up, monkeypatch, disk_free):
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    monkeypatch.setattr(hardware.shutil, "which", lambda name: None)
    nvml = SimpleNamespace(
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: None,
        nvmlDeviceGetCount=lambda: 1,
        nvmlDeviceGetHandleByIndex=lambda i: i,
        nvmlDeviceGetName=lambda h: "RTX test",
        nvmlDeviceGetMemoryInfo=lambda h: SimpleNamespace(total=12 * GIB, used=GIB // 2, free=12 * GIB - GIB // 2),
        nvmlDeviceGetUtilizationRates=lambda h: SimpleNamespace(gpu=0),
    )
    monkeypatch.setattr(
        core, "HardwareScanner", lambda: HardwareScanner(nvml_loader=lambda: nvml, psutil_loader=lambda: None)
    )
    monkeypatch.setattr(
        hardware,
        "disk_info",
        lambda path=None: {"path": "/modeles", "exists": True, "total": 1000 * GIB, "used": 0, "free": disk_free},
    )
    monkeypatch.setattr(admin_api, "disk_info", lambda: {"path": "/modeles", "free": disk_free})
    return create_app(
        [
            ProviderConfig("ollama", "openai", "http://localhost:11434/v1", "local"),
            ProviderConfig("ollama-salon", "openai", "http://192.168.1.20:11434/v1", "local"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({"compte": "free"}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )


def test_recommendations_route(monkeypatch):
    with TestClient(make(Upstream(), monkeypatch, 500 * GIB)) as c:
        assert c.get("/v1/admin/models/recommendations", headers=ACCOUNT).status_code == 403
        r = c.get("/v1/admin/models/recommendations", headers=MASTER)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ollama"] == "ollama" and data["budget"]["gpu"] == "RTX test"
        assert data["best"]["code"] == "qwen2.5-coder:14b"
        assert {m["model"]: m for m in data["models"]}["llama3.1:8b"]["installed"]


def test_pull_refused_when_disk_is_too_small(monkeypatch):
    up = Upstream()
    with TestClient(make(up, monkeypatch, 3 * GIB)) as c:
        r = c.post("/v1/admin/ollama/pull", headers=MASTER, json={"model": "qwen2.5:14b"})
        assert r.status_code == 507 and "Espace disque insuffisant" in r.text
        r = c.post("/v1/admin/ollama/pull", headers=MASTER, json={"model": "inconnu:1b"})
        assert r.status_code == 507 and "taille du modèle inconnue" in r.text
        assert up.pulls == []
        # Ollama d'une autre machine : son disque n'est pas celui de ce PC
        r = c.post("/v1/admin/ollama/pull", headers=MASTER, json={"model": "qwen2.5:14b", "provider": "ollama-salon"})
        assert r.status_code == 200 and up.pulls == [("192.168.1.20", "qwen2.5:14b")]
        r = c.post("/v1/admin/ollama/pull", headers=MASTER, json={"model": "llama3.2:3b"})
        assert r.status_code == 200 and ("localhost", "llama3.2:3b") in up.pulls
