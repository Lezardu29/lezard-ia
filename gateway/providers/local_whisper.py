"""Transcription locale intégrée (13.9) : faster-whisper (Whisper d'OpenAI réécrit en CTranslate2) exécuté DANS la
gateway, sur ce PC. L'audio de la dictée ne quitte jamais la machine et ne coûte rien.

Facultatif : actif si le paquet faster-whisper est installé (requirements-local.txt) et que LOCAL_WHISPER n'est pas
« false ». Le modèle (WHISPER_MODEL : tiny, base, small, medium, large-v3-turbo, large-v3…) est téléchargé une seule
fois depuis Hugging Face, puis lu depuis le cache local. Par défaut (« auto »), il est choisi pour CE PC, comme les
modèles conseillés (13.2) : carte graphique NVIDIA prête (bibliothèques CUDA 12 / cuDNN 9 trouvées) et assez de
mémoire vidéo → large-v3-turbo sur la carte graphique, sinon small sur le processeur (base sur un petit PC).
WHISPER_DEVICE (cpu, cuda) impose l'appareil. Carte graphique en échec au chargement : repli sur le processeur.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import io
import logging
import os
import sys
import threading
from pathlib import Path

import httpx

from ..config import ProviderConfig, hosted_instance
from .base import Provider, ProviderError

NAME = "whisper-local"
log = logging.getLogger("gateway")
GB = 1024**3
GPU_MIN_VRAM = 4 * GB  # large-v3-turbo en float16 : ~2 Go, plus la marge de l'affichage
SMALL_PC = (4, 6 * GB)  # moins de 4 cœurs ou de 6 Go de RAM : « base », plus léger que « small »
CUDA_LIBS = {"nt": ("cublas64_12.dll", "cudnn64_9.dll"), "posix": ("libcublas.so.12", "libcudnn.so.9")}


def enabled() -> bool:
    """Installé, non désactivé, et pas sur une instance hébergée (son processeur n'est pas celui de l'utilisateur)."""
    return (
        os.getenv("LOCAL_WHISPER", "true").lower() not in ("0", "false", "no")
        and not hosted_instance()
        and importlib.util.find_spec("faster_whisper") is not None
    )


def cuda_ready() -> bool:
    """Carte graphique NVIDIA utilisable par faster-whisper : pilote vu par CTranslate2 ET bibliothèques CUDA 12 /
    cuDNN 9 chargeables (installées à part, ou paquets pip nvidia-cublas-cu12 / nvidia-cudnn-cu12)."""
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() < 1:
            return False
    except Exception:
        return False
    if os.name == "nt":  # paquets pip : DLL dans site-packages/nvidia/*/bin, hors du chemin de recherche
        for base in map(Path, sys.path):
            for folder in (base / "nvidia").glob("*/bin") if (base / "nvidia").is_dir() else ():
                try:
                    os.add_dll_directory(str(folder))
                except OSError:
                    pass
    try:
        for lib in CUDA_LIBS.get(os.name, ()):
            ctypes.CDLL(lib)
    except OSError:
        return False
    return True


def auto_plan(gpus: list[dict], cores: int | None, ram_total: int | None, cuda: bool) -> tuple[str, str, str]:
    """(taille, appareil, raison) pour ce PC."""
    gpu = max(gpus, key=lambda g: g.get("vram_total") or 0, default=None)
    if gpu and (gpu.get("vram_total") or 0) >= GPU_MIN_VRAM:
        if cuda:
            return "large-v3-turbo", "cuda", f"carte graphique {gpu['name']}"
        why = f"processeur : {gpu['name']} présente, mais bibliothèques CUDA 12 / cuDNN 9 absentes"
    else:
        why = "processeur (pas de carte graphique NVIDIA assez grande)"
    if (cores or 0) < SMALL_PC[0] or (ram_total or 0) < SMALL_PC[1]:
        return "base", "cpu", why + ", petit PC"
    return "small", "cpu", why


def _plan_for_this_pc(wanted_device: str | None) -> tuple[str, str, str]:
    from ..hardware import _ram_fallback, gpu_info, load_nvml

    cuda = wanted_device != "cpu" and cuda_ready()
    gpus = gpu_info(load_nvml())["gpus"] if wanted_device != "cpu" else []
    size, device, why = auto_plan(gpus, os.cpu_count(), _ram_fallback().get("total"), cuda)
    if wanted_device == "cuda" and device != "cuda":  # imposé : on essaie quand même, repli au chargement
        return "large-v3-turbo", "cuda", "carte graphique (WHISPER_DEVICE=cuda)"
    return size, device, why


def _stamp(seconds: float, sep: str) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02}{sep}{ms:03}"


def subtitles(segments: list[dict], fmt: str) -> bytes:
    sep = "," if fmt == "srt" else "."
    lines = [] if fmt == "srt" else ["WEBVTT", ""]
    for i, s in enumerate(segments, 1):
        if fmt == "srt":
            lines.append(str(i))
        lines += [f"{_stamp(s['start'], sep)} --> {_stamp(s['end'], sep)}", s["text"].strip(), ""]
    return "\n".join(lines).encode("utf-8")


class LocalWhisperProvider(Provider):
    """Fournisseur « whisper-local » : un seul modèle de transcription, chargé au premier usage (ou en avance par
    `preload`). Une transcription à la fois : elle occupe les cœurs du processeur."""

    def __init__(self, client: httpx.AsyncClient, size: str | None = None, factory=None):
        super().__init__(ProviderConfig(NAME, "openai", "builtin://faster-whisper", "local"), client)
        wanted = (size or os.getenv("WHISPER_MODEL", "auto")).strip().lower() or "auto"
        device = os.getenv("WHISPER_DEVICE", "").strip().lower() or None
        if wanted == "auto":
            self.size, self.device, self.reason = _plan_for_this_pc(device)
            self.auto = True
        else:
            self.size, self.device = wanted, device or "cpu"
            self.reason, self.auto = "WHISPER_MODEL dans .env", False
        self._factory = factory  # tests : modèle simulé
        self._model = None
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self.load_error: str | None = None

    @property
    def model_id(self) -> str:
        return f"whisper-{self.size}"

    async def list_models(self) -> list[str]:
        return [self.model_id]

    def _load(self):
        with self._load_lock:
            if self._model is None:
                if self._factory:
                    self._model = self._factory()
                else:
                    from faster_whisper import WhisperModel

                    try:
                        self._model = WhisperModel(
                            self.size, device=self.device, compute_type="int8" if self.device == "cpu" else "float16"
                        )
                    except Exception as e:
                        if self.device == "cpu":
                            raise
                        log.warning("Transcription sur la carte graphique impossible (%s) : processeur.", e)
                        self.size = self.size if not self.auto else "small"
                        self.device, self.reason = "cpu", f"processeur (carte graphique en échec : {str(e)[:120]})"
                        self._model = WhisperModel(self.size, device="cpu", compute_type="int8")
                self.load_error = None
            return self._model

    def preload(self) -> None:
        """Téléchargement / chargement en arrière-plan au démarrage : la première dictée n'attend pas."""

        def run():
            try:
                self._load()
                log.info("Transcription locale prête : %s (%s).", self.model_id, self.device)
            except Exception as e:  # réseau absent au premier lancement, CUDA manquant…
                self.load_error = str(e)[:300]
                log.warning("Transcription locale indisponible pour l'instant (%s) : %s", self.model_id, e)

        threading.Thread(target=run, name="whisper-preload", daemon=True).start()

    def _run(self, audio: bytes, fields: dict) -> dict:
        model = self._load()
        options = {"vad_filter": True}  # ignore les silences (début / fin de dictée)
        if fields.get("language"):
            options["language"] = str(fields["language"]).strip().lower()[:5]
        if fields.get("prompt"):
            options["initial_prompt"] = str(fields["prompt"])[:1000]
        if fields.get("temperature") not in (None, ""):
            options["temperature"] = float(fields["temperature"])
        with self._run_lock:
            segments, info = model.transcribe(io.BytesIO(audio), **options)
            segs = [
                {"id": i, "start": round(s.start, 2), "end": round(s.end, 2), "text": s.text}
                for i, s in enumerate(segments)
            ]
        return {
            "text": "".join(s["text"] for s in segs).strip(),
            "language": info.language,
            "duration": round(info.duration, 2),
            "segments": segs,
        }

    async def transcribe(self, model: str, audio: bytes, filename: str, kind: str, fields: dict) -> dict | bytes:
        try:
            async with self._slot(model, "transcription"):  # file d'attente : jamais plus de threads que de places
                out = await asyncio.to_thread(self._run, audio, fields)
        except ProviderError:  # PC occupé, file d'attente pleine (18.4) : relayé tel quel
            raise
        except (ValueError, TypeError) as e:
            raise ProviderError(400, f"Paramètre de transcription invalide : {e}", NAME)
        except Exception as e:
            self.load_error = str(e)[:300]
            raise ProviderError(503, f"Transcription locale impossible : {e}", NAME)
        fmt = fields.get("response_format", "json")
        if fmt == "verbose_json":
            return out
        if fmt == "text":
            return out["text"].encode("utf-8")
        if fmt in ("srt", "vtt"):
            return subtitles(out["segments"], fmt)
        return {"text": out["text"], "duration": out["duration"]}

    async def chat(self, payload: dict) -> dict:
        raise ProviderError(400, "« whisper-local » transcrit l'audio : ce n'est pas un modèle de chat.", NAME)
