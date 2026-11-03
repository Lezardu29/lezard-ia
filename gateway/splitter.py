"""Découpage des messages trop longs pour les IA locales (13.4) : map-reduce.

Quand le message dépasse la fenêtre MESURÉE (10.6) du modèle local et qu'aucun modèle autorisé n'a une fenêtre
suffisante, le dernier message de l'utilisateur est découpé aux limites naturelles (sections Markdown,
paragraphes, phrases, fonctions de code) avec les découpeurs de gateway/documents.py, sans LangChain, tiktoken
ni BlingFire (14.5) :
1. map : chaque morceau est traité avec la consigne de la demande (début et fin du message, où elle se trouve
   en général), en parallèle sur plusieurs serveurs locaux, sinon à la suite (un serveur local traite souvent une
   requête à la fois) ;
2. condensation, si les notes ne tiennent pas ensemble dans la fenêtre : elles sont regroupées et résumées ;
3. reduce : le modèle choisi rédige la réponse finale à partir des notes, avec l'historique de la conversation.
Chaque appel passe par les limites du fournisseur (admit, 9.6) et compte dans les quotas de la clé (finish).
Une demande qui exige une vue d'ensemble (relecture croisée de code, comparaison de deux passages éloignés) perd
en qualité : c'est signalé dans x_gateway (`split.notice`) et dans le badge du chat.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from collections.abc import AsyncIterator, Callable

from .classifier import CHARS_PER_TOKEN, Classification, _text_and_images, estimate_tokens
from .documents import CODE_BOUNDARY, _atoms, _chunks_code, _merge
from .limits import RateLimited
from .orchestrator import _agent, _call, _failed, _system_text, _text
from .providers.base import ProviderError
from .registry import ModelInfo, usd_eur_rate
from .router import RoutingError, route
from .validation import RequestError

if TYPE_CHECKING:
    from .core import Gateway, Selection

SPLIT_VALUES = ("auto", "off")
# phrases avant lignes : un texte coupé à 80 colonnes ne doit pas être coupé au milieu d'une phrase
SEPARATORS = ["\n\n", ".\n", "!\n", "?\n", ". ", "? ", "! ", "\n", "; ", ", ", " "]
SAFETY = 0.85  # tokens estimés (4 caractères) contre tokens réels du modèle : marge
PROMPT_TOKENS = 220  # consignes du map / du reduce, en tokens
MIN_CHUNK_TOKENS = 200
NOTICE = (
    "Message trop long pour la fenêtre du modèle local : découpé en {n} parties lues séparément, puis "
    "synthétisées. Une demande qui exige une vue d'ensemble (relecture croisée de code, comparaison de "
    "passages éloignés) perd en qualité ; un modèle à plus grande fenêtre la traiterait en une fois."
)

NOTICE_TRIM = (
    "Conversation trop longue pour la fenêtre du modèle local : ses {k} plus anciens messages n'ont pas été "
    "transmis pour cette réponse."
)

# {consigne} : début et fin du long message, précédés de la question posée ensuite s'il n'est pas le dernier
MAP_PROMPT = (
    "Un message de l'utilisateur est trop long pour être lu d'un seul tenant : il est découpé en {n} parties, "
    "lues séparément. {consigne}\n\n"
    "Tu reçois la partie {i} sur {n}. Traite-la selon cette demande : relève de façon complète et concise tout ce "
    "qui y répond (faits, chiffres, citations courtes, éléments de code utiles), sans introduction ni conclusion. "
    "Si la partie ne contient rien d'utile pour la demande, réponds seulement « (rien) »."
)
CONDENSE_PROMPT = (
    "Voici des notes prises sur plusieurs parties d'un long message de l'utilisateur. {consigne}\n\n"
    "Condense ces notes sans rien perdre d'utile pour la demande (faits, chiffres, citations, code), sans "
    "introduction ni conclusion."
)
REDUCE_PROMPT = (
    "Mon message précédent était trop long pour toi : il a été lu en {n} parties, séparément. Début et fin du "
    "message (la demande s'y trouve en général) :\n{excerpt}\n\n"
    "Notes prises sur chaque partie, dans l'ordre du message :\n\n{notes}\n\n"
    "Réponds maintenant à ma demande à partir de ces notes, comme si tu avais lu le message en entier, sans "
    "mentionner ce découpage."
)
PLACEHOLDER = (
    "[Message trop long pour être transmis tel quel : il a été lu en {n} parties, voir les notes plus "
    "loin. Début et fin :\n{excerpt}]"
)
REDUCE_LATER = (
    "{demand}\n\n---\nNotes prises sur chaque partie de mon long message, dans l'ordre :\n\n{notes}\n\n"
    "Réponds à ma demande ci-dessus à partir de ces notes, comme si tu avais lu le long message en entier, sans "
    "mentionner ce découpage."
)


def max_parts() -> int:
    try:
        return max(2, int(os.getenv("SPLIT_MAX_PARTS", "24")))
    except ValueError:
        return 24


def normalize(value) -> str:
    v = str(value or "auto").strip().lower()
    if v not in SPLIT_VALUES:
        raise RequestError(f"'split' inconnu : '{value}'. Valeurs : auto, off.")
    return v


@dataclass
class SplitPlan:
    chunks: list[str]  # morceaux du dernier message utilisateur, dans l'ordre
    workers: list[ModelInfo]  # le modèle choisi, puis un modèle par autre serveur local
    window: int  # plus petite fenêtre mesurée des workers
    chunk_tokens: int
    map_tokens: int  # max_tokens de chaque appel map
    notes_budget: int  # place des notes dans l'appel reduce
    excerpt: str
    index: int  # position du message découpé (le plus long de l'utilisateur)
    last: int = -1  # position de la dernière question (= index si c'est elle, la plus longue)
    demand: str = ""  # extrait de la dernière question, quand ce n'est pas le message découpé
    trimmed: list[int] = field(default_factory=list)  # anciens messages retirés de la synthèse, faute de place
    agents: list[dict] = field(default_factory=list)

    @property
    def single(self) -> bool:
        """Le long message tient en un morceau : c'est l'historique qui déborde, seule la synthèse est appelée."""
        return len(self.chunks) == 1

    @property
    def consigne(self) -> str:
        if self.demand:
            return (
                f"Demande de l'utilisateur, posée après ce long message :\n{self.demand}\n\n"
                f"Début et fin du long message :\n{self.excerpt}"
            )
        return f"Voici le début et la fin du message, où se trouve en général sa demande :\n{self.excerpt}"

    def estimate(self, out_tokens: int) -> int:
        """Tokens consommés au plus (réservation des quotas, 10.4) : maps + reduce."""
        n = 0 if self.single else len(self.chunks)
        consigne = estimate_tokens(self.consigne)
        maps = sum(estimate_tokens(c) for c in self.chunks) * (n > 0) + n * (PROMPT_TOKENS + consigne + self.map_tokens)
        return maps + self.window + out_tokens  # synthèse : au plus la fenêtre

    def to_dict(self) -> dict:
        refs = list(dict.fromkeys(w.ref for w in self.workers))
        notice = [] if self.single else [NOTICE.format(n=len(self.chunks))]
        if self.trimmed:
            notice.append(NOTICE_TRIM.format(k=len(self.trimmed)))
        return {
            "parts": len(self.chunks),
            "window": self.window,
            "chunk_tokens": self.chunk_tokens,
            "workers": refs,
            "parallel": len(refs) > 1 and not self.single,
            "trimmed_messages": len(self.trimmed),
            "notice": " ".join(notice),
        }


# ---------- découpage aux limites naturelles ----------

FENCE = re.compile(r"```([\w+#.-]*)[^\n]*\n(.*?)```", re.DOTALL)
HEADING = re.compile(r"(?m)^(?=#{1,6} \S)")
LONE_HEADING = re.compile(r"#{1,6} [^\n]*\s*")
PROSE_END = re.compile(r"[.!?»…:]\s*$")


def looks_code(text: str) -> bool:
    """Code collé sans balises ``` : au moins 3 définitions de premier niveau, peu de lignes de prose."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 6:
        return False
    defs = sum(1 for l in lines if CODE_BOUNDARY.match(l))
    prose = sum(1 for l in lines if PROSE_END.search(l) and " " in l.strip())
    return defs >= 3 and prose < len(lines) * 0.2


def _code_atoms(code: str, size: int, lang: str | None, fenced: bool) -> list[str]:
    """Code coupé entre les fonctions / classes (documents._chunks_code), balises ``` remises sur chaque morceau."""
    pieces = [t for t, _ in _chunks_code(code, max(1, size - 8), lang)]
    if not fenced:
        return [p + "\n\n" for p in pieces]
    return [f"```{lang or ''}\n{p}\n```\n\n" for p in pieces]


def atoms(text: str, max_chars: int) -> list[str]:
    """Plus petits morceaux insécables du message : blocs de code (coupés entre fonctions s'ils sont trop gros),
    sections Markdown, paragraphes, phrases."""
    size = max_chars // CHARS_PER_TOKEN
    out: list[str] = []
    pos = 0
    for m in [*FENCE.finditer(text), None]:
        prose = text[pos : m.start() if m else len(text)]
        if prose.strip():
            if looks_code(prose):
                out += _code_atoms(prose.strip("\n"), size, None, fenced=False)
            else:
                for section in HEADING.split(prose):
                    parts = _atoms(section, max_chars, SEPARATORS) if section else []
                    if len(parts) > 1 and LONE_HEADING.fullmatch(parts[0]) and len(parts[0] + parts[1]) <= max_chars:
                        parts[:2] = [parts[0] + parts[1]]  # un titre reste avec son 1er paragraphe
                    out += parts
        if m:
            block = m.group(0) + "\n\n"
            out += [block] if len(block) <= max_chars else _code_atoms(m.group(2), size, m.group(1), fenced=True)
            pos = m.end()
    return out


def split_message(text: str, chunk_tokens: int) -> list[str]:
    max_chars = chunk_tokens * CHARS_PER_TOKEN
    return _merge(atoms(text, max_chars), max_chars, 0)


def excerpt(text: str, tokens: int) -> str:
    """Début et fin du message, coupés à une fin de phrase ou de mot : la consigne de la demande."""
    n = max(200, tokens * CHARS_PER_TOKEN // 2)
    if len(text) <= 2 * n:
        return f"« {text.strip()} »"

    head, tail = text[:n], text[-n:]
    h = max(head.rfind(". "), head.rfind("\n"))  # dernière fin de phrase du début
    head = head[: h + 1] if h > n // 3 else head.rsplit(" ", 1)[0]
    t = min((i for i in (tail.find(". "), tail.find("\n")) if i >= 0), default=-1)  # 1re de la fin
    tail = tail[t + 1 :] if 0 <= t < n * 2 // 3 else tail.split(" ", 1)[-1]
    return f"« {head.strip()} […]\n[…] {tail.strip()} »"


# ---------- plan ----------


def _msg_text(m: dict) -> str:
    return _text_and_images(m.get("content"))[0]


def splittable(body: dict, cls: Classification) -> int | None:
    """Position du message à découper : le plus long message de l'utilisateur (texte seul), pas forcément le dernier
    — un long texte collé, puis « traduis-le » dans le message suivant. None : outils et images ne se découpent pas."""
    if cls.needs_tools or cls.needs_vision:
        return None
    users = [(len(_msg_text(m)), i) for i, m in enumerate(body.get("messages") or []) if m.get("role") == "user"]
    if not users or not max(users)[0]:
        return None
    return max(users)[1]


def make_plan(body: dict, cls: Classification, workers: list[ModelInfo]) -> SplitPlan:
    """Découpe le plus long message de l'utilisateur pour la plus petite fenêtre des workers ; la dernière question
    sert de consigne si ce n'est pas lui. La synthèse reprend la conversation, sans ses plus anciens messages s'ils
    ne laissent pas la place aux notes. RequestError si le message reste trop long même découpé."""
    index = splittable(body, cls)
    if index is None:
        raise RequestError("Ce message ne peut pas être découpé (outils ou images).")
    msgs = body["messages"]
    last = max(i for i, m in enumerate(msgs) if m.get("role") == "user")
    text = _msg_text(msgs[index])
    window = min(w.context_window for w in workers)
    out = int(cls.max_output_tokens or min(1024, window // 4))
    ex = excerpt(text, min(300, window // 10))
    demand = excerpt(_msg_text(msgs[last]), min(300, window // 10)) if last != index else ""
    consigne = estimate_tokens(ex) + estimate_tokens(demand)
    system = sum(estimate_tokens(_msg_text(m)) + 4 for m in msgs if m.get("role") in ("system", "developer"))
    map_room = int((window - min(1024, window // 4) - PROMPT_TOKENS - consigne - system) * SAFETY)
    tooshort = RequestError(
        f"Message trop long pour une fenêtre de {window} tokens, même découpé : les consignes système et la "
        "réponse attendue ne laissent pas assez de place. Raccourcissez le prompt système, demandez une réponse "
        "plus courte (max_tokens) ou utilisez un modèle à plus grande fenêtre."
    )
    if map_room < MIN_CHUNK_TOKENS:
        raise tooshort
    chunks = split_message(text, map_room)
    if len(chunks) > max_parts():
        raise RequestError(
            f"Message trop long : il faudrait le découper en {len(chunks)} parties pour une fenêtre de {window} "
            f"tokens (au plus {max_parts()}, SPLIT_MAX_PARTS). Utilisez un modèle à plus grande fenêtre."
        )
    # synthèse : système + conversation (le long message remplacé par un rappel) + notes ; les plus anciens
    # échanges sont retirés tant que les notes n'ont pas la place nécessaire
    single = len(chunks) == 1
    question = estimate_tokens(_msg_text(msgs[last])) if last != index else 0  # repris en entier
    room = int((window - out - PROMPT_TOKENS - estimate_tokens(ex) - question - system) * SAFETY)
    need = estimate_tokens(text) if single else max(MIN_CHUNK_TOKENS, min(len(chunks) * 128, room // 2))
    older = [i for i, m in enumerate(msgs) if m.get("role") not in ("system", "developer") and i not in (index, last)]
    size = {i: estimate_tokens(_msg_text(msgs[i])) + 4 for i in older}
    trimmed = []
    while older and room - sum(size[i] for i in older) < need:
        trimmed.append(older.pop(0))
    notes_budget = room - sum(size[i] for i in older)
    if notes_budget < need:
        raise tooshort
    map_tokens = max(128, min(min(1024, window // 4), notes_budget // len(chunks)))
    return SplitPlan(
        chunks, workers, window, map_room, map_tokens, notes_budget, ex, index, last, demand, sorted(trimmed)
    )


def choose_workers(
    gw: Gateway, cls: Classification, mode: str, models: list[ModelInfo], json_required: bool, effort: str
) -> list[ModelInfo]:
    """Routage auto sans modèle assez grand : modèles locaux à fenêtre MESURÉE (une fenêtre supposée pourrait
    tronquer en silence), classés selon le mode ; le premier fait la synthèse, et un modèle par AUTRE serveur local
    traite des parties en parallèle (s'il a au moins la moitié de la fenêtre du premier)."""
    local = [m for m in models if m.hosting == "local" and m.ref in gw.registry.local_context]
    if not local:
        return []
    try:
        d = route(replace(cls, min_context=0), mode, local, gw.latency, json_required=json_required, effort=effort)
    except RoutingError:
        return []
    by_ref = {m.ref: m for m in local}
    ranked = [by_ref[r] for r in [d.ref, *d.alternatives] if r in by_ref]
    ranked += [m for m in local if m not in ranked]
    first, servers = ranked[0], {ranked[0].provider}
    workers = [first]
    for m in ranked[1:]:
        if m.provider not in servers and m.context_window * 2 >= first.context_window:
            servers.add(m.provider)
            workers.append(m)
    return workers


# ---------- exécution ----------


def _map_body(body: dict, plan: SplitPlan, prompt: str, user: str, max_tokens: int) -> dict:
    system = prompt
    original = _system_text(body["messages"])
    if original:
        system += f"\n\nConsignes d'origine :\n{original}"
    clean = {
        k: v
        for k, v in body.items()
        if k not in ("response_format", "tools", "tool_choice", "n", "stream_options", "max_completion_tokens")
    }
    return {
        **clean,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }


async def _map(
    gw: Gateway,
    sel: Selection,
    plan: SplitPlan,
    items: list[str],
    build: Callable[[int], dict],
    role: str,
    progress: Callable[[int, int], None] | None,
):
    """Traite chaque élément : un worker par serveur local, chacun prenant le suivant dans la file (en parallèle
    s'il y a plusieurs serveurs, à la suite sinon). Un élément en échec est repris (une fois) par un worker ; un
    worker qui échoue deux fois s'arrête."""
    answers: list[str | None] = [None] * len(items)
    queue, tries, lost = deque(range(len(items))), [0] * len(items), []
    done = [0]

    async def worker(info: ModelInfo):
        fails = 0
        while queue and not lost:
            i = queue.popleft()
            try:
                resp, ms = await _call(gw, sel, info, build(i))
            except ProviderError as e:
                plan.agents.append(_failed(info, role, e, part=i + 1))
                tries[i] += 1
                fails += 1
                if tries[i] < 2:
                    queue.appendleft(i)
                else:
                    lost.append(e)
                if fails >= 2:
                    return
                continue
            answers[i] = _text(resp).strip() or "(rien)"
            plan.agents.append(_agent(info, role, resp.get("usage") or {}, ms, part=i + 1))
            done[0] += 1
            if progress:
                progress(done[0], len(items))

    await asyncio.gather(*(worker(w) for w in plan.workers))
    if any(a is None for a in answers):
        e = lost[0] if lost else next((a for a in plan.agents if not a["ok"]), None)
        msg = e.message if isinstance(e, ProviderError) else (e or {}).get("error", "aucun serveur disponible")
        raise ProviderError(
            502, f"Message découpé : une partie n'a pas pu être traitée ({msg}).", plan.workers[0].provider
        )
    return answers


def _notes(answers: list[str]) -> str:
    return "\n\n".join(f"### Partie {i + 1}\n{a}" for i, a in enumerate(answers))


async def collect(
    gw: Gateway, sel: Selection, body: dict, plan: SplitPlan, progress: Callable[[int, int], None] | None = None
) -> tuple[str, int]:
    """Phase map, puis condensation tant que les notes ne tiennent pas dans l'appel reduce (2 tours au plus).
    Long message d'un seul morceau (seul l'historique débordait) : aucune note, synthèse directe."""
    n = len(plan.chunks)
    if plan.single:
        return "", 0
    answers = await _map(
        gw,
        sel,
        plan,
        plan.chunks,
        lambda i: _map_body(
            body, plan, MAP_PROMPT.format(n=n, i=i + 1, consigne=plan.consigne), plan.chunks[i], plan.map_tokens
        ),
        "map",
        progress,
    )
    notes, rounds = _notes(answers), 0
    while estimate_tokens(notes) > plan.notes_budget and rounds < 2:
        rounds += 1
        groups = split_message(notes, plan.chunk_tokens)
        out = max(128, plan.notes_budget // max(1, len(groups)))
        condensed = await _map(
            gw,
            sel,
            plan,
            groups,
            lambda i, groups=groups, out=out: _map_body(
                body, plan, CONDENSE_PROMPT.format(consigne=plan.consigne), groups[i], out
            ),
            "condense",
            None,
        )
        notes = "\n\n".join(condensed)
    return notes, rounds


def reduce_body(body: dict, plan: SplitPlan, notes: str) -> dict:
    """Conversation pour la synthèse : anciens messages retirés si besoin ; le long message remplacé par les notes
    (ou par un rappel, les notes rejoignant la dernière question posée après lui)."""
    msgs = [dict(m) for m in body["messages"]]
    n = len(plan.chunks)
    if not plan.single:
        if plan.last == plan.index:
            msgs[plan.index] = {"role": "user", "content": REDUCE_PROMPT.format(n=n, excerpt=plan.excerpt, notes=notes)}
        else:
            msgs[plan.index] = {"role": "user", "content": PLACEHOLDER.format(n=n, excerpt=plan.excerpt)}
            msgs[plan.last] = {
                "role": "user",
                "content": REDUCE_LATER.format(demand=_msg_text(msgs[plan.last]), notes=notes),
            }
    drop = set(plan.trimmed)
    return {**body, "messages": [m for i, m in enumerate(msgs) if i not in drop]}


def _meta(sel: Selection, plan: SplitPlan, t0: float, rounds: int, reduced: bool) -> tuple[dict, dict]:
    ok = [a for a in plan.agents if a["ok"]]
    i = sum(a["prompt_tokens"] for a in ok)
    o = sum(a["completion_tokens"] for a in ok)
    usd = sum(a["cost_usd"] for a in ok)
    info = sel.info
    meta = {
        "provider": info.provider,
        "model": info.id,
        "hosting": info.hosting,
        "cost_usd": round(usd, 8),
        "cost_eur": round(usd * usd_eur_rate(), 8),
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "split": {**plan.to_dict(), "condense_rounds": rounds, "reduced": reduced, "agents": plan.agents},
    }
    if info.free:
        meta["free_tier"] = True
    return {"prompt_tokens": i, "completion_tokens": o, "total_tokens": i + o}, meta


def _fallback_text(notes: str) -> str:
    return "La synthèse finale a échoué ; voici les notes prises sur chaque partie du message :\n\n" + notes


async def run(gw: Gateway, sel: Selection, body: dict, t0: float) -> tuple[dict, dict]:
    plan: SplitPlan = sel.split
    notes, rounds = await collect(gw, sel, body, plan)
    try:
        resp, ms = await _call(gw, sel, sel.info, reduce_body(body, plan, notes))
        plan.agents.append(_agent(sel.info, "reduce", resp.get("usage") or {}, ms))
        reduced = True
    except ProviderError as e:
        if not notes:  # rien à rendre à la place de la synthèse
            raise
        plan.agents.append(_failed(sel.info, "reduce", e))
        resp, reduced = (
            {
                "object": "chat.completion",
                "model": sel.info.id,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": _fallback_text(notes)},
                    }
                ],
            },
            False,
        )
    usage, meta = _meta(sel, plan, t0, rounds, reduced)
    resp["usage"] = usage
    return resp, meta


def progress_chunk(model: str, done: int, total: int) -> dict:
    """Avancement de la lecture (le flux s'ouvre dès la 1re partie lue : pas de longue attente muette)."""
    return {
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [],
        "x_gateway_progress": {"split": {"done": done, "parts": total}},
    }


async def stream(gw: Gateway, sel: Selection, body: dict, t0: float) -> AsyncIterator[dict]:
    """Maps non streamés (avec chunks d'avancement), puis synthèse streamée ; le dernier chunk porte l'usage agrégé
    et les métadonnées (`x_gateway`)."""
    plan: SplitPlan = sel.split
    events: asyncio.Queue = asyncio.Queue()
    task = asyncio.ensure_future(collect(gw, sel, body, plan, lambda d, n: events.put_nowait((d, n))))
    try:
        while True:
            get = asyncio.ensure_future(events.get())
            await asyncio.wait({task, get}, return_when=asyncio.FIRST_COMPLETED)
            if get.done():
                yield progress_chunk(sel.info.id, *get.result())
                continue
            get.cancel()
            while not events.empty():
                yield progress_chunk(sel.info.id, *events.get_nowait())
            break
        notes, rounds = task.result()
    finally:
        if not task.done():
            task.cancel()
    info, p = sel.info, gw.providers[sel.info.provider]
    payload = gw.upstream_payload(reduce_body(body, plan, notes), info, sel.effort, sel.prompt_cache, sel.cache_key)
    t_red, red_usage, last, started, reduced, hold = time.monotonic(), {}, None, False, True, None
    try:
        hold = gw.admit(sel, info, payload)  # limites du fournisseur (9.6)
        async for chunk in p.stream(payload):
            if chunk.get("usage"):
                red_usage, last = chunk["usage"], chunk
                continue
            started = True
            yield chunk
        gw.health.mark_success(info.ref)
        plan.agents.append(_agent(info, "reduce", red_usage, int((time.monotonic() - t_red) * 1000)))
    except (ProviderError, RateLimited) as e:  # RateLimited : refusé avant l'envoi
        if isinstance(e, ProviderError):
            gw.health.mark_failure(info.ref, e)
        gw.unadmit(sel, hold)
        if started or not notes:
            raise
        plan.agents.append(_failed(info, "reduce", e))
        reduced = False
        yield {
            "object": "chat.completion.chunk",
            "model": info.id,
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": _fallback_text(notes)}, "finish_reason": "stop"}
            ],
        }
    usage, meta = _meta(sel, plan, t0, rounds, reduced)
    yield {
        **(last or {"object": "chat.completion.chunk", "model": info.id}),
        "choices": [],
        "usage": usage,
        "x_gateway": meta,
    }
