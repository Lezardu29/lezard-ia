"""Orchestration multi-agents (tâche 2.4, revue en 10.5).

Stratégie « découper, répartir, assembler » :
1. découpage : une IA rapide ou peu chère (selon le mode) découpe la demande en sous-questions
   indépendantes (au plus ORCHESTRATION_MAX_PARTS) ;
2. répartition : chaque sous-question est routée SÉPARÉMENT selon l'objectif de l'utilisateur (vitesse, coût,
   équilibre) — une partie simple peut aller à un petit modèle local gratuit — et toutes partent EN MÊME TEMPS,
   de préférence vers des IA différentes (un serveur local traite souvent une requête à la fois) ;
3. assemblage : le modèle choisi pour la demande complète rédige la réponse finale à partir des parties.
Demande indivisible : un seul appel normal (le surcoût se limite au découpage, petit et bon marché).
Partie en échec : retentée sur une autre IA, sinon laissée à l'assembleur. Assembleur en échec : les parties
sont renvoyées telles quelles, avec leurs titres.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from collections.abc import AsyncIterator

from .classifier import _text_and_images, classify
from .health import max_attempts
from .limits import RateLimited
from .local_context import looks_ollama
from .providers.base import ProviderError
from .registry import ModelInfo, usage_tokens, usd_eur_rate
from .router import RoutingError, route

if TYPE_CHECKING:
    from .main import Gateway, Selection

STRATEGY = "decompose-parallel-assemble"
PLANNER_PROMPT = (
    "Tu répartis le travail entre plusieurs assistants IA qui vont travailler EN PARALLÈLE, sans se voir. "
    "Découpe la dernière demande de l'utilisateur en sous-questions INDÉPENDANTES (au plus {n}), chacune "
    "compréhensible seule, qui ensemble couvrent toute la demande. Ne découpe que si cela aide vraiment : "
    "une demande simple ou d'un seul tenant reste UNE seule sous-question (la demande elle-même). "
    "Donne à chaque sous-question le rôle de l'assistant qui la traite : une spécialité courte, par exemple « juriste », "
    "« développeur Python » ou « nutritionniste ». "
    'Réponds UNIQUEMENT avec ce JSON, dans la langue de l\'utilisateur : {{"parts": [{{"role": "…", "question": "…"}}]}}'
)
WORKER_PROMPT = (
    "Tu traites UNE partie d'une demande plus large, répartie entre plusieurs assistants qui travaillent en "
    "parallèle. Demande complète, pour contexte seulement : « {question} ». Réponds de façon complète et précise "
    "à la seule partie qui t'est confiée, sans introduction ni conclusion générales. N'invente ni chiffres, ni "
    "lieux, ni sondages, ni sources : ce que la demande ne précise pas, présente-le comme une hypothèse à vérifier "
    "(« par exemple », « à adapter »)."
)
ASSEMBLER_PROMPT = (
    "Plusieurs assistants ont traité chacun une partie de la dernière demande de l'utilisateur. Assemble leurs "
    "réponses en UNE réponse finale cohérente et bien structurée : élimine les redites, corrige les "
    "contradictions, fais les liens entre les parties et traite toi-même toute partie restée sans réponse. Garde les hypothèses signalées comme telles, sans les présenter comme des faits. "
    "Réponds directement à l'utilisateur, sans mentionner ce découpage ni les assistants."
)


def max_parts() -> int:
    try:
        return min(8, max(2, int(os.getenv("ORCHESTRATION_MAX_PARTS", "4"))))
    except ValueError:
        return 4


@dataclass
class Plan:
    parts: list[str]  # ≥ 2 sous-questions, ou vide (demande indivisible / découpage raté)
    planner: dict  # agent du découpage (coût, latence) ; ok=False s'il a échoué
    roles: list[str] = field(default_factory=list)  # un rôle d'expert par partie (vide : pas de rôle)


# ---------- utilitaires ----------


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return _text_and_images(m.get("content"))[0]
    return ""


def _system_text(messages: list[dict]) -> str:
    return "\n".join(
        m["content"] for m in messages if m.get("role") in ("system", "developer") and isinstance(m.get("content"), str)
    )


def parse_plan(text: str, limit: int) -> tuple[list[str], list[str]] | None:
    """Sous-questions et rôles extraits de la réponse du découpeur (JSON, éventuellement entouré de texte).
    Accepte « {"parts": [{"role", "question"}]} » et l'ancien format « {"parts": ["…"]} » (rôle vide)."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        data = json.loads(m.group(0)) if m else None
    except ValueError:
        return None
    parts = data.get("parts") if isinstance(data, dict) else None
    if not isinstance(parts, list):
        return None
    out: list[str] = []
    roles: list[str] = []
    for p in parts:
        if isinstance(p, dict):
            question, role = str(p.get("question") or "").strip(), " ".join(str(p.get("role") or "").split())
        elif isinstance(p, str):
            question, role = p.strip(), ""
        else:
            continue
        if question and question not in out:
            out.append(question[:2000])
            roles.append(role[:60])  # un titre de rôle, pas une consigne : court et sur une ligne
    if not out:
        return None
    return out[:limit], roles[:limit]


def parse_parts(text: str, limit: int) -> list[str] | None:
    """Sous-questions seules (voir parse_plan)."""
    plan = parse_plan(text, limit)
    return plan[0] if plan else None


def _agent(info: ModelInfo, role: str, usage: dict, ms: int, **extra) -> dict:
    i, o, cached, _ = usage_tokens(usage)
    return {
        "ref": info.ref,
        "role": role,
        "ok": True,
        "prompt_tokens": i,
        "completion_tokens": o,
        "cached_tokens": cached,
        "cost_usd": round(info.usage_cost(usage), 8),
        "latency_ms": ms,
        **extra,
    }


def _failed(info: ModelInfo, role: str, e: BaseException, **extra) -> dict:
    msg = e.message if isinstance(e, ProviderError) else str(e)
    return {"ref": info.ref, "role": role, "ok": False, "error": msg[:200], **extra}


def _payload(gw: Gateway, sel: Selection, info: ModelInfo, body: dict, effort: str | None = None) -> dict:
    return gw.upstream_payload(body, info, effort or sel.effort, sel.prompt_cache, sel.cache_key)


def _quick(gw: Gateway, info: ModelInfo) -> dict:
    """Découpage = tâche simple : sans phase de réflexion. Ollama l'accepte pour tout modèle (« none ») — mesuré
    sur gemma4 : 0,7 s au lieu de 5 à 7 s, et sans risque d'épuiser max_tokens avant le JSON. Un autre serveur
    pourrait refuser ce paramètre : il n'est envoyé qu'à Ollama."""
    p = gw.providers[info.provider]
    return {"reasoning_effort": "none"} if looks_ollama(p.name, p.cfg.base_url) else {}


async def _call(
    gw: Gateway, sel: Selection, info: ModelInfo, body: dict, effort: str | None = None, extra: dict | None = None
) -> tuple[dict, int]:
    p = gw.providers[info.provider]
    payload = {**_payload(gw, sel, info, {**body, "stream": False}, effort), **(extra or {})}
    t0 = time.monotonic()
    try:  # limites du fournisseur, appels simultanés compris (9.6)
        hold = gw.admit(sel, info, payload)
    except RateLimited as e:  # saturé : l'appelant passe à une autre IA
        raise ProviderError(429, e.message, info.provider, e.retry_after)
    try:
        resp = await p.chat(payload)
    except ProviderError as e:
        gw.unadmit(sel, hold)
        gw.health.mark_failure(info.ref, e)
        raise
    gw.health.mark_success(info.ref)
    ms = int((time.monotonic() - t0) * 1000)
    gw.latency.observe(info.ref, ms)
    return resp, ms


def _text(resp: dict) -> str:
    return (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""


# ---------- 1. découpage ----------


def _quota_capped(m: ModelInfo) -> bool:
    """Offre gratuite à quota quotidien serré (OpenRouter « :free » : 50 requêtes par jour sans crédit)."""
    return m.free and m.provider == "openrouter"


def planner_models(gw: Gateway, sel: Selection) -> list[ModelInfo]:
    """IA du découpage, la moins chère d'abord, sans rien perdre en qualité sur une tâche aussi simple : une IA
    locale (0 €, aucun quota), puis les IA cloud sans quota serré, et seulement en dernier recours les offres
    gratuites à quota quotidien serré (elles servent aux réponses, pas au découpage)."""
    by_ref = {m.ref: m for m in sel.candidates}
    cls = replace(sel.cls, complexity="low", task="chat", needs_tools=False, needs_vision=False)
    local = [m for m in sel.candidates if m.hosting == "local"]
    cloud = [m for m in sel.candidates if m.hosting != "local" and not _quota_capped(m)]
    capped = [m for m in sel.candidates if _quota_capped(m)]
    # mode vitesse : le plus rapide prime (le local n'est pas forcément le plus rapide) ; sinon le gratuit d'abord
    groups = [cloud, local, capped] if sel.decision.mode == "speed" else [local, cloud, capped]
    out: list[ModelInfo] = []
    for group in groups:
        if not group:
            continue
        try:
            d = route(cls, sel.decision.mode, group, gw.latency, effort="low")
        except RoutingError:  # aucun modèle de ce groupe ne convient à la tâche : groupe suivant
            continue
        out += [by_ref[r] for r in [d.ref, *d.alternatives[:1]] if r in by_ref and by_ref[r] not in out]
    return out[:3]


def assembly_chain(sel: Selection) -> list[ModelInfo]:
    """Rédacteur de la réponse finale : le modèle choisi, puis les secours de la chaîne (2.5), dans l'ordre et dans la
    limite des tentatives configurées (FALLBACK_MAX_ATTEMPTS)."""
    out: list[ModelInfo] = []
    for m in [sel.info, *(sel.fallbacks or [])]:
        if len(out) < max_attempts() and m.ref not in {x.ref for x in out}:
            out.append(m)
    return out


async def plan(gw: Gateway, sel: Selection, body: dict) -> Plan:
    convo = [m for m in body["messages"] if m.get("role") in ("user", "assistant")][-6:]
    ask = {k: v for k, v in body.items() if k not in ("response_format", "tools", "tool_choice", "n")}
    ask.update(
        messages=[{"role": "system", "content": PLANNER_PROMPT.format(n=max_parts())}, *convo],
        max_tokens=1500,
        temperature=0,
    )
    failure = None
    for planner in planner_models(gw, sel):  # IA choisie, puis une remplaçante
        try:
            resp, ms = await _call(gw, sel, planner, ask, effort="low", extra=_quick(gw, planner))
        except ProviderError as e:
            failure = _failed(planner, "planner", e)
            continue
        parts, roles = parse_plan(_text(resp), max_parts()) or ([], [])
        agent = _agent(planner, "planner", resp.get("usage") or {}, ms, parts=len(parts) or 1)
        if len(parts) >= 2:
            return Plan(parts, agent, roles)
        return Plan([], agent)
    return Plan([], failure)  # découpage impossible : réponse en un seul appel


# ---------- 2. répartition ----------


def assign(gw: Gateway, sel: Selection, parts: list[str]) -> list[list[ModelInfo]]:
    """Pour chaque partie : IA choisies selon le mode (la 1re, puis ses remplaçantes), en évitant de donner deux
    parties à la même IA tant qu'une autre, classée juste après, est disponible."""
    by_ref, used, out = {m.ref: m for m in sel.candidates}, set(), []
    # l'assembleur cloud garde son quota de la minute (offres gratuites : tokens/min) : aucune partie s'il y a une autre IA
    keep = sel.info.ref if sel.info.hosting == "cloud" else None
    if keep:
        used.add(keep)
    for part in parts:
        cls = classify({"messages": [{"role": "user", "content": part}]})
        try:
            d = route(cls, sel.decision.mode, sel.candidates, gw.latency, effort=sel.effort)
            ranked = [by_ref[r] for r in [d.ref, *d.alternatives] if r in by_ref]
        except RoutingError:  # aucun modèle adapté à la partie : celui de la demande
            ranked = [sel.info]
        fresh = [m for m in ranked[:3] if m.ref not in used]
        others = [m for m in ranked if m.ref != keep]
        first = fresh[0] if fresh else others[0] if others else ranked[0]
        used.add(first.ref)
        out.append([first] + [m for m in ranked if m.ref != first.ref][:1])
    return out


def worker_body(body: dict, question: str, part: str, role: str = "") -> dict:
    system = (f"Tu es {role}. " if role else "") + WORKER_PROMPT.format(question=question[:3000])
    original = _system_text(body["messages"])
    if original:
        system += f"\n\nConsignes d'origine :\n{original}"
    clean = {k: v for k, v in body.items() if k not in ("response_format", "tools", "tool_choice", "n")}
    return capped({**clean, "messages": [{"role": "system", "content": system}, {"role": "user", "content": part}]})


def part_tokens() -> int:
    """Longueur maximale de la réponse d'un expert : toutes les parties doivent tenir ensemble dans l'appel
    d'assemblage (offres gratuites limitées en tokens par minute : environ 8 000 chez Groq)."""
    return max(200, int(os.getenv("ORCHESTRATION_PART_TOKENS", "1200") or 1200))


def capped(body: dict) -> dict:
    """Requête d'un expert, sa réponse limitée à part_tokens() (ou moins, si la demande le précisait)."""
    asked = body.get("max_tokens") or body.get("max_completion_tokens")
    clean = {k: v for k, v in body.items() if k != "max_completion_tokens"}
    return {**clean, "max_tokens": min(asked, part_tokens()) if asked else part_tokens()}


async def _work(
    gw: Gateway, sel: Selection, body: dict, question: str, i: int, part: str, models: list[ModelInfo], role: str = ""
) -> tuple[str | None, list[dict]]:
    agents = []
    expert = {"expert": role} if role else {}
    for info in models:  # IA choisie, puis une remplaçante
        try:
            resp, ms = await _call(gw, sel, info, worker_body(body, question, part, role))
        except ProviderError as e:
            agents.append(_failed(info, "worker", e, part=i + 1, **expert))
            continue
        agents.append(_agent(info, "worker", resp.get("usage") or {}, ms, part=i + 1, **expert))
        return _text(resp), agents
    return None, agents


async def dispatch(gw: Gateway, sel: Selection, body: dict, parts: list[str], roles: list[str] | None = None):
    question = _last_user_text(body["messages"])
    jobs = assign(gw, sel, parts)
    roles = [*(roles or []), *[""] * len(parts)][: len(parts)]  # une entrée par partie, vide si pas de rôle
    results = await asyncio.gather(
        *(_work(gw, sel, body, question, i, p, m, r) for i, (p, m, r) in enumerate(zip(parts, jobs, roles)))
    )
    answers = [a for a, _ in results]
    agents = [ag for _, ags in results for ag in ags]
    if not any(a is not None for a in answers):
        failed = next(a for a in agents if not a["ok"])
        raise ProviderError(502, f"Aucune partie n'a pu être traitée ({failed['error']}).", failed["ref"])
    return answers, agents


# ---------- 3. assemblage ----------


def assembler_messages(
    messages: list[dict], parts: list[str], answers: list[str | None], roles: list[str] | None = None
) -> list[dict]:
    system = _system_text(messages)
    convo = [dict(m) for m in messages if m.get("role") not in ("system", "developer")]
    sys_text = ASSEMBLER_PROMPT + (f"\n\nConsignes d'origine :\n{system}" if system else "")
    roles = [*(roles or []), *[""] * len(parts)][: len(parts)]
    block = "\n\n".join(
        f"### Partie {i + 1}{f' ({r})' if r else ''} : {p}\n"
        f"{a if a is not None else '(sans réponse : traite-la toi-même)'}"
        for i, (p, a, r) in enumerate(zip(parts, answers, roles))
    )
    block = f"Réponses des assistants, partie par partie :\n\n{block}\n\nRédige maintenant la réponse finale."
    last = convo[-1] if convo else None
    if last and last["role"] == "user":  # évite deux messages user consécutifs
        c = last["content"]
        last["content"] = f"{c}\n\n---\n{block}" if isinstance(c, str) else list(c) + [{"type": "text", "text": block}]
    else:
        convo.append({"role": "user", "content": block})
    return [{"role": "system", "content": sys_text}] + convo


def concatenated(parts: list[str], answers: list[str | None]) -> str:
    """Repli si l'assembleur échoue : les parties, avec leurs titres."""
    return "\n\n".join(f"## {p}\n\n{a}" for p, a in zip(parts, answers) if a is not None)


def _meta(final: ModelInfo, agents: list[dict], parts: list[str], t0: float, assembled: bool) -> tuple[dict, dict]:
    ok = [a for a in agents if a["ok"]]
    i = sum(a["prompt_tokens"] for a in ok)
    o = sum(a["completion_tokens"] for a in ok)
    usd = sum(a["cost_usd"] for a in ok)
    usage = {"prompt_tokens": i, "completion_tokens": o, "total_tokens": i + o}
    meta = {
        "provider": final.provider,
        "model": final.id,
        "hosting": final.hosting,
        "cost_usd": round(usd, 8),
        "cost_eur": round(usd * usd_eur_rate(), 8),
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "orchestration": {
            "strategy": STRATEGY,
            "decomposed": True,
            "parts": parts,
            "assembled": assembled,
            "agents": agents,
        },
    }
    return usage, meta


def _fallback_response(final: ModelInfo, text: str) -> dict:
    return {
        "object": "chat.completion",
        "model": final.id,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
    }


async def run(gw: Gateway, sel: Selection, body: dict, plan_: Plan, t0: float) -> tuple[dict, dict]:
    answers, workers = await dispatch(gw, sel, body, plan_.parts, plan_.roles)
    agents = [plan_.planner, *workers]
    messages = assembler_messages(body["messages"], plan_.parts, answers, plan_.roles)
    resp, final, assembled = None, sel.info, False
    for assembler in assembly_chain(sel):  # saturée ou en panne : le secours suivant rédige
        try:
            resp, ms = await _call(gw, sel, assembler, {**body, "messages": messages})
        except ProviderError as e:
            agents.append(_failed(assembler, "assembler", e))
            continue
        agents.append(_agent(assembler, "assembler", resp.get("usage") or {}, ms))
        final, assembled = assembler, True
        break
    if resp is None:  # toute la chaîne a échoué : les parties sont renvoyées telles quelles
        resp = _fallback_response(sel.info, concatenated(plan_.parts, answers))
    usage, meta = _meta(final, agents, plan_.parts, t0, assembled)
    resp["usage"] = usage
    return resp, meta


async def stream(gw: Gateway, sel: Selection, body: dict, plan_: Plan, t0: float) -> AsyncIterator[dict]:
    """Découpage et parties en non-streaming, puis réponse de l'assembleur streamée. Le dernier chunk porte
    l'usage agrégé et les métadonnées (clé `x_gateway`)."""
    answers, workers = await dispatch(gw, sel, body, plan_.parts, plan_.roles)
    agents = [plan_.planner, *workers]
    messages = assembler_messages(body["messages"], plan_.parts, answers, plan_.roles)
    final, assembled, last = None, False, None
    for assembler in assembly_chain(sel):  # basculement possible tant qu'aucun morceau n'est parti
        p = gw.providers[assembler.provider]
        payload = _payload(gw, sel, assembler, {**body, "messages": messages})
        t_asm, asm_usage, started = time.monotonic(), {}, False
        hold, refused = None, False
        try:
            try:
                hold = gw.admit(sel, assembler, payload)  # limites du fournisseur (9.6)
            except RateLimited as e:
                refused = True
                raise ProviderError(429, e.message, assembler.provider, e.retry_after)
            async for chunk in p.stream(payload):
                if chunk.get("usage"):
                    asm_usage, last = chunk["usage"], chunk
                    continue
                started = True
                yield chunk
            gw.health.mark_success(assembler.ref)
            agents.append(_agent(assembler, "assembler", asm_usage, int((time.monotonic() - t_asm) * 1000)))
            final, assembled = assembler, True
            break
        except ProviderError as e:
            if not refused:  # refus de la gateway elle-même : l'IA n'est pas en panne
                gw.health.mark_failure(assembler.ref, e)
            gw.unadmit(sel, hold)
            if started:
                raise
            agents.append(_failed(assembler, "assembler", e))
            last = None
    if final is None:  # toute la chaîne a échoué : les parties sont renvoyées telles quelles
        final = sel.info
        yield {
            "object": "chat.completion.chunk",
            "model": final.id,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": concatenated(plan_.parts, answers)},
                    "finish_reason": "stop",
                }
            ],
        }
    usage, meta = _meta(final, agents, plan_.parts, t0, assembled)
    yield {
        **(last or {"object": "chat.completion.chunk", "model": final.id}),
        "choices": [],
        "usage": usage,
        "x_gateway": meta,
    }


# ---------- aperçu (/v1/route) ----------


def preview(gw: Gateway, sel: Selection) -> dict:
    """Qui découpe, qui assemble, et le surcoût estimé de l'orchestration (10.5)."""
    planner = planner_models(gw, sel)[0]
    n, out = max_parts(), sel.cls.max_output_tokens or 500
    over = planner.cost_usd(sel.cls.prompt_tokens, 200) + sel.info.cost_usd(sel.cls.prompt_tokens + n * out, out)
    return {
        "strategy": STRATEGY,
        "planner": planner.ref,
        "assembler": sel.info.ref,
        "max_parts": n,
        "workers": "chaque sous-question est routée selon le mode, en parallèle",
        "est_overhead_usd": round(over, 8),
    }
