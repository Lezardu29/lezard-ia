"""Scanner matériel (13.1) : NVML simulé, absent, en erreur ; repli nvidia-smi ; route réservée à la clé maître."""

import asyncio
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import hardware
from gateway import core
from gateway.config import ProviderConfig
from gateway.hardware import HardwareScanner, gpu_info
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

GIB = 1024**3
MASTER, ACCOUNT = {"Authorization": "Bearer master"}, {"Authorization": "Bearer compte"}


class NVMLError(Exception):
    pass


def fake_nvml(fail: bool = False):
    calls = []

    def init():
        calls.append("init")
        if fail:
            raise NVMLError("Driver Not Loaded")

    h = object()
    return SimpleNamespace(
        calls=calls,
        nvmlInit=init,
        nvmlShutdown=lambda: calls.append("shutdown"),
        nvmlDeviceGetCount=lambda: 1,
        nvmlDeviceGetHandleByIndex=lambda i: h,
        nvmlDeviceGetName=lambda _: b"NVIDIA GeForce RTX 4070",  # anciennes versions : bytes
        nvmlDeviceGetMemoryInfo=lambda _: SimpleNamespace(total=12 * GIB, used=3 * GIB, free=9 * GIB),
        nvmlDeviceGetUtilizationRates=lambda _: SimpleNamespace(gpu=37),
    )


def smi_run(stdout: str, code: int = 0):
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="erreur" if code else "")

    run.seen = seen
    return run


@pytest.fixture
def no_smi(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda name: None)
    monkeypatch.setattr(hardware, "other_gpus", lambda: [])


@pytest.fixture
def with_smi(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(hardware, "other_gpus", lambda: [])  # cartes AMD / Intel de la machine des tests : à part


def test_nvml_simulated(no_smi):
    nvml = fake_nvml()
    g = gpu_info(nvml)
    assert g["source"] == "nvml" and g["error"] is None
    assert g["gpus"] == [
        {
            "index": 0,
            "name": "NVIDIA GeForce RTX 4070",
            "vram_total": 12 * GIB,
            "vram_used": 3 * GIB,
            "vram_free": 9 * GIB,
            "utilization_pct": 37,
            "vendor": "nvidia",
            "kind": "dedicated",
            "source": "nvml",
        }
    ]
    assert nvml.calls == ["init", "shutdown"]


def test_nvml_absent_without_smi_is_not_an_error(no_smi):
    assert gpu_info(None) == {"gpus": [], "source": None, "error": None}  # AMD, Apple, conteneur


def test_nvml_error_falls_back_to_smi(with_smi):
    run = smi_run("NVIDIA GeForce RTX 3060, 12288, 1024, 11264, 5\nTesla T4, 15360, [N/A], [N/A], [N/A]\n")
    g = gpu_info(fake_nvml(fail=True), run)
    assert g["source"] == "nvidia-smi" and g["error"] is None
    assert g["gpus"][0] == {
        "index": 0,
        "name": "NVIDIA GeForce RTX 3060",
        "vram_total": 12 * GIB,
        "vram_used": GIB,
        "vram_free": 11 * GIB,
        "utilization_pct": 5,
        "vendor": "nvidia",
        "kind": "dedicated",
        "source": "nvidia-smi",
    }
    assert g["gpus"][1]["vram_used"] is None and g["gpus"][1]["utilization_pct"] is None
    assert "--format=csv,noheader,nounits" in run.seen[0]


def test_nvml_error_without_smi_reports_reason(no_smi):
    nvml = fake_nvml(fail=True)
    g = gpu_info(nvml)
    assert g["gpus"] == [] and g["source"] is None and "Driver Not Loaded" in g["error"]
    assert nvml.calls == ["init"]  # rien à refermer si l'ouverture a échoué


def test_smi_failure_is_reported(with_smi):
    g = gpu_info(None, smi_run("", code=9))
    assert g["gpus"] == [] and "nvidia-smi" in g["error"]  # pas d'exception remontée


def test_without_optional_libraries_ram_and_disk_only(no_smi, tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "absent" / "models"))
    snap = asyncio.run(
        HardwareScanner(nvml_loader=lambda: None, psutil_loader=lambda: None).snapshot([], httpx.AsyncClient())
    )
    assert snap["gpus"] == [] and snap["gpu_error"] is None
    assert snap["libraries"] == {"nvml": False, "psutil": False}
    assert snap["ram"]["total"] > 0 and snap["cpu"]["usage_pct"] is None and snap["cpu"]["cores_logical"]
    assert snap["disk"]["exists"] is False and snap["disk"]["free"] > 0  # disque du parent existant


def test_psutil_used_when_present(no_smi):
    ps = SimpleNamespace(
        cpu_count=lambda logical=True: 8,
        cpu_percent=lambda interval=None: 12.5,
        virtual_memory=lambda: SimpleNamespace(total=32 * GIB, available=20 * GIB),
    )
    snap = asyncio.run(
        HardwareScanner(nvml_loader=lambda: None, psutil_loader=lambda: ps).snapshot([], httpx.AsyncClient())
    )
    assert snap["cpu"]["cores_physical"] == 8 and snap["cpu"]["usage_pct"] == 12.5
    assert snap["ram"] == {"total": 32 * GIB, "available": 20 * GIB, "used": 12 * GIB}


class Upstream:
    def __init__(self):
        self.ps_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
        if path == "/api/ps":
            self.ps_calls += 1
            size = 6 * GIB if host == "localhost" else 2 * GIB
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "llama3.1:8b",
                            "size": size,
                            "size_vram": size,
                            "context_length": 8192,
                            "expires_at": "2026-10-19T12:00:00Z",
                        }
                    ]
                },
            )
        return httpx.Response(404)


def test_hardware_route_master_only_cached_with_loaded_models(no_smi, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        core,
        "HardwareScanner",
        lambda: HardwareScanner(clock=lambda: clock[0], nvml_loader=fake_nvml, psutil_loader=lambda: None),
    )
    up = Upstream()
    app = create_app(
        [
            ProviderConfig("ollama", "openai", "http://localhost:11434/v1", "local"),
            ProviderConfig("ollama-salon", "openai", "http://192.168.1.20:11434/v1", "local"),
        ],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({"compte": "free"}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        assert c.get("/v1/admin/hardware", headers=ACCOUNT).status_code == 403
        assert c.get("/v1/admin/hardware").status_code in (401, 403)
        r = c.get("/v1/admin/hardware", headers=MASTER)
        assert r.status_code == 200, r.text
        snap = r.json()
        assert snap["gpus"][0]["vram_total"] == 12 * GIB and snap["gpu_source"] == "nvml"
        by = {m["provider"]: m for m in snap["loaded_models"]}
        assert by["ollama"]["this_machine"] and not by["ollama-salon"]["this_machine"]
        assert snap["models_vram"] == 6 * GIB  # la VRAM d'une autre machine ne compte pas
        calls = up.ps_calls
        c.get("/v1/admin/hardware", headers=MASTER)
        assert up.ps_calls == calls  # cache de quelques secondes
        clock[0] += 10
        c.get("/v1/admin/hardware", headers=MASTER)
        assert up.ps_calls == calls + 2
