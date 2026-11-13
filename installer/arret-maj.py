"""Arrêt de Lezard IA avant une mise à jour, par l'installateur (PrepareToInstall), SANS arrêter l'installateur.

Une mise à jour lancée depuis le tableau de bord (bandeau « Mettre à jour ») est un descendant de la gateway. Or
« lezard.py stop » arrête la gateway avec tout son arbre (taskkill /T) : l'installateur était arrêté avec elle, sans
rien installer ni relancer Lezard IA. Ici, chaque processus est arrêté seul : d'abord le service (sa fenêtre
« Lezard IA », qui relancerait la gateway), puis la gateway. L'installateur, qui n'est ni l'un ni l'autre, continue :
il remplace les fichiers et relance Lezard IA. Le tunnel éventuel (cloudflared) est arrêté au démarrage suivant (son
numéro est gardé dans data\\cloudflared.pid).

Lancé avec le Python de l'installation : python.exe arret-maj.py <dossier de Lezard IA>. Ce fichier est fourni par
l'installateur (il ne dépend pas de la version installée, qui peut être ancienne). Journal : data\\logs\\arret-maj.log.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

# Windows, sans programme externe (tasklist, taskkill) : leur sortie est vide quand l'installateur lance ce script
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
QUERY, TERMINATE, STILL_ACTIVE = 0x1000, 0x0001, 259  # PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE


def is_python(pid: int) -> bool:
    """Ce numéro est-il un Python encore en marche (et pas un autre programme qui aurait repris le numéro) ?"""
    h = kernel32.OpenProcess(QUERY, False, pid)
    if not h:
        return False
    try:
        code, size, name = wintypes.DWORD(), wintypes.DWORD(1024), ctypes.create_unicode_buffer(1024)
        if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return False
        if not kernel32.QueryFullProcessImageNameW(h, 0, name, ctypes.byref(size)):
            return False
        return Path(name.value).name.lower() in ("python.exe", "pythonw.exe")
    finally:
        kernel32.CloseHandle(h)


def stop_one(pid: int) -> str:
    """Ce processus seul : jamais son arbre (il comprend l'installateur)."""
    h = kernel32.OpenProcess(TERMINATE, False, pid)
    if not h:
        return f"ouverture refusée (erreur {ctypes.get_last_error()})"
    try:
        ok = kernel32.TerminateProcess(h, 1)
        return "arrêté" if ok else f"arrêt refusé (erreur {ctypes.get_last_error()})"
    finally:
        kernel32.CloseHandle(h)


class _Entry(ctypes.Structure):  # PROCESSENTRY32W
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def all_pids() -> list[int]:
    """Numéros de tous les processus (instantané de Windows, sans programme externe)."""
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Entry))
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    try:
        entry, found = _Entry(), []
        entry.dwSize = ctypes.sizeof(_Entry)
        more = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while more:
            found.append(int(entry.th32ProcessID))
            more = kernel32.Process32NextW(snap, ctypes.byref(entry))
        return found
    finally:
        kernel32.CloseHandle(snap)


def program(pid: int) -> str:
    """Chemin du programme de ce processus s'il est encore en marche, sinon « »."""
    h = kernel32.OpenProcess(QUERY, False, pid)
    if not h:
        return ""
    try:
        code, size, name = wintypes.DWORD(), wintypes.DWORD(1024), ctypes.create_unicode_buffer(1024)
        if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return ""
        return name.value if kernel32.QueryFullProcessImageNameW(h, 0, name, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(h)


def others(app: Path) -> list[int]:
    """Programmes encore lancés avec le Python de l'installation (son dossier python), sauf ce script : le serveur
    MCP « lezard » ou « orchestre » que Claude Code garde ouvert, un outil du chat… Ils tiennent les fichiers de ce
    dossier ouverts : l'installateur ne pouvait pas les remplacer, abandonnait, et Lezard IA restait arrêté (1.0.23)."""
    root = os.path.normcase(str(app / "python")) + os.sep
    return [pid for pid in all_pids() if pid != os.getpid() and os.path.normcase(program(pid)).startswith(root)]


def main(app: Path) -> int:
    data = app / "data"
    logs = data / "logs"

    def say(text: str) -> None:
        """À l'écran et dans data\\logs\\arret-maj.log : ce qui s'est passé, si une mise à jour échoue."""
        print(text)
        try:
            logs.mkdir(parents=True, exist_ok=True)
            with open(logs / "arret-maj.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
        except OSError:
            pass

    try:
        state = json.loads((data / "launcher.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
        say("service non lancé")
    pids = [p for p in (state.get("pid"), state.get("gateway")) if isinstance(p, int) and p > 0]
    if state:
        say(f"arrêt pour la mise à jour : service {state.get('pid')}, gateway {state.get('gateway')}")
    for pid in pids:  # le service d'abord : sinon il relancerait la gateway arrêtée
        if is_python(pid):
            say(f"  {pid} : arrêt…")
            say(f"  {pid} : {stop_one(pid)}")
    for _ in range(30):  # fichiers libérés (python\, gateway\) avant la copie
        if not any(is_python(pid) for pid in pids):
            break
        time.sleep(0.5)
    (data / "launcher.stop").unlink(missing_ok=True)
    # le reste : serveur MCP gardé ouvert par Claude Code, outil du chat… lancés avec le Python de l'installation
    for pid in others(app):
        say(f"  {pid} : autre programme lancé avec le Python de Lezard IA, arrêt…")
        say(f"  {pid} : {stop_one(pid)}")
    for _ in range(20):
        if not others(app):
            break
        time.sleep(0.5)
    left = [pid for pid in pids if is_python(pid)] + others(app)
    say("Lezard IA arrêté pour la mise à jour" if not left else f"toujours en cours : {left}")
    return 0 if not left else 1


if __name__ == "__main__":
    try:
        sys.exit(main(Path(sys.argv[1])))
    except Exception:  # jamais muet : l'erreur dans le journal, pour comprendre une mise à jour ratée
        import traceback

        with open(Path(sys.argv[1]) / "data" / "logs" / "arret-maj.log", "a", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        raise
