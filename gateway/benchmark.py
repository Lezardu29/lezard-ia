"""Test de performance du PC pour choisir ses IA locales (après l'installation : aucune n'est installée d'office).

Deux petits modèles de test (qwen2.5:0.5b et qwen2.5:1.5b, 1,4 Go, supprimés après le test s'ils n'étaient pas là) écrivent le même texte, sur
la carte graphique puis sur le processeur. La génération est limitée par la lecture des poids en mémoire : le temps
par token suit « coût fixe + taille du modèle / débit ». Deux tailles donnent les deux inconnues, d'où une vitesse
estimée pour chaque modèle du catalogue, en VRAM, en RAM ou partagé entre les deux.
Mesuré sur une RTX 5070 (12 Go) : estimation prudente, environ 17 % sous la réalité (qwen2.5:14b : 54 tok/s
estimés, 64 mesurés ; gemma3:12b : 59 estimés, 72 mesurés). Un seul résultat gardé (réglage « benchmark »).
"""

from __future__ import annotations

import re
import time

import httpx

PROBES = ("qwen2.5:0.5b", "qwen2.5:1.5b")
PROMPT = "Écris un court paragraphe sur l'histoire de l'informatique."
TOKENS = 160
SETTING = "benchmark"
PULL_TIMEOUT = 1800.0
# lecture confortable : au-delà de 10 tokens/s, une réponse s'affiche plus vite qu'on ne la lit
COMFORT_TPS = 10.0
LABELS = ((25.0, "rapide"), (COMFORT_TPS, "confortable"), (4.0, "lent"), (0.0, "très lent"))


class BenchmarkError(Exception):
    """Test impossible (Ollama absent, téléchargement refusé…) : message pour le propriétaire."""


async def _sizes(client: httpx.AsyncClient, root: str) -> dict[str, int]:
    r = await client.get(f"{root}/api/tags", timeout=10)
    r.raise_for_status()
    return {m["name"]: m["size"] for m in r.json().get("models") or []}


async def _rate(client: httpx.AsyncClient, root: str, model: str, cpu: bool) -> float:
    """Tokens générés par seconde (2e passage : le modèle est déjà chargé, son chargement ne compte pas)."""
    options = {"num_predict": TOKENS, "temperature": 0, **({"num_gpu": 0} if cpu else {})}
    body = {"model": model, "prompt": PROMPT, "stream": False, "options": options, "keep_alive": "30s"}
    for _ in range(2):
        r = await client.post(f"{root}/api/generate", json=body, timeout=600)
        r.raise_for_status()
    data = r.json()
    return data["eval_count"] / (data["eval_duration"] / 1e9)


def fit_line(points: list[tuple[float, float]]) -> dict:
    """(taille en Go, tokens/s) de deux modèles -> coût fixe par token (s) et débit (Go/s)."""
    (s1, r1), (s2, r2) = sorted(points)
    slope = (1 / r2 - 1 / r1) / (s2 - s1) if s2 > s1 else 0
    if slope <= 0:  # mesure bruitée : tout sur le débit, sans coût fixe
        return {"overhead_s": 0.0, "bandwidth_gbs": round(s2 * r2, 1)}
    return {"overhead_s": max(0.0, 1 / r1 - s1 * slope), "bandwidth_gbs": round(1 / slope, 1)}


async def run(client: httpx.AsyncClient, root: str, step=lambda text: None) -> dict:
    """Télécharge les modèles de test au besoin, mesure, puis les décharge de la mémoire."""
    try:
        sizes = await _sizes(client, root)
    except (httpx.HTTPError, ValueError):
        raise BenchmarkError("Ollama ne répond pas : installez-le ou démarrez-le, puis relancez le test.") from None
    pulled = [p for p in PROBES if p not in sizes]
    for probe in pulled:
        step(f"Téléchargement du modèle de test {probe}…")
        r = await client.post(f"{root}/api/pull", json={"model": probe, "stream": False}, timeout=PULL_TIMEOUT)
        if r.status_code >= 400:
            raise BenchmarkError(f"Téléchargement de {probe} impossible ({r.status_code}).")
    sizes = await _sizes(client, root)
    result: dict = {"at": time.time(), "probes": {}}
    try:
        for device, cpu in (("gpu", False), ("cpu", True)):
            points = []
            for probe in PROBES:
                step(f"Mesure sur {'le processeur' if cpu else 'la carte graphique'} ({probe})…")
                rate = await _rate(client, root, probe, cpu)
                points.append((sizes[probe] / 1e9, rate))
                result["probes"][f"{probe}/{device}"] = round(rate, 1)
            result[device] = fit_line(points)
    except (httpx.HTTPError, KeyError, ValueError, ZeroDivisionError) as e:
        raise BenchmarkError(f"Mesure interrompue ({type(e).__name__}) : relancez le test.") from None
    finally:
        for probe in PROBES:  # mémoire rendue aux vrais modèles ; place rendue : modèles de test téléchargés supprimés
            try:
                await client.post(f"{root}/api/generate", json={"model": probe, "keep_alive": 0}, timeout=30)
                if probe in pulled:
                    await client.request("DELETE", f"{root}/api/delete", json={"model": probe}, timeout=30)
            except httpx.HTTPError:
                pass
    return result


def estimate(bench: dict, size_gb: float, vram_gb: float) -> float:
    """Tokens/s estimés pour un modèle de `size_gb` (poids lus à chaque token) : la part en VRAM au débit de la carte,
    le reste au débit de la RAM."""
    gpu, cpu = bench["gpu"], bench["cpu"]
    on_gpu = min(size_gb, max(0.0, vram_gb))
    in_ram = size_gb - on_gpu
    seconds = (gpu if on_gpu else cpu)["overhead_s"] + on_gpu / gpu["bandwidth_gbs"] + in_ram / cpu["bandwidth_gbs"]
    return round(1 / max(seconds, 1e-6), 1)


def label(tps: float) -> str:
    return next(name for floor, name in LABELS if tps >= floor)


MODEL_SETTING = "benchmark_models"  # vitesse mesurée de chaque modèle installé, par nom (ex. qwen3.8:27b)


async def measure_model(client: httpx.AsyncClient, root: str, model: str, step=lambda text: None) -> dict:
    """Vitesse réelle d'un modèle installé, comme il tournera (sans forcer GPU ni CPU : Ollama répartit selon la
    VRAM). Le premier passage charge le modèle ; le second est mesuré. `vram` : part des poids dans la carte graphique."""
    body = {"model": model, "prompt": PROMPT, "stream": False, "options": {"num_predict": TOKENS, "temperature": 0}}
    try:
        step(f"Chargement de {model}…")
        await client.post(f"{root}/api/generate", json={**body, "keep_alive": "2m"}, timeout=1800)
        step(f"Mesure de {model}…")
        r = await client.post(f"{root}/api/generate", json={**body, "keep_alive": "2m"}, timeout=1800)
        r.raise_for_status()
        data = r.json()
        loaded = (await client.get(f"{root}/api/ps", timeout=10)).json().get("models") or []
        info = next((m for m in loaded if m.get("name") == model), {})
    except (httpx.HTTPError, KeyError, ValueError, ZeroDivisionError) as e:
        raise BenchmarkError(f"Mesure de {model} interrompue ({type(e).__name__}) : relancez le test.") from None
    finally:
        try:  # mémoire rendue tout de suite
            await client.post(f"{root}/api/generate", json={"model": model, "keep_alive": 0}, timeout=60)
        except httpx.HTTPError:
            pass
    return {
        "tps": round(data["eval_count"] / (data["eval_duration"] / 1e9), 1),
        "size": info.get("size") or 0,
        "vram": info.get("size_vram") or 0,
        "at": time.time(),
    }


def valid_model(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}(:[a-z0-9][a-z0-9._-]{0,63})?", name or ""))
