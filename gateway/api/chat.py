"""Chat : /v1/chat/completions (OpenAI), /v1/messages (Anthropic), routage, classification, modèles,
compression et cache de réponses."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from types import SimpleNamespace
from collections.abc import AsyncIterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import crew, mcp, mcp_help, orchestrator, skills, splitter, tools, translate
from ..audit import capture_days, describe_actor
from ..cache import cache_key, normalize_policy, response_ttl
from ..classifier import _text_and_images, classify
from ..compression import CompressionError, compact, compress_history, normalize_mode as normalize_compression
from ..counters import Reservation
from ..documents import DocumentError, context_message, fits, select_context
from ..permissions import model_allowed
from ..plans import FEATURES, PlanContext, PlanError
from ..providers.base import ProviderError
from ..router import NON_CHAT, RoutingError
from ..store import DocumentStore
from ..tools.budget import select_specs
from ..validation import RequestError, json_object, validate_chat, validate_messages_body
from ..workspaces import WorkspaceError, render
from ..core import Gateway, Selection, _err
from ..tools import app_guides, memory, runner
from ..tools.state import TaskState
from ..tools.basic import DAYS


TIME_QUESTION = re.compile(r"\b(heure|date|jour|aujourd|demain|hier|semaine|today|now)\b", re.I)
TIMEZONE = os.getenv("GATEWAY_TIMEZONE", "Europe/Paris")  # fuseau de l'utilisateur, pour la date donnée au modèle


def user_now() -> tuple[datetime, str]:
    """Heure dans le fuseau de l'utilisateur, et son nom. Windows n'a pas de base des fuseaux : sans le paquet tzdata
    (absent du Python de l'installateur), ZoneInfo échouait et toute question sur la date ou l'heure finissait en
    erreur 500 ; c'est alors l'heure locale de ce PC."""
    try:
        return datetime.now(ZoneInfo(TIMEZONE)), TIMEZONE
    except (ZoneInfoNotFoundError, ValueError):
        local = datetime.now().astimezone()
        return local, local.tzname() or "heure de ce PC"


def clock_message(now: datetime | None = None) -> dict:
    """Message système : la date et l'heure du jour, dans le fuseau de l'utilisateur."""
    zone = TIMEZONE
    if now is None:
        now, zone = user_now()
    return {
        "role": "system",
        "content": f"Date et heure actuelles : {DAYS[now.weekday()]} {now:%d/%m/%Y %H:%M} ({zone}). "
        "Pour toute question sur la date ou l'heure, répondez avec ces valeurs, sans les deviner.",
    }


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    manager = shared.manager
    plan_ctx = shared.plan_ctx

    def limit_headers(sel: Selection) -> dict:
        return holder["gw"].limiter.headers(sel.plan.key, sel.plan.limits)

    @router.post("/v1/compress")
    async def compress_endpoint(request: Request):
        """Prévisualise la compression d'un historique sans appeler le modèle de réponse."""
        body = await json_object(request)
        validate_chat(body)
        limit, reserve = body.get("max_context_tokens"), body.get("reserve", 0)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1024:
            return _err(400, "'max_context_tokens' (entier >= 1024) est requis.")
        if not isinstance(reserve, int) or isinstance(reserve, bool) or not 0 <= reserve < limit:
            return _err(400, "'reserve' doit être un entier entre 0 et max_context_tokens.")
        ctx = plan_ctx(body, request)
        msgs, report = await compress_history(
            holder["gw"],
            ctx,
            holder["gw"].summaries,
            body["messages"],
            limit,
            reserve,
            normalize_compression(body.get("compression")),
        )
        return {"messages": msgs, "compression": report}

    @router.post("/v1/compact")
    async def compact_endpoint(request: Request):
        """Compacte une conversation (commande /compacte) : résumé qui remplace l'historique envoyé à l'IA."""
        body = await json_object(request)
        validate_chat(body)
        focus = body.get("instructions")
        if focus is not None and not isinstance(focus, str):
            return _err(400, "'instructions' doit être un texte.")
        ctx = plan_ctx(body, request)  # « private » : résumé par une IA locale seulement
        try:
            return await compact(holder["gw"], ctx, body["messages"], focus)
        except CompressionError as e:
            return _err(400, str(e))
        except RoutingError as e:
            return _err(e.status, f"Compactage impossible : {e.message}")
        except ProviderError as e:  # jamais le code du fournisseur (un 401 amont déconnecterait la page)
            return _err(502, f"Compactage impossible : {e.message}")

    @router.get("/v1/cache")
    async def cache_stats(request: Request):
        return holder["gw"].cache.stats(plan_ctx({}, request).identity)

    @router.delete("/v1/cache")
    async def cache_purge(request: Request):
        return {"purged": holder["gw"].cache.purge(plan_ctx({}, request).identity)}

    async def inject_documents(body: dict, ctx: PlanContext, doc_ids, budget) -> dict:
        """Ajoute le contenu des documents demandés en message système (tout, ou les extraits
        les plus pertinents pour le dernier message utilisateur si le budget est dépassé)."""
        if isinstance(doc_ids, str):
            doc_ids = [doc_ids]
        try:
            budget = int(budget or os.getenv("DOCUMENT_CONTEXT_TOKENS", "6000"))
        except (TypeError, ValueError):
            raise DocumentError(400, "documents_budget doit être un entier (tokens).")
        store: DocumentStore = holder["store"]
        pool = []
        for order, doc_id in enumerate(doc_ids):
            chunks = store.chunks(ctx.identity, doc_id)
            if chunks is None:
                raise DocumentError(404, f"Document '{doc_id}' introuvable.")
            pool += [{**c, "doc_order": order} for c in chunks]
        query = next(
            (_text_and_images(m.get("content"))[0] for m in reversed(body["messages"]) if m.get("role") == "user"), ""
        )
        # trop long pour tout fournir : extraits par mots-clés ET par le sens (17.1), si vectorisés
        semantic = None if fits(pool, budget) else await holder["vectors"].semantic_order(pool, query)
        picked, truncated = select_context(pool, query, budget, semantic)
        msgs = body["messages"]
        i = 0
        while i < len(msgs) and msgs[i].get("role") in ("system", "developer"):
            i += 1
        body["messages"] = msgs[:i] + [{"role": "system", "content": context_message(picked)}] + msgs[i:]
        return {
            "ids": list(doc_ids),
            "chunks_used": len(picked),
            "chunks_total": len(pool),
            "tokens": sum(c["tokens"] for c in picked),
            "truncated": truncated,
            "sources": sorted({(c["filename"], c["index"]) for c in picked}),
            "retrieval": "hybrid" if semantic else "bm25" if truncated else "full",
        }

    async def prepare(body: dict, request: Request, reserve: bool = True) -> Selection:
        """Extrait les options gateway (body ou en-têtes X-Gateway-*) et sélectionne le modèle.
        `reserve` : la consommation estimée est réservée (10.4) ; l'appelant DOIT exécuter la sélection
        (execute / execute_stream la libèrent) — False pour une simple simulation (/v1/route)."""
        ctx = plan_ctx(body, request)
        holder["gw"].limiter.hit_request(ctx.key, ctx.limits)  # RPM, avant tout travail (4.3)
        mode = body.pop("mode", None) or request.headers.get("x-gateway-mode")
        effort = body.pop("effort", None) or request.headers.get("x-gateway-effort")
        orch = body.pop("orchestration", None) or request.headers.get("x-gateway-orchestration")
        fallbacks = body.pop("fallbacks", None) or request.headers.get("x-gateway-fallbacks")
        if isinstance(fallbacks, str):
            fallbacks = [f.strip() for f in fallbacks.split(",") if f.strip()]
        doc_ids, doc_budget = body.pop("documents", None), body.pop("documents_budget", None)
        if isinstance(doc_ids, str):
            doc_ids = [doc_ids]
        store: DocumentStore = holder["store"]

        # ---- projet / workspace (3.2) : défauts, prompt système, documents, fenêtre ----
        project_id = (
            body.pop("project", None) or request.headers.get("x-gateway-project") or (ctx.key or {}).get("project_id")
        )  # projet par défaut de la clé (4.1)
        prompt_id, prompt_vars = body.pop("prompt_id", None), body.pop("prompt_vars", None) or {}
        project = None
        if project_id:
            project = store.get_project(ctx.identity, project_id)
            if not project:
                raise WorkspaceError(404, f"Projet '{project_id}' introuvable.")
            mode = mode or project["mode"]
            effort = effort or project["effort"]
            body["model"] = body.get("model") or project["model"] or "auto"
            doc_ids = list(dict.fromkeys(project["documents"] + (doc_ids or [])))
            doc_budget = doc_budget or project["documents_budget"]
            if project["max_output_tokens"] and not (body.get("max_tokens") or body.get("max_completion_tokens")):
                body["max_tokens"] = project["max_output_tokens"]
        if not body.get("model"):
            raise WorkspaceError(400, "'model' est requis (ou un projet avec un modèle par défaut).")

        system_text, used_prompt = None, None
        pname = project["name"] if project else None
        pvars = {**(project["prompt_vars"] if project else {}), **prompt_vars}
        if prompt_id or (project and project["prompt_id"]):
            used_prompt = prompt_id or project["prompt_id"]
            p = store.get_prompt(ctx.identity, used_prompt)
            if not p:
                raise WorkspaceError(404, f"Prompt '{used_prompt}' introuvable.")
            system_text = render(p["content"], pvars, pname)
        elif project and project["system_prompt"]:
            system_text = render(project["system_prompt"], pvars, pname)
        try:  # compétences (sélecteur, /competence) : après le prompt du projet, avant les messages de la requête
            skill_ids = skills.normalize(body.pop("skills", None) or request.headers.get("x-gateway-skills"))
        except skills.SkillError as e:
            raise WorkspaceError(400, str(e))
        if skill_ids:
            body["messages"] = [skills.system_message(skill_ids)] + body["messages"]
        if system_text:  # en tête : les messages système de la requête peuvent le préciser
            body["messages"] = [{"role": "system", "content": system_text}] + body["messages"]
        last_ask = next(
            (_text_and_images(m.get("content"))[0] for m in reversed(body["messages"]) if m.get("role") == "user"), ""
        )
        if TIME_QUESTION.search(last_ask):  # sans la date, le modèle la devine (et se trompe)
            body["messages"] = [clock_message()] + body["messages"]

        # fenêtre de contexte : requête > projet ; compression de l'historique (3.4)
        limit = body.pop("max_context_tokens", None) or (project["max_context_tokens"] if project else None)
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1024):
            raise WorkspaceError(400, "'max_context_tokens' doit être un entier >= 1024.")
        comp_mode = normalize_compression(
            body.pop("compression", None)
            or request.headers.get("x-gateway-compression")
            or (project["compression"] if project else None)
        )
        if limit and doc_ids:  # les documents ne prennent pas plus de la moitié de la fenêtre
            doc_budget = min(int(doc_budget or os.getenv("DOCUMENT_CONTEXT_TOKENS", "6000")), limit // 2)
        context = await inject_documents(body, ctx, doc_ids, doc_budget) if doc_ids else None
        compression = None
        if limit:
            reserve = min(body.get("max_tokens") or body.get("max_completion_tokens") or 0, limit // 2)
            body["messages"], compression = await compress_history(
                holder["gw"], ctx, holder["gw"].summaries, body["messages"], limit, reserve, comp_mode
            )
        dropped = (compression or {}).get("trimmed_messages", 0)

        caching = {
            "policy": normalize_policy(body.pop("prompt_cache", None) or request.headers.get("x-gateway-prompt-cache")),
            "key": cache_key(ctx.identity, project["id"] if project else None),
            "ttl": response_ttl(body.pop("cache", None) if "cache" in body else request.headers.get("x-gateway-cache")),
        }
        split = splitter.normalize(body.pop("split", None) or request.headers.get("x-gateway-split"))
        sel = await holder["gw"].select(body, ctx, mode, effort, orch, fallbacks, caching, split)
        sel.context = context
        sel.compression = compression
        # TPM / TPD / budget : estimation a priori de la requête (4.3), réservée jusqu'à
        # l'enregistrement de l'usage réel pour que des requêtes simultanées ne dépassent pas ensemble (10.4).
        # Contrôle et réservation sans `await` entre eux : aucune autre requête ne peut s'intercaler.
        est_out = sel.cls.max_output_tokens or 500
        agents = 3 if sel.orchestrate else 1
        est_tokens = (
            sel.split.estimate(est_out)
            if sel.split  # tous les appels du découpage (13.4)
            else (sel.cls.prompt_tokens + est_out) * agents
        )
        res = Reservation() if reserve else None
        try:
            if ctx.limits:
                est_cost = (
                    sel.info.cost_usd(est_tokens - est_out, est_out)
                    if sel.split
                    else sel.info.cost_usd(sel.cls.prompt_tokens, est_out) * agents
                )
                holder["gw"].limiter.check_usage(ctx.key, ctx.limits, sel.cls.prompt_tokens, est_cost, res, est_tokens)
        except BaseException:
            if res:
                res.release()
            raise
        sel.reservation = res
        if project or used_prompt:
            sel.project = {
                "id": project["id"] if project else None,
                "name": pname,
                "prompt_id": used_prompt,
                "max_context_tokens": limit,
                "trimmed_messages": dropped,
            }
        return sel

    @router.get("/v1/plan")
    async def plan(request: Request):
        ctx = plan_ctx({}, request)
        return {"plan": ctx.plan, "features": FEATURES[ctx.plan]}

    @router.get("/v1/providers")
    async def providers(request: Request):
        """État des fournisseurs, réservé aux comptes (9.3) : il révèle les adresses internes des serveurs
        locaux. Servi depuis le cache de détection (DETECT_TTL) : ne déclenche pas de sonde à chaque appel."""
        ctx = manager(request)
        owner = ctx.identity
        status = await holder["gw"].detect()
        out = {n: s for n, s in status.items() if holder["gw"].visible(n, owner)}  # ponts d'autrui masqués
        if not ctx.is_admin:  # autres comptes : ni les adresses du réseau de la maison, ni les erreurs détaillées
            out = {
                n: {
                    **{k: v for k, v in s.items() if k not in ("base_url", "error")},
                    **({"base_url": s.get("base_url")} if holder["gw"].providers[n].cfg.owner == owner else {}),
                }
                for n, s in out.items()
            }
        return out

    def meta(info) -> dict:
        """Métadonnées d'un modèle ; « chat » : faux pour les embeddings, la voix, la dictée… (liste du chat)."""
        chat = not any(k in info.id.lower() for k in NON_CHAT)
        return {**info.to_dict(), "chat": chat}

    @router.get("/v1/skills")
    async def skills_list(request: Request):
        """Compétences disponibles (sans leur texte) ; le texte d'une compétence : /v1/skills/{id}."""
        plan_ctx({}, request)  # clé valide requise, comme /v1/models
        return {
            "object": "list",
            "data": skills.catalog(),
            "max_active": skills.MAX_ACTIVE,
            "catalog_url": skills.CATALOG_URL,
        }

    @router.get("/v1/skills/{skill_id}")
    async def skill_detail(skill_id: str, request: Request):
        plan_ctx({}, request)
        skill = skills.load().get(skill_id)
        if not skill:
            return _err(404, f"Compétence '{skill_id}' introuvable.")
        return {**skill.to_dict(), "content": skill.content}

    @router.get("/v1/models")
    async def models(request: Request):
        gw: Gateway = holder["gw"]
        ctx = plan_ctx({}, request)
        perms = ctx.permissions  # une clé ne voit que ses modèles permis
        data, seen = [], set()
        for name, ids in gw.discovered.items():
            if not gw.visible(name, ctx.identity):
                continue
            p = gw.providers[name]
            for mid in ids:
                info = gw.registry.get(name, mid, p.hosting)
                seen.add(info.ref)
                if model_allowed(perms, info):
                    data.append(
                        {
                            "id": info.ref,
                            "object": "model",
                            "owned_by": name,
                            "metadata": meta(info),
                        }
                    )
        for info in gw.registry.all():  # modèles cloud connus dont le provider est actif
            p = gw.providers.get(info.provider)
            if p and p.cfg.enabled and info.ref not in seen and model_allowed(perms, info):
                data.append(
                    {
                        "id": info.ref,
                        "object": "model",
                        "owned_by": info.provider,
                        "metadata": meta(info),
                    }
                )
        return {"object": "list", "data": data}

    @router.post("/v1/chat/completions")
    async def chat(request: Request):
        gw: Gateway = holder["gw"]
        body = await json_object(request)
        validate_chat(body)
        request.state.gw_model = body.get("model")
        body.pop("_num_ctx", None)  # directive interne (contrôle du PC), jamais celle d'un client
        if "gateway_tools" in body:
            return await with_tools(body, request, body.pop("gateway_tools"))
        sel = await prepare(body, request)
        sel.endpoint, sel.stream = "chat", bool(body.get("stream"))
        if body.get("stream"):
            chunks = await _primed(gw.execute_stream(sel, body))
            return StreamingResponse(_sse_openai(chunks), media_type="text/event-stream", headers=limit_headers(sel))
        resp = await gw.execute(sel, body)
        return JSONResponse(resp, headers=limit_headers(sel))

    async def with_tools(body: dict, request: Request, wanted) -> JSONResponse | StreamingResponse:
        """Outils exécutés par la gateway (21 à 26) : chaque tour du modèle est une requête routée normale
        (quotas, limites, mode Privé, modèle imposé), les outils tournent ici entre deux tours."""
        ctx = plan_ctx(body, request)
        allowed = tools.available(ctx)
        if wanted is not True and wanted is not False:
            if not isinstance(wanted, list) or not all(isinstance(n, str) for n in wanted):
                raise RequestError("'gateway_tools' : true ou liste de noms d'outils.")
            allowed = {n: t for n, t in allowed.items() if n in wanted}
        if body.get("tools"):
            raise RequestError("'gateway_tools' (outils de la gateway) et 'tools' (les vôtres) ne se combinent pas.")
        if not wanted or not allowed:
            raise RequestError(
                "Outils non permis pour cette clé (clé maître, ou permission « Outils » de la clé).", 403
            )
        if ctx.is_admin and any(t.level in ("pc", "mcp", "docs") for t in allowed.values()):
            shared.admin_session(request)  # PC, MCP et sources de documents : double authentification depuis Internet
        stream = bool(body.pop("stream", False))
        state = None
        max_steps = int(os.getenv("TOOLS_MAX_STEPS", "12"))
        if any(t.level == "pc" for t in allowed.values()):
            # préférences de l'utilisateur (/retiens) et guide des applications de ce PC (index + celles de la demande)
            store, owner = holder.get("store"), tools.actor(ctx)
            task = next(
                (_text_and_images(m.get("content"))[0] for m in reversed(body["messages"]) if m.get("role") == "user"),
                "",
            )
            state = TaskState(task, max_steps)  # objectif, plan, actions faites, limites (24.2)
            extras = [memory.guide(memory.recall(store, owner)) if store else "", app_guides.guide(task)]
            extra = "\n\n".join(e for e in extras if e)
            # méthode guidée : sans elle, les petits modèles agissent au hasard
            guide = tools.PC_GUIDE + (f"\n\n{extra}" if extra else "")
            body["messages"] = [{"role": "system", "content": guide}] + body["messages"]
        if any(t.level == "pc" for t in allowed.values()):
            body["_num_ctx"] = runner.PC_CONTEXT  # Ollama : fenêtre agrandie pour cette tâche seulement
        base = copy.deepcopy(body)
        # mode Privé : lu (et retiré du corps) par plan_ctx ci-dessus, redemandé à chaque tour du modèle. Écran
        # capturé : la suite de la tâche reste aussi sur les IA locales (routage « auto », secours compris).
        private = {"on": bool((ctx.permissions or {}).get("private"))}

        def watched(t: tools.Tool) -> tools.Tool:
            async def run(args: dict) -> tools.ToolResult:
                result = await t.run(args)
                if result.image or t.local_only:  # écran, document, outil MCP « local_only » : jamais au cloud
                    private["on"] = True
                return result

            return dataclasses.replace(t, run=run)

        # avant : seul l'écran capturé passait la suite en privé ; un document lu (Paperless, Nextcloud) partait au
        # tour suivant vers un modèle cloud si le routage « auto » en choisissait un
        allowed = {n: watched(t) if t.level == "pc" or t.local_only else t for n, t in allowed.items()}

        last_text = next(
            (_text_and_images(m.get("content"))[0] for m in reversed(body["messages"]) if m.get("role") == "user"), ""
        )
        all_specs = select_specs(list(allowed.values()), last_text)
        mode = {"emulated": False}  # modèle imposé sans appel d'outils natif (gemma3…) : outils décrits en texte
        has_mcp = any(t.level == "mcp" for t in allowed.values())

        async def call(msgs: list, specs: list | None) -> tuple[dict, str]:
            sel = None
            extra = {"private": True} if private["on"] else {}
            if not mode["emulated"]:
                b = {**copy.deepcopy(base), **extra, "messages": list(msgs), **({"tools": specs} if specs else {})}
                try:
                    sel = await prepare(b, request)
                except RequestError as e:
                    if "function calling" not in e.message:
                        raise
                    if has_mcp:  # outils décrits en texte : le modèle ne les appelle pas, il invente (constaté)
                        raise RequestError(
                            "Ce modèle n'appelle pas les outils MCP de façon fiable : choisissez un modèle qui les "
                            "appelle (qwen2.5:14b, qwen2.5-coder:14b…) ou « auto ».",
                            400,
                        )
                    mode["emulated"] = True
            if sel is None:
                b = {**copy.deepcopy(base), **extra, "messages": runner.emulated(msgs, all_specs if specs else [])}
                sel = await prepare(b, request)
            sel.endpoint = "chat"
            resp = await holder["gw"].execute(sel, b)
            return (runner.parse_emulated(resp, allowed) if mode["emulated"] else resp), sel.info.hosting

        def audit(name: str, args: dict, decision: str, result: str | None = None, image: str | None = None) -> None:
            """Journal d'audit (21.4) : décision, résultat et capture d'écran chiffrée (purgée après
            AUDIT_CAPTURE_DAYS jours, rien gardé à 0)."""
            store = holder.get("store")
            if store is None:
                return
            kind, actor = describe_actor(ctx, getattr(request.state, "admin_session_id", None))
            detail = {"action": decision, "arguments": json.dumps(args, ensure_ascii=False)[:300]}
            if result is not None:
                detail["résultat"] = " ".join(result.split())[:300]
            at = time.time()
            audit_id = store.add_audit(
                {
                    "at": at,
                    "actor_kind": kind,
                    "actor": actor,
                    "owner": ctx.identity,
                    "ip": shared.client_ip(request),
                    "action": "outil",
                    "target": name,
                    "status": 200,
                    "detail": detail,
                }
            )
            cipher = holder.get("cipher")
            if image and cipher is not None and capture_days() > 0:
                store.add_capture(audit_id, at, cipher.encrypt(image, f"audit-capture:{audit_id}"))

        chunks = runner.run(
            call=call,
            tools=allowed,
            messages=body["messages"],
            approvals=runner.APPROVALS,
            identity=tools.actor(ctx),
            audit=audit,
            max_steps=max_steps,
            failsafe=tools.FAILSAFE,
            state=state,
        )
        if state is not None:  # la tâche elle-même au journal : objectif, puis réponse finale (21.4)
            audit("tâche", {"objectif": state.goal}, "début")
            chunks = _on_end(chunks, lambda text: audit("tâche", {"objectif": state.goal}, "fin", text or "(arrêtée)"))
        if stream:
            return StreamingResponse(_sse_openai(await _primed(chunks)), media_type="text/event-stream")
        text, out = "", {}
        async for c in chunks:
            for choice in c.get("choices") or []:
                text += (choice.get("delta") or {}).get("content") or ""
            out.update({k: c[k] for k in ("usage", "x_gateway") if k in c})
        return JSONResponse(
            {
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                **out,
            }
        )

    @router.get("/v1/tools")
    async def list_tools(request: Request):
        """Outils de la gateway permis pour ce compte (à activer avec « gateway_tools »)."""
        ctx = plan_ctx({}, request)
        allowed = tools.available(ctx)

        def row(t) -> dict:
            base = {"name": t.name, "description": t.description, "level": t.level, "confirm": t.confirm}
            origin = mcp.ORIGIN.get(t.name)
            if not origin:
                return base
            catalog = mcp_help.catalog_id(getattr(mcp.SERVERS.get(origin[0]), "cfg", None))  # textes de son paquet
            return {
                **base,
                "server": origin[0],
                "tool": origin[1],
                "help": mcp_help.tool_help(origin[1], t.description, catalog),
            }

        rows = [row(t) for t in allowed.values()]
        count = Counter(r["server"] for r in rows if "server" in r)
        servers = [  # clé virtuelle : ses serveurs permis seulement, et jamais le détail d'une erreur (chemins de ce PC)
            {
                **mcp_help.server_view(s["name"]),
                "connected": s["ok"],
                "tools": count[s["name"]],
                **({"error": s["error"]} if ctx.is_admin and s["error"] else {}),
            }
            for s in mcp.overview()
            if ctx.is_admin or count[s["name"]]
        ]
        return {"object": "list", "data": rows, "mcp_servers": servers}

    def pc_user(request: Request):
        """Clé maître (double authentification depuis Internet) ou clé virtuelle permise sur le contrôle du PC."""
        ctx = plan_ctx({}, request)
        if ctx.is_admin:
            return shared.admin(request)
        if not any(t.level == "pc" for t in tools.available(ctx).values()):
            raise RequestError("Contrôle du PC non permis pour cette clé.", 403)
        return ctx

    @router.post("/v1/tools/approvals/{approval_id}")
    async def decide(approval_id: str, request: Request):
        """Autoriser / refuser une action en attente (contrôle du PC : clé maître + double authentification)."""
        ctx = pc_user(request)
        data = await json_object(request)
        if not isinstance(data.get("approve"), bool):
            raise RequestError("'approve' : true (autoriser) ou false (refuser).")
        if not runner.APPROVALS.decide(approval_id, tools.actor(ctx), data["approve"]):
            raise RequestError("Validation inconnue ou expirée.", 404)
        return {"approved": data["approve"]}

    # ---------- mémoire du contrôle du PC : préférences et leçons (commandes /memoire, /retiens, /oublie) ----------
    def memory_owner(request: Request) -> tuple[DocumentStore, str]:
        ctx = pc_user(request)
        store = holder.get("store")
        if store is None:
            raise RequestError("Mémoire indisponible : aucune base de données.", 503)
        return store, tools.actor(ctx)

    @router.get("/v1/tools/memory")
    async def list_memory(request: Request):
        """Préférences dictées (/retiens), anciennes leçons, et applications du guide de ce PC (lu, jamais écrit,
        par le modèle)."""
        store, owner = memory_owner(request)
        apps = [{"name": a["name"], "role": a.get("role"), "usage": a.get("usage")} for a in app_guides.load()]
        return {"object": "list", "data": store.list_memory(owner), "apps": apps}

    @router.post("/v1/tools/memory")
    async def add_memory(request: Request):
        """Souvenir dicté par l'utilisateur ({"kind": "preference" | "lesson", "text": …})."""
        store, owner = memory_owner(request)
        data = await json_object(request)
        try:
            text = memory.clean(data.get("kind", "preference"), data.get("text"))
        except memory.InvalidMemory as e:
            raise RequestError(str(e)) from None
        return store.add_memory(owner, data.get("kind", "preference"), text, "user", memory.KEEP)

    @router.delete("/v1/tools/memory/{memory_id}")
    async def delete_memory(memory_id: int, request: Request):
        store, owner = memory_owner(request)
        if not store.delete_memory(owner, memory_id):
            raise RequestError("Souvenir introuvable.", 404)
        return {"deleted": 1}

    @router.delete("/v1/tools/memory")
    async def clear_memory(request: Request):
        store, owner = memory_owner(request)
        return {"deleted": store.delete_memory(owner)}

    @router.post("/v1/route")
    async def route_endpoint(request: Request):
        """Simule le routage sans appeler de modèle (debug / dashboard)."""
        gw: Gateway = holder["gw"]
        body = await json_object(request)
        validate_chat(body)
        if not (body.get("project") or request.headers.get("x-gateway-project")):
            body.setdefault("model", "auto")
        sel = await prepare(body, request, reserve=False)
        out = {
            "plan": sel.plan.plan,
            "classification": sel.cls.to_dict(),
            "effort": sel.effort,
            "routing": sel.decision.to_dict() if sel.decision else None,
            "upstream": {
                "provider": sel.provider.name,
                "model": sel.upstream,
                "reasoning_effort": sel.payload.get("reasoning_effort"),
                "thinking": sel.payload.get("thinking"),
                "cot_prompt": sel.payload["messages"][0].get("role") == "system"
                and "étape par étape" in str(sel.payload["messages"][0].get("content")),
            },
            "fallbacks": [m.ref for m in sel.fallbacks],
            "documents": sel.context,
            "project": sel.project,
            "messages_sent": len(sel.payload["messages"]),
            "orchestration": None,
            "split": sel.split.to_dict() if sel.split else None,
        }
        if sel.orchestrate:
            out["orchestration"] = orchestrator.preview(gw, sel)
            if crew.applies(sel.info):  # gros modèle : les experts sont des agents CrewAI
                out["orchestration"]["strategy"] = crew.STRATEGY
        return out

    @router.post("/v1/classify")
    async def classify_endpoint(request: Request):
        body = await json_object(request)
        validate_chat(body)
        return classify(body).to_dict()

    @router.post("/v1/messages")
    async def messages(request: Request):
        gw: Gateway = holder["gw"]
        body = await json_object(request)
        validate_messages_body(body)
        thinking = body.get("thinking")
        if thinking is not None and not (
            isinstance(thinking, dict)
            and (
                thinking.get("type") != "enabled"
                or (isinstance(thinking.get("budget_tokens"), int) and thinking["budget_tokens"] > 0)
            )
        ):
            raise RequestError('\'thinking\' : {"type": "enabled", "budget_tokens": entier > 0} attendu.')
        if not body.get("effort") and (thinking or {}).get("type") == "enabled":
            body["effort"] = "high"  # client Anthropic demandant l'extended thinking
        oa_body = translate.anthropic_to_openai_request(body)
        for k in (
            "plan",
            "mode",
            "effort",
            "orchestration",
            "fallbacks",
            "documents",
            "documents_budget",
            "project",
            "prompt_id",
            "prompt_vars",
            "prompt_cache",
            "cache",
            "compression",
            "split",
            "max_context_tokens",
        ):  # options gateway conservées
            if k in body:
                oa_body[k] = body[k]
        request.state.gw_model = body.get("model")
        sel = await prepare(oa_body, request)
        sel.endpoint, sel.stream = "messages", bool(body.get("stream"))
        model_name = body.get("model") or oa_body["model"]
        if body.get("stream"):
            chunks = await _primed(gw.execute_stream(sel, oa_body))
            return StreamingResponse(
                translate.openai_stream_to_anthropic_sse(_guard(chunks), model_name),
                media_type="text/event-stream",
                headers=limit_headers(sel),
            )
        resp = await gw.execute(sel, oa_body)
        out = translate.openai_to_anthropic_response(resp, model_name)
        out["x_gateway"] = resp["x_gateway"]
        return JSONResponse(out, headers=limit_headers(sel))

    return router


async def _primed(chunks: AsyncIterator[dict]) -> AsyncIterator[dict]:
    """Attend le 1er chunk avant d'ouvrir la réponse HTTP : si tous les modèles échouent
    (fallback épuisé), l'erreur remonte avec son vrai code HTTP au lieu d'un 200."""
    try:
        first = await chunks.__anext__()
    except StopAsyncIteration:
        first = None

    async def rest():
        if first is not None:
            yield first
            async for c in chunks:
                yield c

    return rest()


async def _on_end(chunks: AsyncIterator[dict], done) -> AsyncIterator[dict]:
    """Laisse passer les chunks ; `done(texte de la réponse)` à la fin, même interrompue (Stop, déconnexion)."""
    text = ""
    try:
        async for c in chunks:
            for choice in c.get("choices") or []:
                text += (choice.get("delta") or {}).get("content") or ""
            yield c
    finally:
        done(text)


async def _guard(chunks):
    """Laisse passer les chunks ; une erreur en cours de flux termine proprement."""
    try:
        async for c in chunks:
            yield c
    except (ProviderError, PlanError):
        return


async def _sse_openai(chunks: AsyncIterator[dict]):
    try:
        async for chunk in chunks:
            yield f"data: {json.dumps(chunk)}\n\n"
    except (ProviderError, PlanError) as e:
        err = {"message": e.message, "code": e.status, "provider": getattr(e, "provider", "plan")}
        yield f"data: {json.dumps({'error': err})}\n\n"
    yield "data: [DONE]\n\n"
