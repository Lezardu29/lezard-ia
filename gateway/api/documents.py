"""Prompts, projets (3.2) et documents (3.1)."""

from __future__ import annotations

import asyncio
import base64
import binascii
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request

from ..documents import (
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DocumentError,
    bm25_rank,
    chunk_document,
    hybrid_order,
)
from ..plans import PlanContext
from ..validation import json_object
from ..workspaces import WorkspaceError, render, validate_project, validate_prompt


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    plan_ctx = shared.plan_ctx

    # ----- prompts & projets (3.2) -----
    def _owner(request: Request) -> str:
        return plan_ctx({}, request).identity

    def _check_refs(owner: str, data: dict) -> None:
        if data.get("prompt_id") and not holder["store"].get_prompt(owner, data["prompt_id"]):
            raise WorkspaceError(404, f"Prompt '{data['prompt_id']}' introuvable.")
        for d in data.get("documents") or []:
            if not holder["store"].get(owner, d):
                raise WorkspaceError(404, f"Document '{d}' introuvable.")
        if data.get("prompt_id") and data.get("system_prompt"):
            raise WorkspaceError(400, "Choisir 'system_prompt' OU 'prompt_id', pas les deux.")

    @router.post("/v1/prompts", status_code=201)
    async def create_prompt(request: Request):
        return holder["store"].add_prompt(_owner(request), validate_prompt(await json_object(request)))

    @router.get("/v1/prompts")
    async def list_prompts(request: Request):
        return {"object": "list", "data": holder["store"].list_prompts(_owner(request))}

    def _prompt_or_404(owner: str, prompt_id: str) -> dict:
        p = holder["store"].get_prompt(owner, prompt_id)
        if not p:
            raise WorkspaceError(404, f"Prompt '{prompt_id}' introuvable.")
        return p

    @router.get("/v1/prompts/{prompt_id}")
    async def get_prompt(prompt_id: str, request: Request):
        return _prompt_or_404(_owner(request), prompt_id)

    @router.patch("/v1/prompts/{prompt_id}")
    async def update_prompt(prompt_id: str, request: Request):
        owner = _owner(request)
        _prompt_or_404(owner, prompt_id)
        return holder["store"].update_prompt(
            owner, prompt_id, validate_prompt(await json_object(request), partial=True)
        )

    @router.post("/v1/prompts/{prompt_id}/render")
    async def render_prompt(prompt_id: str, request: Request):
        p = _prompt_or_404(_owner(request), prompt_id)
        body = await json_object(request)
        return {"id": prompt_id, "content": render(p["content"], body.get("variables"), body.get("project"))}

    @router.delete("/v1/prompts/{prompt_id}")
    async def delete_prompt(prompt_id: str, request: Request):
        owner = _owner(request)
        _prompt_or_404(owner, prompt_id)
        users = holder["store"].prompt_users(owner, prompt_id)
        if users:
            raise WorkspaceError(409, f"Prompt utilisé par les projets : {', '.join(users)}.")
        holder["store"].delete_prompt(owner, prompt_id)
        return {"id": prompt_id, "deleted": True}

    @router.post("/v1/projects", status_code=201)
    async def create_project(request: Request):
        owner = _owner(request)
        data = validate_project(await json_object(request))
        _check_refs(owner, data)
        return holder["store"].add_project(owner, data)

    @router.get("/v1/projects")
    async def list_projects(request: Request):
        return {"object": "list", "data": holder["store"].list_projects(_owner(request))}

    def _project_or_404(owner: str, project_id: str) -> dict:
        p = holder["store"].get_project(owner, project_id)
        if not p:
            raise WorkspaceError(404, f"Projet '{project_id}' introuvable.")
        return p

    @router.get("/v1/projects/{project_id}")
    async def get_project(project_id: str, request: Request):
        return _project_or_404(_owner(request), project_id)

    @router.patch("/v1/projects/{project_id}")
    async def update_project(project_id: str, request: Request):
        owner = _owner(request)
        current = _project_or_404(owner, project_id)
        data = validate_project(await json_object(request), partial=True)
        # choisir l'un remplace l'autre
        if data.get("prompt_id") and "system_prompt" not in data:
            data["system_prompt"] = None
        if data.get("system_prompt") and "prompt_id" not in data:
            data["prompt_id"] = None
        _check_refs(owner, {**current, **data})
        return holder["store"].update_project(owner, project_id, data)

    @router.delete("/v1/projects/{project_id}")
    async def delete_project(project_id: str, request: Request):
        if not holder["store"].delete_project(_owner(request), project_id):
            raise WorkspaceError(404, f"Projet '{project_id}' introuvable.")
        return {"id": project_id, "deleted": True}

    @router.post("/v1/projects/{project_id}/documents/{doc_id}")
    async def attach_document(project_id: str, doc_id: str, request: Request):
        owner = _owner(request)
        p = _project_or_404(owner, project_id)
        _check_refs(owner, {"documents": [doc_id]})
        return holder["store"].update_project(
            owner, project_id, {"documents": list(dict.fromkeys(p["documents"] + [doc_id]))}
        )

    @router.delete("/v1/projects/{project_id}/documents/{doc_id}")
    async def detach_document(project_id: str, doc_id: str, request: Request):
        owner = _owner(request)
        p = _project_or_404(owner, project_id)
        return holder["store"].update_project(
            owner, project_id, {"documents": [d for d in p["documents"] if d != doc_id]}
        )

    # ----- documents (3.1) -----
    def _int(value, default: int, name: str) -> int:
        try:
            return int(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            raise DocumentError(400, f"{name} doit être un entier.")

    @router.post("/v1/documents", status_code=201)
    async def upload_document(request: Request):
        """Multipart (champ `file`) ou JSON {filename, content | content_base64}.
        Options : chunk_size, chunk_overlap (tokens)."""
        ctx = plan_ctx({}, request)  # taille du corps : BodyLimitMiddleware (9.1)
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            form = await request.form()
            f = form.get("file")
            if f is None or not hasattr(f, "read"):
                raise DocumentError(400, "Champ 'file' manquant.")
            data, filename = await f.read(), f.filename or "document.txt"
            opts = form
        else:
            opts = await json_object(request)
            filename = opts.get("filename")
            if not filename:
                raise DocumentError(400, "'filename' est requis.")
            if opts.get("content_base64") is not None:
                try:
                    data = base64.b64decode(opts["content_base64"], validate=True)
                except (binascii.Error, ValueError):
                    raise DocumentError(400, "content_base64 invalide.")
            elif opts.get("content") is not None:
                data = str(opts["content"]).encode("utf-8")
            else:
                raise DocumentError(400, "'content' ou 'content_base64' est requis.")
        size = _int(opts.get("chunk_size"), DEFAULT_CHUNK_TOKENS, "chunk_size")
        overlap = _int(opts.get("chunk_overlap"), min(DEFAULT_OVERLAP_TOKENS, size // 8), "chunk_overlap")
        project_id = opts.get("project")  # rattachement direct à un projet (3.2)
        project = _project_or_404(ctx.identity, project_id) if project_id else None
        kind, language, chunks = await asyncio.to_thread(chunk_document, filename, data, size, overlap)
        doc = holder["store"].add(ctx.identity, filename, kind, language, len(data), chunks, size, overlap)
        holder["vectors"].schedule(ctx.identity, doc["id"])  # recherche hybride (17.1), en tâche de fond
        doc["vectors"] = holder["vectors"].status(doc)
        if project:
            holder["store"].update_project(ctx.identity, project_id, {"documents": project["documents"] + [doc["id"]]})
            doc["project"] = project_id
        return doc

    @router.get("/v1/documents")
    async def list_documents(request: Request):
        docs = holder["store"].list(plan_ctx({}, request).identity)
        return {"object": "list", "data": [{**d, "vectors": holder["vectors"].status(d)} for d in docs]}

    def _doc_or_404(request: Request, doc_id: str) -> tuple[PlanContext, dict]:
        ctx = plan_ctx({}, request)
        doc = holder["store"].get(ctx.identity, doc_id)
        if not doc:
            raise DocumentError(404, f"Document '{doc_id}' introuvable.")
        return ctx, doc

    @router.get("/v1/documents/{doc_id}")
    async def get_document(doc_id: str, request: Request):
        doc = _doc_or_404(request, doc_id)[1]
        return {**doc, "vectors": holder["vectors"].status(doc)}

    @router.get("/v1/documents/{doc_id}/chunks")
    async def get_chunks(doc_id: str, request: Request):
        ctx, _ = _doc_or_404(request, doc_id)
        return {"object": "list", "data": holder["store"].chunks(ctx.identity, doc_id)}

    @router.post("/v1/documents/{doc_id}/search")
    async def search_document(doc_id: str, request: Request):
        ctx, _ = _doc_or_404(request, doc_id)
        body = await json_object(request)
        if not body.get("query"):
            raise DocumentError(400, "'query' est requis.")
        top_k = _int(body.get("top_k"), 5, "top_k")
        chunks = holder["store"].chunks(ctx.identity, doc_id)
        lexical = {id(c): s for s, c in bm25_rank(body["query"], chunks)}
        # 17.1 : passages proches par le sens aussi (synonymes), si le document est vectorisé ; sinon BM25 seul
        semantic = await holder["vectors"].semantic_order([{**c, "doc_order": 0} for c in chunks], body["query"])
        ordered = hybrid_order(body["query"], chunks, semantic)
        hits = [c for c in ordered if lexical[id(c)] > 0 or semantic][:top_k]
        return {
            "object": "list",
            "retrieval": "hybrid" if semantic else "bm25",
            "data": [{**c, "score": round(lexical[id(c)], 4)} for c in hits],
        }

    @router.delete("/v1/documents/{doc_id}")
    async def delete_document(doc_id: str, request: Request):
        if not holder["store"].delete(plan_ctx({}, request).identity, doc_id):
            raise DocumentError(404, f"Document '{doc_id}' introuvable.")
        return {"id": doc_id, "deleted": True}

    return router
