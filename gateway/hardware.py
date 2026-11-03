"""Scanner matériel du PC qui héberge la gateway (13.1) : GPU, CPU, RAM, disque des modèles, modèles chargés.

Réservé à la clé maître (14.3) : modèle de GPU et mémoire décrivent la machine du propriétaire.
Dépendances FACULTATIVES, absentes de requirements.txt :
- nvidia-ml-py (module `pynvml`) : GPU NVIDIA ; à défaut, ou s'il échoue, `nvidia-smi` s'il est installé.
- psutil : charge CPU, cœurs physiques ; à défaut, RAM lue par le système (Windows, /proc/meminfo, sysconf).
Sans GPU NVIDIA (AMD, Apple, conteneur) : RAM et disque seulement, sans erreur.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .local_context import looks_ollama, server_root

TTL = 5.0  # secondes de cache : le widget (13.3) interroge toutes les 5 s
MIB = 1024 * 1024
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


def load_nvml():
    try:
        import pynvml  # fourni par le paquet nvidia-ml-py
    except ImportError:
        return None
    return pynvml


def load_psutil():
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def _text(v) -> str:
    return v.decode(errors="replace") if isinstance(v, bytes) else str(v)


def nvml_gpus(nvml) -> list[dict]:
    """GPU via NVML ; lève l'erreur de NVML (pilote absent, GPU perdu…) pour le repli sur nvidia-smi."""
    nvml.nvmlInit()
    try:
        out = []
        for i in range(nvml.nvmlDeviceGetCount()):
            h = nvml.nvmlDeviceGetHandleByIndex(i)
            mem = nvml.nvmlDeviceGetMemoryInfo(h)
            try:
                util = nvml.nvmlDeviceGetUtilizationRates(h).gpu
            except Exception:  # non pris en charge par certains GPU
                util = None
            out.append(
                {
                    "index": i,
                    "name": _text(nvml.nvmlDeviceGetName(h)),
                    "vram_total": int(mem.total),
                    "vram_used": int(mem.used),
                    "vram_free": int(mem.free),
                    "utilization_pct": util,
                }
            )
        return out
    finally:
        try:
            nvml.nvmlShutdown()
        except Exception:
            pass


def smi_gpus(run=subprocess.run) -> list[dict] | None:
    """GPU via nvidia-smi (valeurs en Mio) ; None si l'outil est absent."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    r = run(
        [exe, "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=5,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "nvidia-smi a échoué").strip()[:200])
    num = lambda s: int(float(s)) if s.replace(".", "", 1).isdigit() else None  # « [N/A] » → None  # noqa: E731
    mib = lambda s: num(s) * MIB if num(s) is not None else None  # noqa: E731
    out = []
    for i, line in enumerate(l for l in r.stdout.splitlines() if l.strip()):
        name, total, used, free, util = (c.strip() for c in (line.split(",") + [""] * 5)[:5])
        out.append(
            {
                "index": i,
                "name": name,
                "vram_total": mib(total),
                "vram_used": mib(used),
                "vram_free": mib(free),
                "utilization_pct": num(util),
            }
        )
    return out


def gpu_info(nvml=None, run=subprocess.run) -> dict:
    """{gpus, source, error} : NVML d'abord, nvidia-smi ensuite ; jamais d'exception."""
    errors = []
    if nvml is not None:
        try:
            return {"gpus": nvml_gpus(nvml), "source": "nvml", "error": None}
        except Exception as e:
            errors.append(f"NVML : {_text(e) or type(e).__name__}")
    try:
        gpus = smi_gpus(run)
        if gpus is not None:
            return {"gpus": gpus, "source": "nvidia-smi", "error": None}
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as e:
        errors.append(f"nvidia-smi : {e}")
    return {"gpus": [], "source": None, "error": " ; ".join(errors) or None}


def _ram_fallback() -> dict:
    """RAM sans psutil : API Windows, /proc/meminfo (Linux, conteneurs), sinon sysconf (total seulement)."""
    if os.name == "nt":

        class MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MemStatus()
        st.dwLength = ctypes.sizeof(MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return {"total": st.ullTotalPhys, "available": st.ullAvailPhys}
        return {"total": None, "available": None}
    try:
        info = {}
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0]) * 1024
        return {"total": info.get("MemTotal"), "available": info.get("MemAvailable", info.get("MemFree"))}
    except (OSError, ValueError, IndexError):
        pass
    try:
        return {"total": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"), "available": None}
    except (ValueError, OSError, AttributeError):
        return {"total": None, "available": None}


def cpu_ram_info(ps=None, first: bool = False) -> tuple[dict, dict]:
    cpu = {
        "model": platform.processor() or platform.machine() or None,
        "cores_logical": os.cpu_count(),
        "cores_physical": None,
        "usage_pct": None,
    }
    if ps is not None:
        try:
            cpu["cores_physical"] = ps.cpu_count(logical=False)
            # charge depuis l'appel précédent (non bloquant) ; le tout premier appel de psutil renvoie 0 : mesure courte
            cpu["usage_pct"] = ps.cpu_percent(interval=0.2 if first else None)
            vm = ps.virtual_memory()
            ram = {"total": int(vm.total), "available": int(vm.available)}
        except Exception:
            ram = _ram_fallback()
    else:
        ram = _ram_fallback()
    ram["used"] = ram["total"] - ram["available"] if ram["total"] and ram["available"] is not None else None
    return cpu, ram


def models_dir() -> Path:
    """Dossier des modèles Ollama : OLLAMA_MODELS, sinon le défaut d'Ollama (~/.ollama/models)."""
    raw = os.getenv("OLLAMA_MODELS", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".ollama" / "models"


def disk_info(path: Path | None = None) -> dict:
    """Espace du disque qui contient le dossier des modèles (le dossier le plus proche qui existe)."""
    target = path or models_dir()
    probe = target
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        u = shutil.disk_usage(probe)
    except OSError:
        return {"path": str(target), "exists": False, "total": None, "used": None, "free": None}
    return {"path": str(target), "exists": target.exists(), "total": u.total, "used": u.used, "free": u.free}


def is_this_machine(base_url: str) -> bool:
    return (urlsplit(base_url).hostname or "").lower() in LOCAL_HOSTS


async def loaded_models(providers, client: httpx.AsyncClient) -> list[dict]:
    """Modèles chargés en mémoire par les serveurs Ollama (/api/ps). `this_machine` : leur VRAM est celle de ce PC
    (un Ollama d'une autre machine du réseau occupe la sienne)."""
    targets = [
        p
        for p in providers
        if p.cfg.enabled and p.hosting == "local" and not p.cfg.owner and looks_ollama(p.name, p.cfg.base_url)
    ]

    async def ps(p) -> list[dict]:
        key = p.cfg.resolved_key()
        try:
            r = await client.get(
                f"{server_root(p.cfg.base_url)}/api/ps",
                timeout=2,
                headers={"Authorization": f"Bearer {key}"} if key else None,
            )
            items = (r.json().get("models") or []) if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError, AttributeError):
            return []
        local = is_this_machine(p.cfg.base_url)
        return [
            {
                "provider": p.name,
                "model": m.get("name") or m.get("model"),
                "size": m.get("size"),
                "size_vram": m.get("size_vram"),
                "context_length": m.get("context_length"),
                "expires_at": m.get("expires_at"),
                "this_machine": local,
            }
            for m in items
            if isinstance(m, dict)
        ]

    return [m for group in await asyncio.gather(*(ps(p) for p in targets)) for m in group]


class HardwareScanner:
    """État matériel mis en cache TTL secondes ; un seul relevé à la fois."""

    def __init__(
        self,
        ttl: float = TTL,
        clock=time.monotonic,
        nvml_loader=load_nvml,
        psutil_loader=load_psutil,
        run=subprocess.run,
    ):
        self.ttl, self.clock, self.run = ttl, clock, run
        self.nvml_loader, self.psutil_loader = nvml_loader, psutil_loader
        self._cached: dict | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    def _system(self) -> dict:
        nvml, ps = self.nvml_loader(), self.psutil_loader()
        cpu, ram = cpu_ram_info(ps, first=self._cached is None)
        return {
            "gpu": gpu_info(nvml, self.run),
            "cpu": cpu,
            "ram": ram,
            "disk": disk_info(),
            "libraries": {"nvml": nvml is not None, "psutil": ps is not None},
        }

    async def snapshot(self, providers, client: httpx.AsyncClient) -> dict:
        async with self._lock:
            if self._cached is not None and self.clock() - self._at < self.ttl:
                return self._cached
            system, models = await asyncio.gather(asyncio.to_thread(self._system), loaded_models(providers, client))
            gpu = system.pop("gpu")
            self._cached = {
                "gpus": gpu["gpus"],
                "gpu_source": gpu["source"],
                "gpu_error": gpu["error"],
                **system,
                "loaded_models": models,
                "models_vram": sum(
                    m["size_vram"] for m in models if m["this_machine"] and isinstance(m["size_vram"], int)
                ),
                "scanned_at": time.time(),
            }
            self._at = self.clock()
            return self._cached
