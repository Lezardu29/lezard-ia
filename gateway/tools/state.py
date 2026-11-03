"""État d'une tâche sur le PC, gardé hors du modèle (24.2) : objectif, plan en sous-étapes, actions déjà faites
(résumées), nombre d'étapes et durée. Rappelé au modèle à chaque tour dans un seul message, remplacé au tour suivant
(comme la capture) : les petits modèles perdent le fil d'un long historique d'appels d'outils.

Planificateur : avant la première action, le modèle découpe l'objectif en 2 à 6 sous-étapes (un appel sans outils,
réponse JSON). Plan illisible ou vide (modèle « pensant » qui ne répond rien) : la tâche continue sans plan.
Limites : TOOLS_MAX_STEPS tours et TOOLS_PC_MAX_SECONDS secondes par tâche ; au-delà, le modèle est appelé une
dernière fois sans outils pour dire où il en est.
"""

from __future__ import annotations

import json
import os
import re
import time

# ce que le modèle relit d'une action : pas les numéros d'éléments ni les cases, propres à une seule capture
_POSITION = ("reason", "element", "zoom", "cell", "x", "y")


def describe(name: str, args: dict) -> str:
    """Action lisible d'un tour à l'autre : le nom, les arguments utiles et la raison donnée par le modèle."""
    detail = {k: v for k, v in args.items() if k not in _POSITION}
    text = name + (" " + json.dumps(detail, ensure_ascii=False) if detail else "")
    reason = " ".join(str(args.get("reason") or "").split())
    return text + (f" ({reason[:120]})" if reason else "")


MAX_SECONDS = int(os.getenv("TOOLS_PC_MAX_SECONDS", "600"))
PLANNING = os.getenv("TOOLS_PC_PLAN", "true").lower() in ("1", "true", "yes")
HISTORY = 8  # dernières actions rappelées au modèle
MAX_PLAN = 6

PLAN_PROMPT = (
    "Tu prépares une tâche à faire sur un PC Windows, à la souris et au clavier. Découpe l'objectif de "
    f"l'utilisateur en 2 à {MAX_PLAN} sous-étapes courtes et concrètes (ex. « ouvrir le bloc-notes », « écrire le "
    "texte », « enregistrer »), dans l'ordre. N'agis pas encore. Réponds UNIQUEMENT par un objet JSON, sans autre "
    'texte : {"etapes": ["…", "…"]}'
)


def _clean(text: str, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def plan_messages(goal: str) -> list[dict]:
    return [{"role": "system", "content": PLAN_PROMPT}, {"role": "user", "content": f"Objectif : {goal}"}]


def parse_plan(resp: dict) -> list[str]:
    """Sous-étapes lues dans la réponse du planificateur ; [] si illisible."""
    text = (((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return []
    steps = data.get("etapes") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        return []
    return [s for s in (_clean(x, 120) for x in steps if isinstance(x, str)) if s][:MAX_PLAN]


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0][:120]


class TaskState:
    def __init__(
        self, goal: str, max_steps: int, max_seconds: int = MAX_SECONDS, plan: bool = PLANNING, clock=time.monotonic
    ):
        self.goal = _clean(goal, 500) or "(objectif non précisé)"
        self.max_steps, self.max_seconds, self.clock = max_steps, max_seconds, clock
        self.plan_wanted = plan  # planificateur appelé avant la première action
        self.started = clock()
        self.plan: list[str] = []
        self.history: list[str] = []
        self.actions = 0  # actions sur le PC tentées (réussies ou non)
        self.steps = 0  # tours du modèle

    def elapsed(self) -> int:
        return int(self.clock() - self.started)

    def expired(self) -> bool:
        return self.max_seconds > 0 and self.elapsed() >= self.max_seconds

    def record(self, name: str, args: dict, outcome: str) -> None:
        """Action sur le PC terminée : « ok », « sans effet » ou le message d'erreur."""
        self.actions += 1
        self.history.append(f"{describe(name, args)} → {_first_sentence(outcome)}")

    def render(self) -> str:
        lines = [f"État de la tâche (tour {self.steps + 1} sur {self.max_steps}, {self.elapsed()} s écoulées"]
        lines[0] += f" sur {self.max_seconds} s)." if self.max_seconds > 0 else ")."
        lines.append(f"Objectif : {self.goal}")
        if self.plan:
            lines.append("Plan :")
            lines += [f"{i}. {s}" for i, s in enumerate(self.plan, 1)]
        if self.history:
            older = len(self.history) - HISTORY
            lines.append(
                "Actions déjà faites"
                + (f" (les {HISTORY} dernières sur {len(self.history)})" if older > 0 else "")
                + " :"
            )
            lines += [f"- {h}" for h in self.history[-HISTORY:]]
        else:
            lines.append("Aucune action faite pour l'instant.")
        lines.append(
            "Repère où tu en es dans le plan d'après les actions faites et la capture, puis fais l'étape suivante. "
            "Objectif atteint : réponds sans appeler d'outil."
        )
        return "\n".join(lines)
