"""Génération d'images par ComfyUI : flux envoyé, attente du résultat, erreurs, et route /v1/images/generations."""

import asyncio
import base64
from types import SimpleNamespace

import httpx
import pytest

from gateway import comfyui
from gateway.validation import RequestError

from .test_gateway_tools import MASTER, Scripted, make_client

REAL = httpx.AsyncClient
PNG = b"\x89PNG\r\n\x1a\nfaux-png"


def fake_comfy(monkeypatch, *, history_calls=2, status="success", checkpoints=("sd15.safetensors",)):
    seen = {"prompt": None, "polls": 0, "view": None}

    def handler(request):
        path = request.url.path
        if path == "/object_info/CheckpointLoaderSimple":
            return httpx.Response(
                200, json={"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [list(checkpoints)]}}}}
            )
        if path == "/prompt":
            seen["prompt"] = __import__("json").loads(request.content)["prompt"]
            return httpx.Response(200, json={"prompt_id": "abc"})
        if path == "/history/abc":
            seen["polls"] += 1
            if seen["polls"] < history_calls:
                return httpx.Response(200, json={})
            out = {"9": {"images": [{"filename": "lezard_0001.png", "subfolder": "", "type": "output"}]}}
            return httpx.Response(
                200, json={"abc": {"status": {"status_str": status}, "outputs": out if status == "success" else {}}}
            )
        if path == "/view":
            seen["view"] = dict(request.url.params)
            return httpx.Response(200, content=PNG)
        return httpx.Response(404)

    # seul le client du module comfyui est remplacé (la gateway garde le sien)
    monkeypatch.setattr(
        comfyui,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kw: REAL(transport=httpx.MockTransport(handler), **kw), HTTPError=httpx.HTTPError
        ),
    )
    monkeypatch.setenv("COMFYUI_URL", "http://192.168.1.30:8188/")
    monkeypatch.setenv("COMFYUI_POLL", "0")
    return seen


def test_generate_sends_the_workflow_and_returns_the_png(monkeypatch):
    seen = fake_comfy(monkeypatch)
    png = asyncio.run(comfyui.generate("un lézard vert", "flou", "512x768"))
    assert png == PNG and seen["polls"] == 2
    flow = seen["prompt"]
    assert flow["4"]["inputs"]["ckpt_name"] == "sd15.safetensors"  # premier modèle d'image de ComfyUI
    assert flow["6"]["inputs"]["text"] == "un lézard vert" and flow["7"]["inputs"]["text"] == "flou"
    assert flow["5"]["inputs"]["width"] == 512 and flow["5"]["inputs"]["height"] == 768
    assert seen["view"] == {"filename": "lezard_0001.png", "subfolder": "", "type": "output"}
    monkeypatch.setenv("COMFYUI_CHECKPOINT", "mon-modele.safetensors")  # modèle choisi dans .env
    asyncio.run(comfyui.generate("x"))
    assert seen["prompt"]["4"]["inputs"]["ckpt_name"] == "mon-modele.safetensors"


def test_generate_refuses_bad_requests_and_reports_comfyui_problems(monkeypatch):
    monkeypatch.delenv("COMFYUI_URL", raising=False)
    with pytest.raises(RequestError, match="COMFYUI_URL"):
        asyncio.run(comfyui.generate("x"))
    fake_comfy(monkeypatch)
    for bad in ("", "x" * 1001):
        with pytest.raises(RequestError, match="prompt"):
            asyncio.run(comfyui.generate(bad))
    for size in ("100x100", "770x768", "4096x4096", "grand", "2048x2048"):
        with pytest.raises(RequestError, match="size"):
            asyncio.run(comfyui.generate("x", size=size))
    fake_comfy(monkeypatch, status="error")
    with pytest.raises(RequestError, match="pas pu générer"):
        asyncio.run(comfyui.generate("x"))
    fake_comfy(monkeypatch, checkpoints=())
    with pytest.raises(RequestError, match="Aucun modèle d'image"):
        asyncio.run(comfyui.generate("x"))
    fake_comfy(monkeypatch, history_calls=10**9)
    monkeypatch.setenv("COMFYUI_TIMEOUT", "0")
    with pytest.raises(RequestError, match="trop de temps"):
        asyncio.run(comfyui.generate("x"))


def test_images_route_is_openai_shaped_and_needs_a_key(monkeypatch):
    fake_comfy(monkeypatch)
    with make_client(Scripted()) as c:
        # même règle d'accès que le chat, pour une clé absente comme pour une clé fausse
        chat_body = {"model": "ollama/x", "messages": [{"role": "user", "content": "x"}]}
        for headers in ({}, {"Authorization": "Bearer pas-une-vraie-cle"}):
            chat = c.post("/v1/chat/completions", headers=headers, json=chat_body)
            image = c.post("/v1/images/generations", headers=headers, json={"prompt": "x"})
            assert (image.status_code == 401) == (chat.status_code == 401), headers
        r = c.post("/v1/images/generations", headers=MASTER, json={"prompt": "un lézard", "size": "512x512"})
        assert r.status_code == 200 and base64.b64decode(r.json()["data"][0]["b64_json"]) == PNG
        assert r.headers["cache-control"] == "no-store"
        # suivi du tableau de bord : une ligne locale à 0 €, avec le modèle d'image et les tokens estimés de la description
        logged = c.get("/v1/metrics/requests", headers=MASTER).json()["data"]
        row = next(x for x in logged if x["endpoint"] == "images")
        assert (row["provider"], row["model"], row["hosting"]) == ("comfyui", "sd15.safetensors", "local")
        assert row["billed_eur"] == 0 and row["total_tokens"] >= 1 and row["status"] == "ok" and row["usage_estimated"]
        bad = c.post("/v1/images/generations", headers=MASTER, json={"prompt": ""})
        assert bad.status_code == 400
        monkeypatch.delenv("COMFYUI_URL")
        assert c.post("/v1/images/generations", headers=MASTER, json={"prompt": "x"}).status_code == 503
