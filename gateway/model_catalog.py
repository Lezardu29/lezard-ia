"""Recommandation de modèles locaux selon le matériel (13.2).

Mémoire estimée = paramètres × octets par poids (quantification) + cache KV pour la fenêtre d'Ollama + marge
d'exécution. Classement :
- « vram » : tient entièrement en VRAM (rapide) ;
- « offload » : VRAM + RAM, ou RAM seule sans GPU (plus lent) ;
- « too_big » : trop gros pour cette machine.
Les noms Ollama du catalogue sont ceux que le routage reconnaît (coder → code, r1 → raisonnement, vision) :
le meilleur modèle installé par usage sert donc directement le routage par spécialité.
vLLM sert un seul modèle choisi au lancement, en bf16 : simple recommandation (identifiant Hugging Face).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import benchmark
from .gpus import usable
from .local_context import ollama_server_default

GB = 1e9
BYTES_PER_WEIGHT = {"q4_K_M": 0.60, "q8_0": 1.07, "bf16": 2.0}  # ~4,8 bits avec échelles ; 8,5 bits ; 16 bits
KV_GB_PER_B_AT_4K = 0.064  # cache KV fp16 à 4 096 tokens, par milliard de paramètres (Llama 3.1 8B : ~0,5 Go)
RUNTIME_GB = 0.5  # contexte CUDA, tampons de calcul
DISPLAY_RESERVE_GB = 0.6  # VRAM gardée par le bureau / l'affichage (Windows : ~0,5 Go)
DISPLAY_RESERVE_SHARE = 0.06
RAM_SHARE = 0.8  # part de la RAM libre utilisable sans faire ramer le système
DISK_MARGIN_GB = 1.0  # marge au-delà de la taille du téléchargement
UNKNOWN_MODEL_DISK_GB = 4.0  # modèle hors catalogue : espace minimal exigé (petit 7B en Q4)
USES = ("chat", "code", "vision", "reasoning", "embeddings")
USE_LABELS = {
    "chat": "Conversation",
    "code": "Code",
    "vision": "Vision (images)",
    "reasoning": "Raisonnement",
    "embeddings": "Recherche dans vos documents (embeddings)",
}


@dataclass(frozen=True)
class CatalogModel:
    model: str  # nom Ollama
    label: str
    params_b: float
    download_gb: float  # taille du téléchargement Ollama (Q4_K_M)
    uses: tuple[str, ...]
    hf: str | None = None  # identifiant Hugging Face pour vLLM
    extra_gb: float = 0.0  # encodeur d'images (vision), gardé en 16 bits
    note: str = ""


CATALOG = (
    # embeddings (16.1) : très petits ; vecteurs pour Open WebUI, Continue, LlamaIndex… (POST /v1/embeddings)
    CatalogModel(
        "nomic-embed-text",
        "Nomic Embed Text",
        0.14,
        0.27,
        ("embeddings",),
        "nomic-ai/nomic-embed-text-v1.5",
        note="léger, anglais surtout",
    ),
    CatalogModel("bge-m3", "BGE-M3", 0.57, 1.2, ("embeddings",), "BAAI/bge-m3", note="multilingue, bon en français"),
    CatalogModel(
        "llama3.2:3b", "Llama 3.2 3B", 3.2, 2.0, ("chat",), "meta-llama/Llama-3.2-3B-Instruct", note="très léger"
    ),
    CatalogModel("phi3.5", "Phi 3.5 Mini", 3.8, 2.2, ("chat",), "microsoft/Phi-3.5-mini-instruct", note="léger"),
    CatalogModel("gemma3:4b", "Gemma 3 4B", 4.3, 3.3, ("chat", "vision"), "google/gemma-3-4b-it", 0.8),
    CatalogModel("mistral:7b", "Mistral 7B", 7.2, 4.1, ("chat",), "mistralai/Mistral-7B-Instruct-v0.3"),
    CatalogModel(
        "qwen2.5:7b",
        "Qwen 2.5 7B",
        7.6,
        4.7,
        ("chat",),
        "Qwen/Qwen2.5-7B-Instruct",
        note="multilingue, bon en français",
    ),
    CatalogModel("qwen2.5-coder:7b", "Qwen 2.5 Coder 7B", 7.6, 4.7, ("code",), "Qwen/Qwen2.5-Coder-7B-Instruct"),
    CatalogModel("qwen2.5vl:7b", "Qwen 2.5 VL 7B", 8.3, 6.0, ("vision",), "Qwen/Qwen2.5-VL-7B-Instruct", 1.0),
    CatalogModel(
        "llama3.1:8b", "Llama 3.1 8B", 8.0, 4.9, ("chat",), "meta-llama/Llama-3.1-8B-Instruct", note="polyvalent"
    ),
    CatalogModel(
        "deepseek-r1:8b", "DeepSeek R1 8B", 8.0, 5.2, ("reasoning",), "deepseek-ai/DeepSeek-R1-Distill-Llama-8B"
    ),
    CatalogModel("gemma2:9b", "Gemma 2 9B", 9.2, 5.4, ("chat",), "google/gemma-2-9b-it"),
    CatalogModel("gemma3:12b", "Gemma 3 12B", 12.2, 8.1, ("chat", "vision"), "google/gemma-3-12b-it", 0.8),
    CatalogModel("qwen2.5:14b", "Qwen 2.5 14B", 14.8, 9.0, ("chat",), "Qwen/Qwen2.5-14B-Instruct"),
    CatalogModel("qwen2.5-coder:14b", "Qwen 2.5 Coder 14B", 14.8, 9.0, ("code",), "Qwen/Qwen2.5-Coder-14B-Instruct"),
    CatalogModel(
        "deepseek-r1:14b", "DeepSeek R1 14B", 14.8, 9.0, ("reasoning",), "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B"
    ),
    CatalogModel(
        "mistral-small:24b", "Mistral Small 24B", 23.6, 14.0, ("chat",), "mistralai/Mistral-Small-24B-Instruct-2501"
    ),
    CatalogModel("gemma3:27b", "Gemma 3 27B", 27.4, 17.0, ("chat", "vision"), "google/gemma-3-27b-it", 0.8),
    CatalogModel("qwen2.5:32b", "Qwen 2.5 32B", 32.8, 20.0, ("chat",), "Qwen/Qwen2.5-32B-Instruct"),
    CatalogModel("qwen2.5-coder:32b", "Qwen 2.5 Coder 32B", 32.8, 20.0, ("code",), "Qwen/Qwen2.5-Coder-32B-Instruct"),
    CatalogModel(
        "deepseek-r1:32b", "DeepSeek R1 32B", 32.8, 20.0, ("reasoning",), "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"
    ),
    CatalogModel("llama3.3:70b", "Llama 3.3 70B", 70.6, 43.0, ("chat",), "meta-llama/Llama-3.3-70B-Instruct"),
)
BY_NAME = {m.model: m for m in CATALOG}


def normalize(name: str) -> str:
    return name[:-7] if name.endswith(":latest") else name


def find(name: str) -> CatalogModel | None:
    return BY_NAME.get(normalize(name))


def memory_needed(m: CatalogModel, quant: str = "q4_K_M", context: int | None = None) -> float:
    """Mémoire (octets) pour exécuter le modèle avec la fenêtre `context` (défaut : celle du serveur Ollama)."""
    ctx = context or ollama_server_default()
    weights = m.params_b * BYTES_PER_WEIGHT[quant]
    kv = m.params_b * KV_GB_PER_B_AT_4K * ctx / 4096
    return (weights + m.extra_gb + kv + RUNTIME_GB) * GB


def disk_needed(name: str) -> float:
    """Espace disque exigé avant un téléchargement (14.4)."""
    m = find(name)
    return ((m.download_gb if m else UNKNOWN_MODEL_DISK_GB) + DISK_MARGIN_GB) * GB


def budget(hw: dict) -> dict:
    """Mémoire disponible pour un modèle : VRAM TOTALE du plus gros GPU moins la réserve de l'affichage (ce que le
    PC peut faire, pas ce qu'un navigateur ou un jeu ouvert laisse à l'instant), et part de la RAM disponible.
    `vram_busy` : VRAM prise en ce moment par d'autres applications que les modèles (à libérer pour la vitesse).
    Carte DÉDIÉE seulement, de tout fabricant (NVIDIA, AMD, Intel Arc) : la partie graphique intégrée au processeur
    partage la RAM et calcule bien moins vite, elle n'est jamais la carte des modèles."""
    gpus = usable(hw.get("gpus") or [])
    gpu = max(gpus, key=lambda g: g["vram_total"], default=None)
    vram = busy = 0
    if gpu:
        vram = gpu["vram_total"] - max(DISPLAY_RESERVE_GB * GB, gpu["vram_total"] * DISPLAY_RESERVE_SHARE)
        if gpu.get("vram_used") is not None:
            busy = max(0, gpu["vram_used"] - (hw.get("models_vram") or 0))
    ram = (hw.get("ram") or {}).get("available") or 0
    return {
        "gpu": gpu["name"] if gpu else None,
        "vram": vram,
        "vram_busy": busy,
        "ram": ram * RAM_SHARE,
        "disk_free": (hw.get("disk") or {}).get("free"),
    }


def fit(need: float, b: dict) -> str:
    if b["vram"] and need <= b["vram"]:
        return "vram"
    if need <= b["vram"] + b["ram"]:
        return "offload"
    return "too_big"


def speed_of(m, bench: dict | None, measured: dict | None, b: dict) -> float | None:
    """Mesurée sur ce PC pour ce modèle si elle existe, sinon estimée d'après le test de performance (embeddings : pas
    de vitesse, ce ne sont pas des textes générés)."""
    if measured and m.model in measured:
        return measured[m.model]["tps"]
    if bench and m.uses != ("embeddings",):
        return benchmark.estimate(bench, m.download_gb + m.extra_gb, b["vram"] / GB)
    return None


def speed_source(m, bench: dict | None, measured: dict | None) -> str | None:
    if measured and m.model in measured:
        return "mesuré"
    return "estimé" if bench and m.uses != ("embeddings",) else None


def recommend(
    hw: dict,
    installed: list[str] | None = None,
    context: int | None = None,
    bench: dict | None = None,
    measured: dict | None = None,
) -> dict:
    """`bench` : résultat du test de performance (benchmark.run) ; il ajoute la vitesse estimée de chaque modèle et
    choisit, par usage, le plus gros modèle qui reste confortable (au moins benchmark.COMFORT_TPS tokens/s)."""
    b = budget(hw)
    have = {normalize(n) for n in installed or []}
    models = []
    for m in CATALOG:
        need = memory_needed(m, context=context)
        vllm_need = memory_needed(m, "bf16", context)
        disk = disk_needed(m.model)
        models.append(
            {
                "model": m.model,
                "label": m.label,
                "params_b": m.params_b,
                "uses": list(m.uses),
                "note": m.note,
                "download": m.download_gb * GB,
                "memory": round(need),
                "fit": fit(need, b),
                # embeddings : pas de texte généré, une vitesse en tokens/s n'aurait pas de sens
                "speed": speed_of(m, bench, measured, b),
                "speed_source": speed_source(m, bench, measured),
                "installed": m.model in have,
                "disk_ok": b["disk_free"] is None or b["disk_free"] >= disk,
                "vllm": {
                    "hf": m.hf,
                    "memory": round(vllm_need),
                    "fits_vram": bool(b["vram"]) and vllm_need <= b["vram"],
                }
                if m.hf
                else None,
            }
        )
    for x in models:
        x["speed_label"] = benchmark.label(x["speed"]) if x["speed"] is not None and x["fit"] != "too_big" else None
    best = {}
    for use in USES:
        cands = [x for x in models if use in x["uses"] and x["fit"] != "too_big"]
        if bench and cands and all(x["speed"] is not None for x in cands):  # mesurée : le plus gros confortable
            comfy = [x for x in cands if x["speed"] >= benchmark.COMFORT_TPS]
            best[use] = (max(comfy, key=lambda x: x["params_b"]) if comfy else max(cands, key=lambda x: x["speed"]))[
                "model"
            ]
            continue
        fast = [x for x in cands if x["fit"] == "vram"]
        # en VRAM : le plus gros qui tient ; sinon le plus petit en RAM (le moins lent)
        pick = (
            max(fast, key=lambda x: x["params_b"])
            if fast
            else min(cands, key=lambda x: x["params_b"])
            if cands
            else None
        )
        best[use] = pick["model"] if pick else None
    return {
        "budget": b,
        "context": context or ollama_server_default(),
        "best": best,
        "uses": USE_LABELS,
        "models": models,
        "benchmark": bench,
    }
