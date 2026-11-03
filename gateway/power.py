"""Priorité et bridage d'énergie des processus de ce PC (mode économie des IA locales).

- Windows : classe de priorité (SetPriorityClass) et EcoQoS (SetProcessInformation, ProcessPowerThrottling : cœurs
  économes, fréquence basse) ; processus trouvés par Toolhelp32. Sans droits administrateur : les processus de
  l'utilisateur seulement (Ollama lancé par lui).
- Linux : « nice » (os.setpriority) ; processus trouvés dans /proc.
- Ailleurs, ou en cas de refus : sans effet (False), jamais d'erreur.
Ne touche ni à la carte graphique (le pilote ne le permet pas sans droits administrateur) ni à un autre utilisateur.
"""

from __future__ import annotations

import os
import sys

BELOW_NORMAL = 0x4000  # BELOW_NORMAL_PRIORITY_CLASS
NORMAL = 0x20
NICE_LOW = 10
_THROTTLING = 4  # ProcessPowerThrottling
_EXECUTION_SPEED = 0x1


def _win():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return ctypes, wintypes, k32


def _open(pid: int | None):
    ctypes, wintypes, k32 = _win()
    if pid is None or pid == os.getpid():
        return k32, k32.GetCurrentProcess(), False
    h = k32.OpenProcess(0x0200 | 0x1000, False, pid)  # SET_INFORMATION | QUERY_LIMITED_INFORMATION
    return k32, h, True


def set_power_throttling(on: bool | None, pid: int | None = None) -> bool:
    """Windows : EcoQoS imposé (True), refusé (False) ou laissé à Windows (None) pour ce processus ou `pid`."""
    if sys.platform != "win32":
        return False
    try:
        ctypes, wintypes, _ = _win()

        class State(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

        state = State(1, 0 if on is None else _EXECUTION_SPEED, _EXECUTION_SPEED if on else 0)
        k32, h, close = _open(pid)
        if not h:
            return False
        try:
            return bool(k32.SetProcessInformation(h, _THROTTLING, ctypes.byref(state), ctypes.sizeof(state)))
        finally:
            if close:
                k32.CloseHandle(h)
    except (OSError, AttributeError):  # Windows trop ancien
        return False


def get_priority(pid: int) -> int | None:
    """Classe de priorité (Windows) ou « nice » (Linux) actuelle ; None si illisible."""
    try:
        if sys.platform == "win32":
            k32, h, close = _open(pid)
            if not h:
                return None
            try:
                return k32.GetPriorityClass(h) or None
            finally:
                if close:
                    k32.CloseHandle(h)
        return os.getpriority(os.PRIO_PROCESS, pid)
    except (OSError, AttributeError):
        return None


def set_priority(pid: int, value: int) -> bool:
    """Windows : classe de priorité (BELOW_NORMAL, NORMAL…) ; Linux : valeur « nice »."""
    try:
        if sys.platform == "win32":
            k32, h, close = _open(pid)
            if not h:
                return False
            try:
                return bool(k32.SetPriorityClass(h, value))
            finally:
                if close:
                    k32.CloseHandle(h)
        os.setpriority(os.PRIO_PROCESS, pid, value)
        return True
    except (OSError, AttributeError, PermissionError):  # autre utilisateur, nice plus bas interdit sans droits
        return False


def low_value() -> int:
    return BELOW_NORMAL if sys.platform == "win32" else NICE_LOW


def find_processes(names: set[str]) -> list[int]:
    """PID des processus de ce PC dont l'exécutable s'appelle `names` (sans tenir compte de la casse)."""
    wanted = {n.lower() for n in names}
    if sys.platform == "win32":
        return _win_processes(wanted)
    out = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for e in entries:
        if not e.isdigit():
            continue
        try:
            with open(f"/proc/{e}/comm", encoding="utf-8") as f:
                if f.read().strip().lower() in wanted:
                    out.append(int(e))
        except OSError:
            continue
    return out


def _win_processes(wanted: set[str]) -> list[int]:
    try:
        ctypes, wintypes, k32 = _win()

        class Entry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            return []
        out = []
        try:
            e = Entry()
            e.dwSize = ctypes.sizeof(Entry)
            ok = k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                if e.szExeFile.lower() in wanted:
                    out.append(int(e.th32ProcessID))
                ok = k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            k32.CloseHandle(snap)
        return out
    except (OSError, AttributeError):
        return []
