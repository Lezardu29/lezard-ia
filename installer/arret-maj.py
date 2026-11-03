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
        say("service non lancé : rien à arrêter")
        return 0
    pids = [p for p in (state.get("pid"), state.get("gateway")) if isinstance(p, int) and p > 0]
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
    left = [pid for pid in pids if is_python(pid)]
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
