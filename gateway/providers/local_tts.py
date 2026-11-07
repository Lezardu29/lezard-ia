"""Lecture vocale locale intégrée (19.4) : Kokoro (82 M de paramètres, ONNX) exécuté DANS la gateway, sur ce PC.
Le texte lu ne quitte jamais la machine et ne coûte rien ; pas de Docker ni de serveur à part.

Facultatif : actif si le paquet kokoro-onnx est installé (requirements-local.txt), que LOCAL_TTS n'est pas « false »
et que les fichiers du modèle sont présents (KOKORO_DIR, défaut data/models/kokoro). Ils ne sont JAMAIS téléchargés
d'office (14.4) : « python -m gateway.providers.local_tts download » (~350 Mo, empreintes SHA-256 vérifiées).
Voix françaises : ff_siwis ; anglaises af_heart, am_michael… (préfixe = langue). Rend du WAV 24 kHz (pcm sur
demande) : la gateway déclare le type d'après les octets, tout navigateur le lit.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import logging
import os
import sys
import threading
import wave
from array import array
from pathlib import Path

import httpx

from ..config import ProviderConfig, hosted_instance
from ..store import default_db_path
from .base import InProcessModel, Provider, ProviderError

NAME = "tts-local"
MODEL_ID = "kokoro"
RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = {  # fichier -> SHA-256 (version 1.0 publiée par le projet kokoro-onnx)
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}
LANGS = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi", "i": "it", "j": "ja", "p": "pt-br", "z": "cmn"}
log = logging.getLogger("gateway")


def model_dir() -> Path:
    if os.getenv("KOKORO_DIR"):
        return Path(os.environ["KOKORO_DIR"])
    db = default_db_path()
    base = Path(db).parent if db != ":memory:" else Path("data")
    return base / "models" / "kokoro"


def installed() -> bool:
    return all((model_dir() / f).is_file() for f in FILES)


def enabled() -> bool:
    """Paquet installé, modèle présent, non désactivé, et pas sur une instance hébergée."""
    return (
        os.getenv("LOCAL_TTS", "true").lower() not in ("0", "false", "no")
        and not hosted_instance()
        and importlib.util.find_spec("kokoro_onnx") is not None
        and installed()
    )


def no_power_throttling() -> bool:
    """Windows 11 bride (EcoQoS : cœurs économes, fréquence basse) les processus d'arrière-plan, comme une gateway
    lancée en service : Kokoro passe alors de ~1 s à ~4 s pour 6 s d'audio. Désactive ce bridage pour ce processus
    (SetProcessInformation, ProcessPowerThrottling) ; sans effet ailleurs que sous Windows."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class State(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

    state = State(1, 0x1, 0x0)  # PROCESS_POWER_THROTTLING_EXECUTION_SPEED : géré par nous, désactivé
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        return bool(k32.SetProcessInformation(k32.GetCurrentProcess(), 4, ctypes.byref(state), ctypes.sizeof(state)))
    except (OSError, AttributeError):  # Windows trop ancien
        return False


def pcm16(samples) -> bytes:
    """Échantillons flottants (-1…1) en PCM 16 bits petit-boutiste ; numpy (toujours présent avec Kokoro) sinon
    Python pur."""
    try:
        import numpy as np
    except ImportError:
        out = array("h", (int(max(-1.0, min(1.0, float(x))) * 32767) for x in samples))
        if sys.byteorder == "big":
            out.byteswap()
        return out.tobytes()
    return (np.clip(np.asarray(samples, dtype="float32"), -1.0, 1.0) * 32767).astype("<i2").tobytes()


def to_wav(samples, rate: int) -> bytes:
    pcm = pcm16(samples)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class LocalTtsProvider(InProcessModel, Provider):
    """Fournisseur « tts-local » : un seul modèle (« kokoro »), chargé au premier usage (ou en avance par `preload`).
    Une synthèse à la fois : elle occupe les cœurs du processeur."""

    def __init__(self, client: httpx.AsyncClient, factory=None):
        super().__init__(ProviderConfig(NAME, "openai", "builtin://kokoro", "local"), client)
        self._factory = factory  # tests : modèle simulé
        self._model = None
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self.load_error: str | None = None

    async def list_models(self) -> list[str]:
        return [MODEL_ID]

    def _load(self):
        self._touch()
        with self._load_lock:
            if self._model is None:
                if self._factory:
                    self._model = self._factory()
                else:
                    from kokoro_onnx import Kokoro

                    no_power_throttling()
                    d = model_dir()
                    self._model = Kokoro(str(d / "kokoro-v1.0.onnx"), str(d / "voices-v1.0.bin"))
                self.load_error = None
            return self._model

    def preload(self) -> None:
        def run():
            try:
                self._load()
                log.info("Lecture vocale locale prête : Kokoro (%s).", model_dir())
            except Exception as e:  # fichier abîmé, onnxruntime absent…
                self.load_error = str(e)[:300]
                log.warning("Lecture vocale locale indisponible pour l'instant : %s", e)

        threading.Thread(target=run, name="kokoro-preload", daemon=True).start()

    def voices(self) -> list[str]:
        return sorted(self._load().get_voices())

    def _run(self, payload: dict) -> bytes:
        model = self._load()
        voice = payload.get("voice") or "ff_siwis"
        known = model.get_voices()
        if voice not in known:  # voix OpenAI (alloy…) ou faute de frappe : refus clair plutôt qu'une voix au hasard
            fr = [v for v in sorted(known) if v.startswith("f")]
            raise ValueError(f"voix « {voice} » inconnue de Kokoro (français : {', '.join(fr) or 'aucune'}).")
        speed = min(2.0, max(0.5, float(payload.get("speed") or 1.0)))
        # réglages de la lecture (page Modèles, catégorie « Voix ») : prononciation, mélange de deux voix, pauses
        lang = payload.get("lang") if payload.get("lang") in LANGS.values() else LANGS.get(voice[0], "en-us")
        style, blend = voice, payload.get("blend")
        if blend:
            if blend["voice"] not in known:
                raise ValueError(f"voix « {blend['voice']} » inconnue de Kokoro.")
            share = min(0.9, max(0.1, float(blend["share"])))
            style = model.get_voice_style(voice) * (1 - share) + model.get_voice_style(blend["voice"]) * share
        options = {"sentence_pause": float(payload["pause"])} if payload.get("pause") is not None else {}
        with self._run_lock:
            samples, rate = model.create(payload["input"], voice=style, speed=speed, lang=lang, **options)
            self._touch()  # fin d'usage : le délai avant déchargement part d'ici
        if payload.get("response_format") == "pcm":
            return pcm16(samples)
        return to_wav(samples, rate)

    async def speech(self, payload: dict, limit: int) -> bytes:
        try:
            async with self._slot(MODEL_ID, "speech"):  # file d'attente : jamais plus de threads que de places
                audio = await asyncio.to_thread(self._run, payload)
        except ProviderError:  # PC occupé, file d'attente pleine (18.4) : relayé tel quel
            raise
        except ValueError as e:
            raise ProviderError(400, f"Lecture vocale : {e}", NAME)
        except Exception as e:
            self.load_error = str(e)[:300]
            raise ProviderError(503, f"Lecture vocale locale impossible : {e}", NAME)
        if len(audio) > limit:
            raise ProviderError(502, f"Audio trop volumineux (plus de {limit} octets).", NAME)
        return audio

    async def chat(self, payload: dict) -> dict:
        raise ProviderError(400, "« tts-local » lit du texte à voix haute : ce n'est pas un modèle de chat.", NAME)


# ---------- téléchargement du modèle (à la demande seulement) ----------
def download(dest: Path | None = None, client: httpx.Client | None = None, out=print) -> Path:
    """Télécharge les fichiers manquants, vérifie leur SHA-256, puis les met en place (jamais un fichier partiel)."""
    dest = dest or model_dir()
    dest.mkdir(parents=True, exist_ok=True)
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, read=120))
    try:
        for name, digest in FILES.items():
            target = dest / name
            if target.is_file() and _sha256(target) == digest:
                out(f"{name} : déjà présent")
                continue
            part = target.with_suffix(target.suffix + ".part")
            h = hashlib.sha256()
            with client.stream("GET", RELEASE + name) as r:
                r.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        h.update(chunk)
            if h.hexdigest() != digest:
                part.unlink(missing_ok=True)
                raise RuntimeError(f"{name} : empreinte SHA-256 inattendue, fichier supprimé.")
            os.replace(part, target)
            out(f"{name} : téléchargé et vérifié ({target.stat().st_size // 1_000_000} Mo)")
    finally:
        if own:
            client.close()
    return dest


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":  # python -m gateway.providers.local_tts download
    if sys.argv[1:] != ["download"]:
        sys.exit("Usage : python -m gateway.providers.local_tts download")
    where = download()
    print(f"Modèle Kokoro prêt dans {where}. Redémarrez la gateway pour activer la lecture vocale locale.")
