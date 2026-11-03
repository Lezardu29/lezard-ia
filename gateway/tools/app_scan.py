"""Inventaire des applications de ce PC (Windows) pour le guide des applications du contrôle du PC (app_guides.py) :
installées (menu Démarrer, programmes, applications du Store), utilisées (compteur de lancements et dernier usage
tenus par Windows, UserAssist) et ouvertes en ce moment (processus avec une fenêtre, sans leur titre).

Rien ne quitte le PC : `python -m gateway.tools.app_scan` écrit data/pc_apps_scan.json. Une IA (Claude Opus, par
exemple) en tire ensuite les guides de data/pc_apps.json pour les applications vraiment utilisées.
"""

from __future__ import annotations

import codecs
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

USERASSIST = r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
EPOCH_DIFF = 11644473600  # secondes entre 1601 (FILETIME) et 1970
# dossiers connus de Windows (UserAssist les note par GUID)
KNOWN_FOLDERS = {
    "{6D809377-6AF0-444B-8957-A3773F02200E}": "%ProgramFiles%",
    "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}": "%ProgramFiles(x86)%",
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}": "%SystemRoot%\\System32",
    "{F38BF404-1D43-42F2-9305-67DE0B28FC23}": "%SystemRoot%",
    "{0139D44E-6AFE-49F2-8690-3DAFCAE6FFB8}": "%ProgramData%\\Microsoft\\Windows\\Start Menu\\Programs",
    "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}": "%AppData%\\Microsoft\\Windows\\Start Menu\\Programs",
    "{9E3995AB-1F9C-4F13-B827-48B24B6C7174}": "%AppData%\\Microsoft\\Internet Explorer\\Quick Launch\\User Pinned",
}
SKIP = ("uninstall", "désinstall", "setup", "update", "helper", "crash", "redistributable", "runtime", "sdk", "driver")


def _filetime(ft: int) -> float | None:
    t = ft / 10_000_000 - EPOCH_DIFF
    return t if 0 < t < 4_102_444_800 else None  # avant 2100 : sinon valeur abîmée ou absente


def usage() -> list[dict]:
    """Programmes lancés par l'utilisateur : nombre de lancements, temps au premier plan, dernier lancement."""
    import winreg

    out = []
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, USERASSIST) as root:
        for i in range(winreg.QueryInfoKey(root)[0]):
            guid = winreg.EnumKey(root, i)
            try:
                key = winreg.OpenKey(root, guid + r"\Count")
            except OSError:
                continue
            with key:
                for j in range(winreg.QueryInfoKey(key)[1]):
                    name, data, _ = winreg.EnumValue(key, j)
                    if not isinstance(data, bytes) or len(data) < 68:
                        continue
                    path = codecs.decode(name, "rot13")
                    for folder, real in KNOWN_FOLDERS.items():
                        path = path.replace(folder, real)
                    runs, focus_count, focus_ms = struct.unpack_from("<III", data, 4)
                    last = _filetime(struct.unpack_from("<Q", data, 60)[0])
                    if runs or focus_ms:
                        out.append(
                            {
                                "path": path,
                                "runs": runs,
                                "focus_minutes": round(focus_ms / 60000),
                                "last_used": time.strftime("%Y-%m-%d", time.localtime(last)) if last else None,
                            }
                        )
    merged: dict[str, dict] = {}
    for u in out:  # même programme vu par deux GUID (exécutable et raccourci) : on garde le plus d'usage
        k = u["path"].lower()
        if k not in merged or u["runs"] + u["focus_minutes"] > merged[k]["runs"] + merged[k]["focus_minutes"]:
            merged[k] = u
    return sorted(merged.values(), key=lambda u: (u["focus_minutes"], u["runs"]), reverse=True)


def installed() -> list[dict]:
    """Programmes installés (panneau « Applications »), sans les composants techniques."""
    import winreg

    seen, out = set(), []
    for hive, flags in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY), (winreg.HKEY_LOCAL_MACHINE,
                        winreg.KEY_WOW64_32KEY), (winreg.HKEY_CURRENT_USER, 0)):  # fmt: skip
        try:
            root = winreg.OpenKey(hive, UNINSTALL, 0, winreg.KEY_READ | flags)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                        values = {winreg.EnumValue(k, j)[0]: winreg.EnumValue(k, j)[1]
                                  for j in range(winreg.QueryInfoKey(k)[1])}  # fmt: skip
                except OSError:
                    continue
                name = str(values.get("DisplayName") or "").strip()
                if not name or values.get("SystemComponent") == 1 or values.get("ParentKeyName"):
                    continue
                if any(s in name.lower() for s in SKIP) or name.lower() in seen:
                    continue
                seen.add(name.lower())
                out.append({"name": name, "publisher": str(values.get("Publisher") or "").strip() or None,
                            "location": str(values.get("InstallLocation") or "").strip() or None})  # fmt: skip
    return sorted(out, key=lambda a: a["name"].lower())


def store_apps() -> list[str]:
    """Applications du Microsoft Store installées pour cet utilisateur (sans les cadres et composants)."""
    cmd = ("Get-AppxPackage | Where-Object { -not $_.IsFramework -and $_.SignatureKind -ne 'System' } | "
           "ForEach-Object { $_.Name }")  # fmt: skip
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({line.strip() for line in r.stdout.splitlines() if line.strip()})


def running() -> list[str]:
    """Programmes ouverts avec une fenêtre en ce moment (noms de processus seulement, jamais les titres)."""
    cmd = "Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } | ForEach-Object { $_.ProcessName }"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({line.strip() for line in r.stdout.splitlines() if line.strip()})


def scan() -> dict:
    return {
        "scanned_at": time.strftime("%Y-%m-%d %H:%M"),
        "usage": usage(),
        "running": running(),
        "installed": installed(),
        "store": store_apps(),
    }


def main(argv=None) -> int:
    if os.name != "nt":
        print("Inventaire des applications : Windows seulement.", file=sys.stderr)
        return 1
    out = Path(os.getenv("GATEWAY_DATA_DIR", "data")) / "pc_apps_scan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = scan()
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(data['usage'])} programmes utilisés, {len(data['installed'])} installés, "
          f"{len(data['running'])} ouverts : {out}")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
