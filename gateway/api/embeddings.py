"""Embeddings (16.1) : POST /v1/embeddings au format OpenAI, modèles d'embeddings utilisables par la clé, et
catégorie « Embeddings » de l'administration (le modèle retenu pour ce PC, fixé ou automatique)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

import asyncio
import os

from ..embedding_choice import language
from ..embeddings import PATH, candidates, embed, price_per_mtok, usable
from ..model_catalog import budget
from ..plans import PlanContext
from ..validation import RequestError, json_object
from ..vectors import current_model, mode


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    plan_ctx = shared.plan_ctx
    admin = shared.admin

    @router.post(PATH)
    async def embeddings(request: Request):
        """input (texte, liste de textes ou de tokens), model (« auto », fournisseur/modèle ou nom du modèle),
        encoding_format (float, base64), dimensions."""
        gw = holder["gw"]
        body = await json_object(request)
        ctx = plan_ctx(body, request)
        gw.limiter.hit_request(ctx.key, ctx.limits)  # RPM de la clé (4.3)
        request.state.gw_model = body.get("model")
        out = await embed(gw, ctx, body)
        meta = out["x_gateway"]
        return JSONResponse(
            out,
            headers={
                **gw.limiter.headers(ctx.key, ctx.limits),
                "X-Gateway-Provider": meta["provider"],
                "X-Gateway-Model": meta["model"],
                "X-Gateway-Cost-EUR": f"{meta['cost_eur']:.8f}",
            },
        )

    @router.get("/v1/embeddings/models")
    async def embedding_models(request: Request):
        """Modèles d'embeddings que cette clé peut utiliser, dans l'ordre de « auto » (le premier)."""
        gw = holder["gw"]
        ctx = plan_ctx({}, request)
        if not gw.discovered:
            await gw.detect()
        if (ctx.permissions or {}).get("embeddings") is False:
            return {"object": "list", "data": []}
        return {
            "object": "list",
            "data": [
                {"id": m.ref, "hosting": m.hosting, "price_per_mtok_usd": price_per_mtok(m)}
                for m in gw.embedding_choice.rank(usable(gw, ctx))
            ],
        }

    async def overview(ctx: PlanContext) -> dict:
        gw = holder["gw"]
        choice = gw.embedding_choice
        if not gw.discovered:
            await gw.detect()
        hw = await gw.hardware.snapshot(list(gw.providers.values()), gw.client)
        choice.budget = budget(hw)
        models = choice.rank(candidates(gw, ctx))
        used = current_model(gw)
        todo = len(await asyncio.to_thread(holder["store"].documents_to_vectorize, used.ref)) if used else 0
        env_docs = os.getenv("DOCUMENT_EMBEDDINGS_MODEL", "").strip() or None
        env_api = os.getenv("EMBEDDINGS_DEFAULT_MODEL", "").strip() or None
        return {
            "mode": "env" if env_docs else "pinned" if choice.pinned else "auto",
            "pinned": choice.pinned,
            "env": {"documents": env_docs, "api": env_api},
            "documents_embeddings": mode(),  # local, cloud ou off (DOCUMENT_EMBEDDINGS)
            "language": language(),
            "chosen": used.ref if used else None,
            "documents_to_vectorize": todo,
            "gpu": choice.budget.get("gpu"),
            "models": [
                {
                    "ref": m.ref,
                    "provider": m.provider,
                    "model": m.id,
                    "hosting": m.hosting,
                    "this_pc": choice.this_pc(m.provider),
                    "available": gw.health.available(m.ref),
                    "chosen": bool(used and used.ref == m.ref),
                    **choice.assess(m),
                }
                for m in models
            ],
        }

    @router.get("/v1/admin/embeddings")
    async def admin_embeddings(request: Request):
        """Catégorie « Embeddings » : tous les modèles détectés, classés (qualité, place sur ce PC), celui retenu."""
        admin(request)
        return await overview(plan_ctx({}, request))

    @router.put("/v1/admin/embeddings")
    async def admin_embeddings_choice(request: Request):
        """{"model": "ollama/bge-m3:latest"} fixe le modèle ; {"model": null} : choix automatique. Les documents
        sont revectorisés en tâche de fond par le nouveau modèle."""
        admin(request)
        body = await json_object(request)
        if set(body) != {"model"}:
            raise RequestError('Corps attendu : {"model": "fournisseur/modèle"} ou {"model": null}.')
        ref = body["model"]
        gw = holder["gw"]
        ctx = plan_ctx({}, request)
        if ref is not None:
            if not isinstance(ref, str) or ref not in {m.ref for m in candidates(gw, ctx)}:
                raise RequestError(f"'{ref}' n'est pas un modèle d'embeddings détecté.", 404)
        gw.embedding_choice.set_pinned(ref)
        holder["vectors"].refresh()
        request.state.audit = {"target": ref or "auto"}
        return await overview(ctx)

    return router
