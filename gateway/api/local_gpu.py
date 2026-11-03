"""Modèles locaux au quotidien (18.4) — clé maître : files d'attente des serveurs locaux, mode « PC occupé »,
mode économie, keep_alive d'Ollama et déchargement des modèles chargés."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request

from ..local_gpu import LocalGpu, LocalGpuError


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    admin = shared.admin

    def gpu() -> LocalGpu:
        return holder["gw"].local

    async def loaded(name: str) -> dict:
        try:
            return {"models": await gpu().loaded(name)}
        except LocalGpuError as e:
            return {"models": [], "error": e.message}

    @router.get("/v1/admin/gpu")
    async def gpu_status(request: Request):
        admin(request)
        st = gpu().status()
        ollama = [s for s in st["servers"] if s["ollama"]]
        for s, found in zip(ollama, await asyncio.gather(*(loaded(s["name"]) for s in ollama))):
            s["loaded"] = found["models"]
            if found.get("error"):
                s["error"] = found["error"]
        return st

    @router.put("/v1/admin/gpu/settings")
    async def gpu_settings(request: Request, body: dict):
        admin(request)
        gpu().set_config(body)
        await gpu().tick(by_request=True)  # une plage horaire en cours commence tout de suite
        return gpu().status()

    @router.post("/v1/admin/gpu/busy")
    async def gpu_busy(request: Request, body: dict):
        """{"on": true, "minutes": 120} : PC occupé (pour une durée, ou jusqu'à nouvel ordre) ; {"on": false}."""
        admin(request)
        on, minutes = body.get("on"), body.get("minutes")
        if not isinstance(on, bool):
            raise LocalGpuError("'on' : true ou false.")
        if minutes is not None and (
            not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 7 * 1440
        ):
            raise LocalGpuError("'minutes' : durée entre 1 minute et 7 jours.")
        state = await gpu().set_busy(on, minutes)
        request.state.audit = {"detail": {"on": on, "minutes": minutes}}
        return {"busy": {**state, "providers": gpu().busy_providers_config()}}

    @router.post("/v1/admin/gpu/eco")
    async def gpu_eco(request: Request, body: dict):
        """{"on": true, "minutes": 60} : mode économie (pour une durée, ou jusqu'à nouvel ordre) ; {"on": false}."""
        admin(request)
        on, minutes = body.get("on"), body.get("minutes")
        if not isinstance(on, bool):
            raise LocalGpuError("'on' : true ou false.")
        if minutes is not None and (
            not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 7 * 1440
        ):
            raise LocalGpuError("'minutes' : durée entre 1 minute et 7 jours.")
        state = await gpu().set_eco(on, minutes)
        request.state.audit = {"detail": {"on": on, "minutes": minutes}}
        return {"eco": gpu().status()["eco"] | state}

    @router.post("/v1/admin/gpu/unload")
    async def gpu_unload(request: Request, body: dict):
        """{"provider": "ollama", "model": "gemma3:12b"} ; sans « model » : tous les modèles chargés."""
        admin(request)
        name, model = body.get("provider"), body.get("model")
        if not isinstance(name, str) or (model is not None and not isinstance(model, str)):
            raise LocalGpuError("'provider' (et 'model' facultatif) : noms attendus.")
        request.state.audit = {"target": f"{name}/{model}" if model else name}
        return {"unloaded": await gpu().unload(name, model)}

    return router
