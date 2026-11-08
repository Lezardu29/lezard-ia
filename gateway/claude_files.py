"""Retire un dossier écrit par un outil dans celui de Claude Code : python claude_files.py --remove commands/sc

Étape de désinstallation d'une recette (installers.py) : SuperClaude copie ses commandes dans ~/.claude/commands/sc
et n'a pas de commande pour les retirer. Seul un dossier situé DANS ~/.claude est accepté. Lancé comme un script :
aucune dépendance au paquet gateway.
"""

import shutil
import sys
from pathlib import Path


def remove(relative: str) -> int:
    root = (Path.home() / ".claude").resolve()
    target = (root / relative).resolve()
    if target == root or not target.is_relative_to(root):
        print(f"dossier refusé : {relative}")
        return 2
    if not target.exists():
        print("Déjà retiré.")
        return 0
    shutil.rmtree(target)
    print(f"Dossier supprimé : {target}")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] != ["--remove"] or len(sys.argv) != 3:
        print("usage : claude_files.py --remove <dossier dans ~/.claude>", file=sys.stderr)
        sys.exit(2)
    sys.exit(remove(sys.argv[2]))
