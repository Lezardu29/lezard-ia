"""Travail en durée (« /travail 30 min <tâche> » dans le chat) : l'IA travaille seule pendant le temps donné et se
remet en question à chaque tour.

Un tour :
1. critique : l'IA relit la version actuelle, lui donne une note sur 10 et liste les améliorations les plus utiles
   (ou « AUCUNE » si elle ne voit plus rien d'important) ;
2. amélioration : elle réécrit la version complète en appliquant ces améliorations ;
3. comparaison : elle juge laquelle des deux versions répond le mieux à la tâche (ordre tiré au hasard, pour ne pas
   favoriser la première). La moins bonne est écartée : le travail ne recule jamais.
Fin : quand le temps est écoulé, quand l'IA ne trouve plus rien à améliorer deux fois de suite, ou sur demande (bouton
d'arrêt : la meilleure version obtenue est rendue). Le journal dit, tour par tour, ce qui a été tenté et gardé.

Chaque appel passe par la gateway elle-même (/v1/chat/completions, avec la clé de l'appelant) : même choix de modèle,
mêmes limites, même suivi que le chat. Rien n'est écrit sur le disque : le résultat est rendu dans la conversation.
Les séances sont gardées 24 h en mémoire (/travail seul retrouve la dernière, même après avoir fermé la page).
"""

from __future__ import annotations

import asyncio
import random
import re
import secrets
import time
from collections.abc import Awaitable, Callable

import httpx

MIN_SECONDS, MAX_SECONDS = 2 * 60, 8 * 3600
KEEP = 24 * 3600
MAX_RUNNING = 2
MAX_TASK = 8000
MAX_TEXT = 60_000  # une version plus longue est coupée avant d'être relue
CALL_TIMEOUT = 900  # un appel à une IA locale lente peut prendre plusieurs minutes
OVERRUN = 300  # le tour en cours peut dépasser l'heure de fin de 5 minutes au plus, puis il est abandonné
CALM_ROUNDS = 2  # « AUCUNE amélioration » deux fois de suite : le travail est fini avant l'heure
MODEL = re.compile(r"^[\w.:/@+-]{1,200}$")
# l'unité est obligatoire : « /travail 5 conseils pour… » n'est pas une séance de 5 minutes
DURATION = re.compile(
    r"^(?:(\d{1,2})\s*(?:h|heures?)\s*(?:(\d{1,2})\s*(?:min|minutes?|mn|m)?)?|(\d{1,3})\s*(?:min|minutes?|mn))(?=\s|$)",
    re.IGNORECASE,
)
THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
NOTE = re.compile(r"NOTE\s*[:：]?\s*(\d{1,2}(?:[.,]\d)?)\s*/\s*10", re.I)

SYSTEM = (
    "Tu travailles seul, sans pouvoir poser de question : fais des choix raisonnables et avance. Réponds en français "
    "si la tâche est en français."
)


class TravailError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def parse(text: str) -> tuple[int, str]:
    """« 30min écris… », « 1h30 relis… », « 2 h … », « 45 … » → (secondes, tâche)."""
    text = (text or "").strip()
    m = DURATION.match(text)
    if not m:
        raise TravailError(400, "Indiquez d'abord la durée, puis la tâche : /travail 30min écris une nouvelle sur…")
    if m.group(3) is not None:
        seconds = int(m.group(3)) * 60
    else:
        seconds = int(m.group(1)) * 3600 + int(m.group(2) or 0) * 60
    task = text[m.end() :].strip()
    if not task:
        raise TravailError(400, "Indiquez la tâche après la durée : /travail 30min écris une nouvelle sur…")
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise TravailError(400, "Durée entre 2 minutes et 8 heures.")
    if len(task) > MAX_TASK:
        raise TravailError(413, f"Tâche trop longue ({MAX_TASK} caractères au plus).")
    return seconds, task


def clean(text: str) -> str:
    """Réponse sans sa réflexion (<think>…</think>) ni les ``` qui l'entourent parfois."""
    text = THINK.sub("", text or "").strip()
    fenced = re.fullmatch(r"```[\w-]*\n(.*)\n```", text, re.S)
    return (fenced.group(1) if fenced else text).strip()


def read_critique(text: str) -> tuple[float | None, list[str], bool]:
    """(note, améliorations, plus rien à faire) d'après la critique de l'IA."""
    text = clean(text)
    m = NOTE.search(text)
    note = float(m.group(1).replace(",", ".")) if m else None
    items = []
    for line in text.splitlines():
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        if s and s != line.strip() and not NOTE.search(s):
            items.append(s[:300])
    calm = bool(re.search(r"\bAUCUNE\b", text)) and not items
    return (min(note, 10.0) if note is not None else None), items[:5], calm


def critique_prompt(task: str, current: str, tried: list[str]) -> str:
    past = "\n".join(f"- {t}" for t in tried[-8:])
    return (
        f"TÂCHE :\n{task}\n\nVERSION ACTUELLE :\n{current[:MAX_TEXT]}\n\n"
        "Relis cette version d'un œil exigeant, comme un relecteur qui veut qu'elle soit excellente. Demande-toi :\n"
        "1. Respecte-t-elle chaque consigne de la tâche (longueur, format, public, ton) ? Une version trop longue se "
        "raccourcit : améliorer ne veut pas dire ajouter.\n"
        "2. Chaque affirmation est-elle exacte ? Ce qui est douteux ou inventé doit être corrigé ou retiré.\n"
        "3. Qu'est-ce qui est flou, répété, mal organisé, ou qu'un expert ferait mieux ?\n"
        + (f"Améliorations déjà faites (ne les propose pas de nouveau) :\n{past}\n" if past else "")
        + "Réponds exactement sous cette forme :\nNOTE : <note>/10\n- <amélioration la plus utile>\n- <suivante>\n"
        "(3 améliorations au plus, concrètes). Si tu ne vois plus rien d'important à améliorer, réponds seulement :\n"
        "NOTE : <note>/10\nAUCUNE"
    )


def improve_prompt(task: str, current: str, items: list[str]) -> str:
    todo = "\n".join(f"- {i}" for i in items)
    return (
        f"TÂCHE :\n{task}\n\nVERSION ACTUELLE :\n{current[:MAX_TEXT]}\n\nAMÉLIORATIONS À APPLIQUER :\n{todo}\n\n"
        "Réécris la version COMPLÈTE en appliquant ces améliorations, sans rien perdre de ce qui était bon, en "
        "respectant toujours les consignes de la tâche (longueur comprise) et sans rien affirmer d'inexact. "
        "Donne seulement la nouvelle version, sans commentaire avant ni après."
    )


def compare_prompt(task: str, a: str, b: str) -> str:
    return (
        f"TÂCHE :\n{task}\n\nVERSION A :\n{a[: MAX_TEXT // 2]}\n\nVERSION B :\n{b[: MAX_TEXT // 2]}\n\n"
        "Laquelle répond le mieux à la tâche ? Juge dans cet ordre : respect des consignes (longueur, format, public), "
        "exactitude (aucune affirmation fausse ou inventée), puis clarté. Une version plus longue n'est pas meilleure "
        "pour autant. Réponds seulement par A ou B."
    )


def pick(text: str) -> str | None:
    m = re.search(r"\b([AB])\b", clean(text))
    return m.group(1) if m else None


# (consigne système, demande, rapide) → réponse ; rapide : réponse courte (relecture, comparaison), peu de réflexion
Ask = Callable[[str, str, bool], Awaitable[str]]


def gateway_ask(port: int, key: str, model: str | None, private: bool = False) -> Ask:
    """Appel de la gateway elle-même, comme le chat : modèle choisi (ou choix automatique), clé de l'appelant ;
    conversation en mode privé : IA locales seulement, comme dans le chat. Choix automatique : l'IA retenue pour la
    première version sert toute la séance (sinon une relecture, plus longue, partait vers une IA bien plus lente)."""
    chosen = {"ref": model}

    async def ask(system: str, prompt: str, quick: bool = False) -> str:
        body = {
            "model": chosen["ref"] or "auto",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        if private:
            body["private"] = True
        if quick:  # une IA qui réfléchit (gemma4, qwen3…) mettait plusieurs minutes pour une note et trois lignes
            body["reasoning_effort"] = "low"
        try:
            async with httpx.AsyncClient(timeout=CALL_TIMEOUT, trust_env=False) as client:
                r = await client.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {key}"},
                )
        except httpx.HTTPError as e:
            raise TravailError(502, f"L'IA ne répond pas ({type(e).__name__}).") from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            message = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
            raise TravailError(r.status_code, message or f"L'IA a répondu par une erreur {r.status_code}.")
        try:
            text = data["choices"][0]["message"].get("content") or ""
            if not chosen["ref"]:
                ref = ((data.get("x_gateway") or {}).get("routing") or {}).get("ref")
                if isinstance(ref, str) and MODEL.match(ref):
                    chosen["ref"] = ref
            return text
        except (KeyError, IndexError, TypeError, AttributeError):
            raise TravailError(502, "Réponse illisible de l'IA.") from None

    return ask


class Sessions:
    """Séances en cours ou terminées, propres à chaque compte (identité de l'appelant)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.runs: dict[str, dict] = {}
        self.clock = clock

    def _prune(self) -> None:
        now = time.time()
        for rid in [r for r, v in self.runs.items() if v.get("ended") and now - v["ended"] > KEEP]:
            self.runs.pop(rid, None)

    def get(self, rid: str, owner: str) -> dict:
        run = self.runs.get(rid)
        if not run or run["owner"] != owner:
            raise TravailError(404, "Séance introuvable (terminée depuis plus de 24 heures ?).")
        return run

    def last(self, owner: str) -> dict:
        mine = [r for r in self.runs.values() if r["owner"] == owner]
        if not mine:
            raise TravailError(
                404, "Aucune séance de travail ces dernières 24 heures. Lancez-en une : /travail 30min …"
            )
        return max(mine, key=lambda r: r["started"])

    def stop(self, rid: str, owner: str) -> dict:
        run = self.get(rid, owner)
        if run["status"] == "running" and run.get("task"):
            run["stopping"] = True
            run["task"].cancel()
        return run

    def start(self, *, owner: str, text: str, ask: Ask, model: str | None = None) -> dict:
        self._prune()
        seconds, task = parse(text)
        if sum(1 for r in self.runs.values() if r["status"] == "running") >= MAX_RUNNING:
            raise TravailError(429, "Deux séances de travail tournent déjà : attendez qu'une se termine ou arrêtez-la.")
        rid = secrets.token_hex(8)
        run = {
            "id": rid,
            "owner": owner,
            "task": None,
            "goal": task,
            "seconds": seconds,
            "status": "running",
            "started": time.time(),
            "ended": None,
            "step": "Première version…",
            "round": 0,
            "note": None,
            "best": "",
            "journal": [],
            "error": None,
            "model": model,
            "reason": None,
        }
        self.runs[rid] = run
        run["task"] = asyncio.get_running_loop().create_task(self._run(run, ask, seconds))
        return run

    async def _run(self, run: dict, ask: Ask, seconds: int) -> None:
        deadline = self.clock() + seconds
        try:
            await asyncio.wait_for(self._work(run, ask, deadline), timeout=seconds + OVERRUN)
        except TimeoutError:
            run["reason"] = "temps écoulé"
        except asyncio.CancelledError:
            run["reason"] = "arrêtée à votre demande"
        except TravailError as e:
            if not run["best"]:
                run.update(status="error", error=e.message)
            else:
                run["reason"] = f"interrompue : {e.message}"
        finally:
            if run["status"] == "running":
                run["status"] = "done" if run["best"] else "error"
                if not run["best"] and not run["error"]:
                    run["error"] = "Arrêtée avant d'avoir une première version."
            run["step"] = None
            run["ended"] = time.time()

    async def _work(self, run: dict, ask: Ask, deadline: float) -> None:
        task = run["goal"]
        first = clean(await ask(SYSTEM, f"TÂCHE :\n{task}\n\nFais une première version complète."))
        if not first:
            raise TravailError(502, "L'IA a rendu une première version vide.")
        run["best"] = first
        run["journal"].append({"round": 0, "what": "Première version", "kept": True})
        tried: list[str] = []
        calm = 0
        while self.clock() < deadline:
            run["round"] += 1
            n = run["round"]
            run["step"] = f"Tour {n} : relecture critique…"
            note, items, nothing = read_critique(await ask(SYSTEM, critique_prompt(task, run["best"], tried), True))
            if note is not None:
                run["note"] = note
            if nothing or not items:
                calm += 1
                run["journal"].append({"round": n, "what": "Plus rien d'important à améliorer", "note": note})
                if calm >= CALM_ROUNDS:
                    run["reason"] = "l'IA ne trouve plus rien à améliorer"
                    return
                continue
            calm = 0
            if self.clock() >= deadline:
                break
            run["step"] = f"Tour {n} : amélioration ({len(items)} point{'s' if len(items) > 1 else ''})…"
            candidate = clean(await ask(SYSTEM, improve_prompt(task, run["best"], items)))
            kept = False
            # une réécriture vide, ou qui perd plus de la moitié du texte, est un recul : jamais gardée
            if candidate and len(candidate) >= 0.5 * len(run["best"]) and candidate != run["best"]:
                if self.clock() >= deadline:
                    break
                run["step"] = f"Tour {n} : comparaison des deux versions…"
                swap = random.random() < 0.5  # noqa: S311 - ordre de présentation, pas de la sécurité
                a, b = (candidate, run["best"]) if swap else (run["best"], candidate)
                choice = pick(await ask(SYSTEM, compare_prompt(task, a, b), True))
                kept = choice == ("A" if swap else "B")
            if kept:
                run["best"] = candidate
            tried.extend(items)
            what = "; ".join(i if len(i) <= 160 else i[:157] + "…" for i in items)
            run["journal"].append({"round": n, "what": what, "note": note, "kept": kept})
        run["reason"] = "temps écoulé"


def public(run: dict, full: bool = True) -> dict:
    """Ce que voit la page. Le texte n'est envoyé qu'une fois la séance finie (ou à la demande, `full`)."""
    end = run.get("ended") or time.time()
    out = {
        "id": run["id"],
        "goal": run["goal"][:200],
        "status": run["status"],
        "step": run.get("step"),
        "round": run["round"],
        "note": run.get("note"),
        "seconds": round(end - run["started"]),
        "planned": run["seconds"],
        "journal": run["journal"][-50:],
        "error": run.get("error"),
        "reason": run.get("reason"),
        "kept": sum(1 for j in run["journal"] if j.get("kept") and j["round"] > 0),
    }
    if full or run["status"] != "running":
        out["text"] = run["best"]
    return out


def report(run: dict) -> str:
    """Résultat affiché dans la conversation : la meilleure version, puis le journal du travail."""
    p = public(run)
    minutes = max(1, round(p["seconds"] / 60))
    lines = [
        p["text"],
        "",
        "---",
        f"**Travail de {minutes} min** · {p['round']} tour{'s' if p['round'] > 1 else ''} de relecture · "
        f"{p['kept']} amélioration{'s' if p['kept'] > 1 else ''} gardée{'s' if p['kept'] > 1 else ''}"
        + (f" · dernière note de l'IA : {p['note']:g}/10" if p["note"] is not None else "")
        + (f" · fin : {p['reason']}" if p["reason"] else ""),
    ]
    rounds = [j for j in p["journal"] if j["round"] > 0]
    if rounds:
        lines += ["", "**Journal des tours**", ""]
        for j in rounds:
            mark = "gardée" if j.get("kept") else ("rien à changer" if "kept" not in j else "écartée (pas meilleure)")
            note = f" (note {j['note']:g}/10)" if j.get("note") is not None else ""
            lines.append(f"- Tour {j['round']}{note} : {j['what']} → {mark}")
    return "\n".join(lines)
