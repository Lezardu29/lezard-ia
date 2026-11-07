"""Cartes graphiques de tous les fabricants (AMD, Intel, NVIDIA), et distinction carte dédiée / partie graphique
intégrée au processeur.

Avant, seules les cartes NVIDIA étaient vues (NVML, nvidia-smi) : une Radeon RX 9070 XT passait pour « pas de carte
graphique » et les modèles étaient conseillés pour le processeur. Ici :
- Windows : DXGI (l'API graphique de Windows, sans programme ni paquet à installer) donne chaque carte avec son
  fabricant, sa mémoire DÉDIÉE exacte (64 bits : 16 Go pour une RX 9070 XT, là où Win32_VideoController plafonne à
  4 Go) et la mémoire utilisée (IDXGIAdapter3::QueryVideoMemoryInfo, Windows 10 et plus) ;
- Linux : pilote amdgpu (/sys/class/drm/card*/device/mem_info_vram_total et _used).
NVIDIA reste lue par NVML ou nvidia-smi (hardware.py), qui donnent aussi l'utilisation : DXGI n'ajoute que les autres.

Partie graphique intégrée (« AMD Radeon(TM) Graphics » d'un Ryzen, Intel UHD / Iris, Radeon 780M…) : elle partage la
RAM du PC, n'a presque pas de mémoire dédiée et calcule bien moins vite qu'une carte. Elle est listée (« integrated »)
mais jamais retenue pour placer un modèle : seule la carte dédiée compte (model_catalog.budget).
"""

from __future__ import annotations

import ctypes
import os
import re
from pathlib import Path

GIB = 1024**3
VENDORS = {0x10DE: "nvidia", 0x1002: "amd", 0x1022: "amd", 0x8086: "intel", 0x1414: "microsoft", 0x5143: "qualcomm"}
# en dessous : mémoire réservée dans la RAM par le BIOS pour une partie graphique intégrée, pas une carte
DEDICATED_MIN = 2 * GIB
# parties graphiques intégrées, d'après leur nom (certaines APU reçoivent du BIOS plusieurs Go « dédiés »)
_INTEGRATED = re.compile(
    r"radeon\(tm\) graphics|radeon graphics|radeon \d{3,4}m\b|radeon 8\d{2,3}s|radeon vega|vega \d+ graphics"
    r"|intel\(r\) (uhd|hd|iris)|\b(uhd|iris)\b|arc\(tm\) graphics|intel\(r\) graphics|adreno",
    re.I,
)
# cartes dédiées malgré un nom proche : Radeon RX / PRO W, Intel Arc A / B (A770, B580…)
_DEDICATED = re.compile(r"\brx\s?\d|radeon pro|\bw\d{4}\b|instinct|\barc(\(tm\))? [ab]\d{3}", re.I)


def kind_of(vendor: str, name: str, dedicated: int | None) -> str:
    """« dedicated » (carte graphique) ou « integrated » (partie graphique du processeur)."""
    if vendor == "nvidia":
        return "dedicated"
    if _DEDICATED.search(name or ""):
        return "dedicated"
    if _INTEGRATED.search(name or "") or vendor in ("microsoft", "qualcomm"):
        return "integrated"
    return "dedicated" if (dedicated or 0) >= DEDICATED_MIN else "integrated"


def entry(index: int, name: str, vendor: str, total: int | None, used: int | None, source: str) -> dict:
    free = total - used if total is not None and used is not None else None
    return {
        "index": index,
        "name": name,
        "vendor": vendor,
        "kind": kind_of(vendor, name, total),
        "vram_total": total,
        "vram_used": used,
        "vram_free": free,
        "utilization_pct": None,
        "source": source,
    }


# ---------- Windows : DXGI ----------


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> _GUID:
        h = text.replace("-", "")
        return cls(
            int(h[0:8], 16),
            int(h[8:12], 16),
            int(h[12:16], 16),
            (ctypes.c_ubyte * 8).from_buffer_copy(bytes.fromhex(h[16:])),
        )


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class _AdapterDesc1(ctypes.Structure):
    _fields_ = [
        ("Description", ctypes.c_wchar * 128),
        ("VendorId", ctypes.c_uint),
        ("DeviceId", ctypes.c_uint),
        ("SubSysId", ctypes.c_uint),
        ("Revision", ctypes.c_uint),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", ctypes.c_uint),
    ]


class _VideoMemoryInfo(ctypes.Structure):
    _fields_ = [
        ("Budget", ctypes.c_uint64),
        ("CurrentUsage", ctypes.c_uint64),
        ("AvailableForReservation", ctypes.c_uint64),
        ("CurrentReservation", ctypes.c_uint64),
    ]


IID_FACTORY1 = "770aae78-f26f-4dba-a829-253c83d1b387"
IID_ADAPTER3 = "645967a4-1392-4310-a798-8053ce3e93fd"
_NOT_FOUND = 0x887A0002 - (1 << 32)  # DXGI_ERROR_NOT_FOUND : plus de carte à énumérer (HRESULT signé)
_SOFTWARE = 2  # DXGI_ADAPTER_FLAG_SOFTWARE : « Microsoft Basic Render Driver », pas une carte


def _method(
    obj, index: int, *argtypes
):  # pragma: no cover - Windows seulement : testé par la CI Windows (test_real_dxgi_…)
    """Méthode n° `index` de la table virtuelle d'un objet COM."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])


def _release(obj) -> None:  # pragma: no cover - Windows seulement : testé par la CI Windows (test_real_dxgi_…)
    if obj:
        _method(obj, 2)(obj)  # IUnknown::Release


def dxgi_adapters() -> list[dict]:  # pragma: no cover - Windows seulement : testé par la CI Windows (test_real_dxgi_…)
    """Cartes vues par DXGI : {name, vendor_id, dedicated, used, flags} ; [] hors Windows ou si DXGI échoue."""
    if os.name != "nt":
        return []
    try:
        dxgi = ctypes.WinDLL("dxgi")
    except OSError:
        return []
    factory = ctypes.c_void_p()
    if dxgi.CreateDXGIFactory1(ctypes.byref(_GUID.parse(IID_FACTORY1)), ctypes.byref(factory)) != 0:
        return []
    out = []
    try:
        for i in range(16):
            adapter = ctypes.c_void_p()
            hr = _method(factory, 12, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(factory, i, ctypes.byref(adapter))
            if hr == _NOT_FOUND or hr != 0:  # IDXGIFactory1::EnumAdapters1
                break
            try:
                desc = _AdapterDesc1()
                if _method(adapter, 10, ctypes.POINTER(_AdapterDesc1))(adapter, ctypes.byref(desc)) != 0:  # GetDesc1
                    continue
                used = None
                adapter3 = ctypes.c_void_p()
                iid = _GUID.parse(IID_ADAPTER3)
                if (
                    _method(adapter, 0, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
                        adapter, ctypes.byref(iid), ctypes.byref(adapter3)
                    )
                    == 0
                ):  # QueryInterface(IDXGIAdapter3) : Windows 10 et plus
                    try:
                        info = _VideoMemoryInfo()
                        if (
                            _method(adapter3, 14, ctypes.c_uint, ctypes.c_int, ctypes.POINTER(_VideoMemoryInfo))(
                                adapter3, 0, 0, ctypes.byref(info)
                            )
                            == 0
                        ):  # QueryVideoMemoryInfo(nœud 0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL)
                            used = int(info.CurrentUsage)
                    finally:
                        _release(adapter3)
                out.append(
                    {
                        "name": desc.Description.strip(),
                        "vendor_id": desc.VendorId,
                        "dedicated": int(desc.DedicatedVideoMemory),
                        "used": used,
                        "flags": desc.Flags,
                    }
                )
            finally:
                _release(adapter)
    finally:
        _release(factory)
    return out


def from_dxgi(adapters: list[dict]) -> list[dict]:
    """Cartes physiques (pas le rendu logiciel de Windows), une fois chacune (même nom et même mémoire : un seul GPU
    vu par plusieurs sorties)."""
    seen, out = set(), []
    for a in adapters:
        if a["flags"] & _SOFTWARE or VENDORS.get(a["vendor_id"]) == "microsoft":
            continue  # rendu logiciel, écran virtuel (Hyper-V, bureau à distance) : pas une carte
        key = (a["name"], a["vendor_id"], a["dedicated"])
        if key in seen:
            continue
        seen.add(key)
        vendor = VENDORS.get(a["vendor_id"], "other")
        out.append(entry(len(out), a["name"], vendor, a["dedicated"] or None, a["used"], "dxgi"))
    return out


# ---------- Linux : amdgpu ----------


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None


def sysfs_gpus(root: Path = Path("/sys/class/drm")) -> list[dict]:
    """Cartes AMD et Intel vues par le noyau Linux (NVIDIA : NVML). [] hors Linux."""
    if not root.is_dir():
        return []
    out = []
    for card in sorted(root.glob("card[0-9]*")):
        if "-" in card.name:  # card0-HDMI-A-1 : une sortie, pas une carte
            continue
        dev = card / "device"
        try:
            vendor_id = int(_read(dev / "vendor") or "0", 16)
        except ValueError:
            continue
        vendor = VENDORS.get(vendor_id, "other")
        if vendor in ("nvidia", "other"):
            continue
        total, used = _read(dev / "mem_info_vram_total"), _read(dev / "mem_info_vram_used")
        total = int(total) if total and total.isdigit() else None
        used = int(used) if used and used.isdigit() else None
        device_id = (_read(dev / "device") or "").removeprefix("0x")
        name = _read(dev / "product_name") or f"{'AMD Radeon' if vendor == 'amd' else 'Intel'} (PCI {device_id})"
        out.append(entry(len(out), name, vendor, total, used, "amdgpu" if vendor == "amd" else "sysfs"))
    return out


def other_gpus() -> list[dict]:
    """Cartes autres que NVIDIA de ce PC, avec leur type (dédiée / intégrée) ; jamais d'exception."""
    try:
        found = from_dxgi(dxgi_adapters()) if os.name == "nt" else sysfs_gpus()
    except Exception:  # pilote ou API inattendus : on garde au moins NVIDIA, la RAM et le processeur
        return []
    return [g for g in found if g["vendor"] != "nvidia"]


def usable(gpus: list[dict]) -> list[dict]:
    """Cartes qui comptent pour placer un modèle : les dédiées (une partie graphique intégrée partage la RAM et
    calcule bien moins vite). Une ancienne entrée sans « kind » (NVIDIA d'avant) est une carte dédiée."""
    return [g for g in gpus if g.get("kind", "dedicated") == "dedicated" and g.get("vram_total")]
