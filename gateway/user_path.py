"""Retire un dossier du PATH de l'utilisateur Windows : python user_path.py --remove <dossier>

Étape de désinstallation d'une recette (installers.py) : claude-mem ajoute son dossier de commandes au PATH et ne
l'en retire pas en partant. Seule l'entrée exacte est retirée (variables comme %LOCALAPPDATA% comprises), le reste du
PATH est gardé tel quel. Lancé comme un script : aucune dépendance au paquet gateway.
"""

import ctypes
import os
import sys


def same(entry: str, folder: str) -> bool:
    norm = lambda p: os.path.normcase(os.path.normpath(os.path.expandvars(p.strip())))  # noqa: E731
    return bool(entry.strip()) and norm(entry) == norm(folder)


def remove(folder: str) -> int:
    if sys.platform != "win32":
        return 0
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            value, kind = winreg.QueryValueEx(k, "Path")
        except OSError:
            print("Déjà retiré.")
            return 0
        entries = value.split(";")
        kept = [e for e in entries if not same(e, folder)]
        if len(kept) == len(entries):
            print("Déjà retiré.")
            return 0
        winreg.SetValueEx(k, "Path", 0, kind, ";".join(kept))
    # les programmes ouverts ensuite (Explorateur, terminaux) voient le nouveau PATH
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)
    print(f"Retiré du PATH : {folder}")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] != ["--remove"] or len(sys.argv) != 3:
        print("usage : user_path.py --remove <dossier>", file=sys.stderr)
        sys.exit(2)
    sys.exit(remove(os.path.expandvars(sys.argv[2])))
