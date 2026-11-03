"""Génération d'images par votre ComfyUI (COMFYUI_URL, ex. http://localhost:8188).

ComfyUI tourne sur ce PC (ou une machine de votre réseau) avec ses propres modèles (checkpoints) ; Lezard IA lui
envoie un flux de travail texte → image standard (API de ComfyUI : POST /prompt, GET /history, GET /view) et renvoie
l'image. L'adresse est réglée par l'administrateur (jamais par l'IA ni par la requête) : elle peut donc être locale.
Une seule image à la fois (la carte graphique est partagée avec les modèles de langage)."""

from __future__ import annotations

import asyncio
import os
import random
import re
import time

import httpx

from .validation import RequestError

MAX_PROMPT = 1000
MAX_IMAGE = 25_000_000
_LOCK = asyncio.Lock()


def base_url() -> str:
    return os.getenv("COMFYUI_URL", "").strip().rstrip("/")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def parse_size(value) -> tuple[int, int]:
    """« 768x768 » : multiples de 8, 256 à 2048 par côté, COMFYUI_MAX_PIXELS au total (défaut 1 310 720)."""
    text = str(value or os.getenv("COMFYUI_SIZE") or "768x768").lower().strip()
    m = re.fullmatch(r"(\d{3,4})x(\d{3,4})", text)
    if not m:
        raise RequestError("'size' : largeurxhauteur, ex. 768x768.", 400)
    w, h = int(m.group(1)), int(m.group(2))
    if w % 8 or h % 8 or not (256 <= w <= 2048 and 256 <= h <= 2048):
        raise RequestError("'size' : multiples de 8 entre 256 et 2048.", 400)
    if w * h > _int_env("COMFYUI_MAX_PIXELS", 1_310_720):
        raise RequestError("'size' trop grande pour ce PC (COMFYUI_MAX_PIXELS).", 400)
    return w, h


def workflow(checkpoint: str, prompt: str, negative: str, width: int, height: int, seed: int) -> dict:
    """Flux texte → image classique (KSampler) au format API de ComfyUI."""
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": _int_env("COMFYUI_STEPS", 20),
                "cfg": 7,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "lezard", "images": ["8", 0]}},
    }


async def _get_json(client: httpx.AsyncClient, base: str, path: str):
    r = await client.get(f"{base}{path}")
    if r.status_code >= 400:
        raise RequestError(f"ComfyUI répond {r.status_code} ({path}).", 502)
    return r.json()


async def checkpoint(client: httpx.AsyncClient, base: str) -> str:
    if name := os.getenv("COMFYUI_CHECKPOINT", "").strip():
        return name
    info = await _get_json(client, base, "/object_info/CheckpointLoaderSimple")
    names = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    if not names:
        raise RequestError("Aucun modèle d'image dans ComfyUI : placez un checkpoint dans models/checkpoints.", 503)
    return names[0]


async def generate(prompt, negative="", size=None, info: dict | None = None) -> bytes:
    """PNG de l'image ; RequestError (400 requête, 502/503/504 ComfyUI) sinon. `info` reçoit le modèle d'image utilisé."""
    base = base_url()
    if not base:
        raise RequestError(
            "Génération d'images non configurée : COMFYUI_URL dans .env (ex. http://localhost:8188).", 503
        )
    prompt = str(prompt or "").strip()
    if not prompt or len(prompt) > MAX_PROMPT:
        raise RequestError(f"'prompt' requis ({MAX_PROMPT} caractères au plus).", 400)
    width, height = parse_size(size)
    timeout = _int_env("COMFYUI_TIMEOUT", 300)
    try:
        async with _LOCK, httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            ckpt = await checkpoint(client, base)
            if info is not None:
                info["model"] = ckpt
            flow = workflow(ckpt, prompt, str(negative or "")[:MAX_PROMPT], width, height, random.randrange(2**32))
            r = await client.post(f"{base}/prompt", json={"prompt": flow})
            if r.status_code >= 400:
                raise RequestError(f"ComfyUI refuse la demande ({r.status_code}) : {r.text[:200]}", 502)
            pid = r.json()["prompt_id"]
            deadline = time.monotonic() + timeout
            while True:
                hist = (await _get_json(client, base, f"/history/{pid}")).get(pid)
                if hist and hist.get("status", {}).get("status_str") == "error":
                    raise RequestError("ComfyUI n'a pas pu générer l'image (erreur dans son flux de travail).", 502)
                images = [i for out in (hist or {}).get("outputs", {}).values() for i in out.get("images", [])]
                if images:
                    break
                if time.monotonic() > deadline:
                    raise RequestError("ComfyUI met trop de temps (COMFYUI_TIMEOUT).", 504)
                await asyncio.sleep(_int_env("COMFYUI_POLL", 1))
            img = images[0]
            r = await client.get(
                f"{base}/view",
                params={
                    "filename": img["filename"],
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output"),
                },
            )
            if r.status_code >= 400 or len(r.content) > MAX_IMAGE:
                raise RequestError("Image illisible ou trop grosse.", 502)
            return r.content
    except httpx.HTTPError as e:
        raise RequestError(
            f"ComfyUI injoignable ({type(e).__name__}) : vérifiez COMFYUI_URL et qu'il tourne.", 503
        ) from None
    except (KeyError, ValueError, TypeError, IndexError):
        raise RequestError("Réponse inattendue de ComfyUI.", 502) from None
