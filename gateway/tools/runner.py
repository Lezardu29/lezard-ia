"""Boucle d'outils : modèle → appels d'outils → résultats → modèle…, diffusée au chat sous forme d'événements
`x_gateway_tool` (call, approval, waiting, result) puis de la réponse finale au format OpenAI."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable

from . import RuleRefusal, StopRun, Tool, ToolError, ToolResult
from .budget import select_specs
from .state import TaskState, parse_plan, plan_messages

APPROVAL_TIMEOUT = 120
FAILSAFE_POLL = 0.1  # secondes entre deux regards sur l'arrêt d'urgence (souris dans le coin)
HEARTBEAT = 15  # sous le délai d'inactivité des tunnels (Cloudflare : 100 s)
MAX_RESULT = 8000
# appel écrit en texte : balises et blocs de code autour ignorés ; au-delà de quelques mots, la réponse cite un appel
CALL_WRAPPING = re.compile(r"</?tool_call>|```(?:json)?", re.I)
MAX_TEXT_AROUND_CALL = 40
MAX_NUDGES = 2  # réponse vide (ni outil ni texte) : le modèle est relancé, puis on rend la main
NUDGE = (
    "Ta réponse était vide. Continue la tâche : appelle l'outil de l'action suivante, ou réponds en texte si "
    "l'objectif est atteint ou si tu es bloqué."
)
# recherche ou page web redemandée à l'identique (qwen2.5-coder refait douze fois la même) : elle n'est pas relancée,
# et au 2e rappel le modèle doit conclure avec ce qu'il a déjà
REPEATED = (
    "Déjà fait : même appel, même résultat (il est plus haut dans la conversation). Ne le refais pas : réponds "
    "maintenant avec ce que tu as, ou change de requête."
)
MAX_REPEATS = 2
CONCLUDE = "Réponds maintenant à la demande, en texte, avec les résultats déjà obtenus. N'appelle plus d'outil."
NO_ANSWER = "Le modèle n'a pas su conclure après ses recherches : reposez la question, ou choisissez un autre modèle."
# réponse qui affirme avoir agi sur l'écran
CLAIM = re.compile(r"\bj'ai (ouvert|cliqué|lancé|fermé|écrit|tapé|affiché|navigué|démarré)", re.I)
PC_LOCK = asyncio.Lock()  # un seul agent contrôle le PC à la fois
# fenêtre de contexte demandée à Ollama pour une tâche sur le PC : guide, outils, capture et liste des éléments
# dépassent les 4 096 tokens par défaut dès le 2e tour (et ne se découpent pas). Le reste du chat garde la sienne.
PC_CONTEXT = int(os.getenv("TOOLS_PC_CONTEXT", "16384"))


class Approvals:
    """Validations en attente : jeton à usage unique, lié au compte et à l'action affichée. Chaque action sur le PC
    qui en demande une attend sa propre réponse : aucune autorisation n'est gardée pour la suite."""

    def __init__(self) -> None:
        self._pending: dict[str, tuple[str, asyncio.Future]] = {}

    def open(self, identity: str) -> tuple[str, asyncio.Future]:
        aid = secrets.token_urlsafe(18)
        fut = asyncio.get_running_loop().create_future()
        self._pending[aid] = (identity, fut)
        return aid, fut

    def decide(self, aid: str, identity: str, approve: bool) -> bool:
        item = self._pending.get(aid)
        if not item or item[0] != identity or item[1].done():
            return False
        item[1].set_result(approve)
        return True

    def close(self, aid: str) -> None:
        item = self._pending.pop(aid, None)
        if item and not item[1].done():
            item[1].cancel()


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        raise ToolError("Arguments illisibles (JSON attendu).") from None
    if not isinstance(value, dict):
        raise ToolError("Arguments : objet JSON attendu.")
    return value


# ---------- modèles sans appel d'outils natif (gemma3…) : outils décrits dans le prompt, réponse en JSON ----------
def emulated(msgs: list, specs: list) -> list:
    """Conversation réécrite sans rôle « tool » ni tool_calls, avec la liste des outils en tête."""
    lines = [
        f"- {s['function']['name']}({', '.join(s['function']['parameters'].get('properties', {}))}) : "
        f"{s['function']['description']}"
        for s in specs
    ]
    guide = (
        "Outils disponibles :\n" + "\n".join(lines) + "\n\nPour utiliser un outil, réponds UNIQUEMENT par un objet "
        'JSON, sans autre texte : {"tool": "nom", "arguments": {...}}. Un seul outil par réponse. '
        "Sans outil, réponds normalement en texte."
    )
    out = [{"role": "system", "content": guide}] if specs else []  # sans outils (plan, dernier tour) : texte seul
    for m in msgs:
        if m.get("tool_calls"):
            fn = m["tool_calls"][0].get("function") or {}
            args = fn.get("arguments")
            m = {
                "role": "assistant",
                "content": json.dumps(
                    {"tool": fn.get("name"), "arguments": json.loads(args) if isinstance(args, str) else args},
                    ensure_ascii=False,
                ),
            }
        elif m.get("role") == "tool":
            m = {"role": "user", "content": f"Résultat de l'outil : {m.get('content')}"}
        if out and out[-1]["role"] == m["role"] == "user":  # rôles alternés (modèles Gemma) : on fusionne
            parts = [
                p if isinstance(p, dict) else {"type": "text", "text": p}
                for c in (out[-1]["content"], m["content"])
                for p in (c if isinstance(c, list) else [c])
            ]
            out[-1] = {"role": "user", "content": parts}
        else:
            out.append(m)
    return out


def parse_emulated(resp: dict, names) -> dict:
    """Réponse texte {"tool": …, "arguments": …} → appel d'outil au format OpenAI."""
    msg = (resp.get("choices") or [{}])[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return resp
    raw = text[start : end + 1]
    try:
        data = json.loads(raw)
    except ValueError:
        try:  # syntaxe Python (None, True, 'texte') : gemma3 l'écrit souvent ; literal_eval n'exécute rien
            data = ast.literal_eval(raw)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return resp
    if not isinstance(data, dict) or data.get("tool") not in names:
        return resp
    args = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
    call = {
        "id": f"emu_{secrets.token_hex(4)}",
        "type": "function",
        # arguments vides (None) retirés : « "cell": None » ne doit pas masquer « "element": 2 »
        "function": {"name": data["tool"], "arguments": json.dumps({k: v for k, v in args.items() if v is not None})},
    }
    choice = {**resp["choices"][0], "message": {"role": "assistant", "content": "", "tool_calls": [call]}}
    return {**resp, "choices": [choice]}


def text_calls(content, names) -> list[dict]:
    """Appels d'outils écrits en texte par un modèle qui sait appeler les outils mais ne le fait pas toujours
    (qwen2.5 : « {"name": "…", "arguments": {…}} », parfois précédé ou entouré de balises). Ils sont exécutés comme
    des appels natifs. Seuls comptent un outil connu, des arguments en objet JSON, et une réponse qui n'est presque
    que l'appel : un JSON cité dans une vraie réponse (résumé d'un document, d'une page web) n'est jamais exécuté."""
    text = content or ""
    decoder, calls, spans, i = json.JSONDecoder(), [], [], text.find("{")
    while i >= 0:
        try:
            data, end = decoder.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        name = data.get("name", data.get("tool")) if isinstance(data, dict) else None
        args = data.get("arguments", data.get("parameters")) if isinstance(data, dict) else None
        if isinstance(name, str) and name in names and isinstance(args, dict):
            calls.append(
                {
                    "id": f"txt_{secrets.token_hex(4)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                }
            )
            spans.append((i, end))
            i = text.find("{", end)
        else:
            i = text.find("{", i + 1)
    starts, ends = [0] + [e for _, e in spans], [s for s, _ in spans] + [len(text)]
    around = "".join(text[a:b] for a, b in zip(starts, ends))
    if len(CALL_WRAPPING.sub("", around).strip()) > MAX_TEXT_AROUND_CALL:
        return []
    return calls


async def _watch(probe: Callable[[], str | None], tripped: asyncio.Future) -> None:
    """Arrêt d'urgence surveillé en continu : pendant que le modèle réfléchit ou qu'une validation attend aussi,
    pas seulement au moment d'agir."""
    while not tripped.done():
        if message := probe():
            tripped.set_result(message)
            return
        await asyncio.sleep(FAILSAFE_POLL)


async def _guarded(aw, tripped: asyncio.Future | None):
    """Attend `aw`, abandonné (StopRun) dès que l'arrêt d'urgence se déclenche."""
    if tripped is None:
        return await aw
    task = asyncio.ensure_future(aw)
    try:
        await asyncio.wait({task, tripped}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        task.cancel()
        raise
    if not task.done():
        task.cancel()
        raise StopRun(tripped.result())
    return task.result()


def _chunk(content: str | None = None, **extra) -> dict:
    delta = {"role": "assistant", "content": content} if content is not None else None
    return {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": "stop"}] if delta else [],
        **extra,
    }


async def run(
    *,
    call: Callable[[list, list | None], Awaitable[tuple[dict, str]]],
    tools: dict[str, Tool],
    messages: list,
    approvals: Approvals,
    identity: str,
    audit: Callable[..., None],
    max_steps: int,
    failsafe: Callable[[], str | None] | None = None,
    state: TaskState | None = None,
) -> AsyncIterator[dict]:
    """`call(messages, specs)` → (réponse OpenAI, hébergement du modèle « local » / « cloud »).
    `failsafe` : arrêt d'urgence (souris dans le coin) surveillé pendant toute la tâche.
    `state` (24.2) : objectif, plan, actions faites et limites de la tâche, gardés ici et rappelés à chaque tour.
    `audit(nom, arguments, décision, résultat=None, capture=None)` (21.4) : une entrée par action sur le PC, écrite
    après son exécution (décision, résultat, capture d'écran qui suit) ; un refus, aussitôt ; une lecture de
    l'écran (pc_screenshot, pc_zoom), avec sa capture."""
    query = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user" and isinstance(m.get("content"), str)), ""
    )
    specs = select_specs(list(tools.values()), query)
    pc_run = any(t.level == "pc" for t in tools.values())
    msgs, steps, usage = list(messages), [], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    holds_pc, done, seen_image = False, [], False  # done : actions sur le PC exécutées (détection des boucles)
    # données privées dans le contexte (écran, documents, outil MCP « local_only ») : dès lors, une recherche ou une
    # page web demandée par le modèle passe par la validation, sinon il pourrait les glisser dans l'adresse ou la
    # requête et les envoyer à un site (consigne cachée dans un e-mail ou une page affichée)
    sensitive = False
    nudges = 0  # réponses vides relancées
    fetched, repeats = set(), 0  # appels web déjà faits, et rappels à l'identique
    tripped = asyncio.get_running_loop().create_future() if pc_run and failsafe else None
    watcher = asyncio.create_task(_watch(failsafe, tripped)) if tripped else None
    try:
        if state is not None and state.plan_wanted:  # planificateur : sous-étapes avant la première action
            resp, _ = await _guarded(call(plan_messages(state.goal), None), tripped)
            for k in usage:
                usage[k] += (resp.get("usage") or {}).get(k) or 0
            state.plan = parse_plan(resp)
            if state.plan:
                yield _chunk(x_gateway_tool={"type": "plan", "id": "plan", "steps": state.plan})
        for step in range(max_steps + 1):
            expired = state is not None and state.expired()
            last = step == max_steps or expired or repeats >= MAX_REPEATS
            if repeats >= MAX_REPEATS:
                msgs.append({"role": "user", "content": CONCLUDE})
            if state is not None:  # état de la tâche : un seul message juste après la demande, remplacé à chaque tour
                state.steps = step
                msgs = [m for m in msgs if not m.get("x_state")]
                msgs.insert(len(messages), {"role": "user", "x_state": True, "content": state.render()})
            resp, hosting = await _guarded(
                call([{k: v for k, v in m.items() if not k.startswith("x_")} for m in msgs], None if last else specs),
                tripped,
            )
            for k in usage:
                usage[k] += (resp.get("usage") or {}).get(k) or 0
            msg = (resp.get("choices") or [{}])[0].get("message") or {}
            calls = msg.get("tool_calls") or []
            if not calls and tools and not last:  # appel écrit en texte : exécuté comme un appel natif
                calls = text_calls(msg.get("content"), tools)
                if calls:
                    msg = {**msg, "content": "", "tool_calls": calls}
            if not calls and not (msg.get("content") or "").strip() and not last and nudges < MAX_NUDGES:
                # petits modèles « pensants » (gemma4…) : tout passe dans la réflexion, ni outil ni réponse
                nudges += 1
                msgs.append({"role": "user", "content": NUDGE})
                continue
            if last or not calls:
                text = msg.get("content") or ("(limite d'étapes atteinte)" if calls else "")
                if (
                    last and tools and text_calls(text, tools)
                ):  # encore un appel, écrit en texte : jamais affiché tel quel
                    text = NO_ANSWER
                if pc_run and not done and CLAIM.search(text):  # « j'ai ouvert… » sans aucune action sur le PC
                    text += "\n\n⚠️ Aucune action n'a été faite sur le PC pendant cette réponse."
                if expired:
                    text += f"\n\n⏱️ Durée maximale de la tâche atteinte ({state.max_seconds} s)."
                yield _chunk(text)
                yield _chunk(usage=usage, x_gateway={**(resp.get("x_gateway") or {}), "tools": steps})
                return
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            image, attach, acted = None, None, False
            for n, c in enumerate(calls):
                cid = c.get("id") or f"call_{step}_{n}"
                name = (c.get("function") or {}).get("name") or ""
                tool, ok, result = tools.get(name), False, None
                learn: bool | str = False  # échec à retenir (texte : la raison à noter plutôt que l'erreur)
                decision = None  # validation à journaliser avec le résultat de l'action (21.4)
                try:
                    args = _args((c.get("function") or {}).get("arguments"))
                    yield _chunk(x_gateway_tool={"type": "call", "id": cid, "name": name, "args": args})
                    if tool is None:
                        raise ToolError(f"Outil inconnu ou non autorisé : {name}")
                    if tool.local_only and hosting != "local":
                        raise ToolError(
                            "Refusé : cet outil n'est permis qu'avec un modèle local (l'écran ne quitte pas le PC)."
                        )
                    if tool.level == "pc" and not holds_pc:
                        if PC_LOCK.locked():
                            raise ToolError("Un autre agent contrôle déjà le PC.")
                        await PC_LOCK.acquire()
                        holds_pc = True
                    # contrôle du PC : agir à l'aveugle, l'écran a changé depuis la capture (pas pour MCP, web…)
                    if tool.level == "pc" and tool.confirm and acted:
                        raise ToolError(
                            "Ignorée : une seule action sur le PC par tour, regarder d'abord la nouvelle capture."
                        )
                    if tool.fresh and seen_image and (stale := tool.fresh()):
                        raise ToolError(stale)
                    key = (name, json.dumps({k: v for k, v in args.items() if k != "reason"}, sort_keys=True))
                    if tool.confirm and done[-2:] == [key, key]:  # boucle : 3e fois la même action
                        learn = "répétée sans résultat"
                        raise ToolError(
                            "Refusée : même action déjà faite deux fois de suite. Elle ne mène nulle part : vérifie "
                            "la fenêtre au premier plan, change d'approche (pc_focus, pc_open, autre case) ou réponds."
                        )
                    if tool.level == "web" and key in fetched:
                        repeats += 1
                        raise ToolError(REPEATED)
                    if tool.confirm or (tool.level == "web" and sensitive):
                        aid, fut = approvals.open(identity)
                        try:
                            preview = tool.preview(args) if tool.preview else None
                            yield _chunk(
                                x_gateway_tool={
                                    "type": "approval",
                                    "id": cid,
                                    "approval": aid,
                                    "name": name,
                                    "args": args,
                                    "preview": preview,
                                }
                            )
                            waited = 0
                            while not fut.done() and waited < APPROVAL_TIMEOUT:
                                try:
                                    await _guarded(asyncio.wait_for(asyncio.shield(fut), HEARTBEAT), tripped)
                                except TimeoutError:
                                    waited += HEARTBEAT
                                    yield _chunk(x_gateway_tool={"type": "waiting", "id": cid})
                            approved = fut.done() and not fut.cancelled() and fut.result()
                            answered = fut.done() and not fut.cancelled()  # refus explicite, pas un silence
                        finally:
                            approvals.close(aid)
                        decision = "autorisée" if approved else "refusée"
                        if not approved:
                            audit(name, args, decision)
                            decision = None
                            learn = "refusée par l'utilisateur" if answered else False
                            raise ToolError(
                                "Action refusée par l'utilisateur (ou sans réponse) : ne pas la retenter "
                                "telle quelle, demander quoi faire."
                            )
                    acted = acted or (tool.level == "pc" and tool.confirm)
                    learn = tool.confirm
                    if tool.level == "pc" and not tool.confirm:
                        decision = "sans validation (lecture de l'écran)" if name != "pc_app_guide" else "guide"
                    result = await _guarded(tool.run(args), tripped)
                    ok = True
                    if tool.level == "web":
                        fetched.add(key)
                    sensitive = sensitive or tool.local_only or bool(result.image)
                    if decision:
                        audit(name, args, decision, result.text, result.image)
                        decision = None
                    if tool.confirm:
                        done.append(key)
                        if state is not None:
                            state.record(name, args, "sans effet, l'écran n'a pas changé" if result.no_effect else "ok")
                except StopRun as e:
                    if decision:
                        audit(name, args, decision, str(e))
                    yield _chunk(x_gateway_tool={"type": "result", "id": cid, "ok": False, "text": str(e)})
                    yield _chunk(f"⛔ {e}")
                    yield _chunk(usage=usage, x_gateway={"tools": steps + [{"name": name, "ok": False}]})
                    return
                except ToolError as e:
                    result = ToolResult(f"Erreur : {e}")
                    if decision:
                        audit(name, args, decision, result.text)
                    if state is not None and (learn or isinstance(e, RuleRefusal)):
                        state.record(name, args, learn if isinstance(learn, str) else str(e))
                text = result.text[:MAX_RESULT]
                steps.append({"name": name, "ok": ok})
                yield _chunk(x_gateway_tool={"type": "result", "id": cid, "ok": ok, "text": text[:300]})
                msgs.append({"role": "tool", "tool_call_id": cid, "content": text})
                if result.image:
                    image, attach = result.image, result.attach
                seen_image = seen_image or bool(result.image)
            if image:  # seule la dernière capture reste dans le contexte (les anciennes coûtent des tokens)
                for m in msgs:
                    if m.get("x_capture"):
                        m["content"] = "[capture précédente retirée]"
                        del m["x_capture"]
                msgs.append(
                    {
                        "role": "user",
                        "x_capture": True,
                        "content": [
                            {"type": "text", "text": "Capture d'écran actuelle." + (f" {attach}" if attach else "")},
                            {"type": "image_url", "image_url": {"url": image}},
                        ],
                    }
                )
    except StopRun as e:  # pendant que le modèle réfléchissait
        yield _chunk(f"⛔ {e}")
        yield _chunk(usage=usage, x_gateway={"tools": steps})
    finally:
        if watcher:
            watcher.cancel()
        if holds_pc:
            PC_LOCK.release()


APPROVALS = Approvals()
