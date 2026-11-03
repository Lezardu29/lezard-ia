"""Catégories « Dictée » et « Lecture vocale » : comme pour les embeddings (embedding_choice.py), le meilleur des modèles
détectés qui tourne bien sur ce PC sert la dictée du chat (13.9) et le bouton « Écouter » (19.4), sauf choix fixé
par le propriétaire (page Modèles).

Classement : qualité connue en français (tableaux ci-dessous : estimations arrondies d'après les taux d'erreur
publiés sur FLEURS / Common Voice pour la dictée, et la qualité des voix françaises pour la lecture ; un modèle
surtout anglais est pénalisé), place sur CE PC (mémoire vidéo, VRAM + RAM, processeur pour les modèles intégrés),
local d'abord. À qualité voisine, le serveur local le moins occupé passe devant (voice_split) ; les autres restent
des secours si le premier tombe en panne.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from . import voice_split
from .embedding_choice import EmbeddingChoice
from .providers.local_tts import NAME as LOCAL_TTS
from .providers.local_whisper import NAME as LOCAL_WHISPER

if TYPE_CHECKING:
    from .registry import ModelInfo

# motif du nom → (nom affiché, qualité 0–1 en français, multilingue, mémoire en Go) ; le plus précis d'abord
STT = (
    ("gpt-4o-mini-transcribe", "OpenAI GPT-4o mini Transcribe", 0.93, True, 0),
    ("gpt-4o-transcribe", "OpenAI GPT-4o Transcribe", 0.95, True, 0),
    ("voxtral", "Mistral Voxtral", 0.94, True, 9.5),
    ("parakeet-tdt-0.6b-v3", "NVIDIA Parakeet TDT 0.6B v3", 0.91, True, 2.0),
    ("parakeet", "NVIDIA Parakeet", 0.85, False, 2.0),
    ("canary", "NVIDIA Canary", 0.91, True, 3.0),
    ("stt-1b-en_fr", "Kyutai STT 1B", 0.88, True, 2.5),
    ("distil", "Distil-Whisper", 0.86, False, 1.5),
    ("large-v3-turbo", "Whisper large-v3-turbo", 0.91, True, 2.0),
    ("turbo", "Whisper turbo", 0.91, True, 2.0),
    ("large-v3", "Whisper large-v3", 0.93, True, 4.0),
    ("large", "Whisper large", 0.91, True, 4.0),
    ("medium", "Whisper medium", 0.86, True, 2.0),
    ("small", "Whisper small", 0.78, True, 1.0),
    ("base", "Whisper base", 0.62, True, 0.5),
    ("tiny", "Whisper tiny", 0.48, True, 0.4),
    ("whisper-1", "Whisper (whisper-1)", 0.90, True, 0),  # OpenAI : large-v2 ; ailleurs, taille inconnue
)
TTS = (
    ("gpt-4o-mini-tts", "OpenAI GPT-4o mini TTS", 0.92, True, 0),
    ("tts-1-hd", "OpenAI TTS HD", 0.88, True, 0),
    ("tts-1.6b-en_fr", "Kyutai TTS 1.6B", 0.86, True, 3.5),
    ("tts-1", "OpenAI TTS", 0.84, True, 0),
    ("xtts", "Coqui XTTS", 0.88, True, 3.0),
    ("chatterbox", "Chatterbox", 0.87, True, 3.0),
    ("orpheus", "Orpheus", 0.85, False, 4.0),
    ("kokoro", "Kokoro", 0.80, True, 0.4),
    ("melo", "MeloTTS", 0.74, True, 0.5),
    ("parler", "Parler-TTS", 0.75, False, 2.0),
    ("piper", "Piper", 0.70, True, 0.1),
)
UNKNOWN_QUALITY = 0.65
CPU_PENALTY = 0.3  # gros modèle sur le processeur : lent (les petits n'ont pas de malus)
_ENGLISH = re.compile(r"[.\-_]en(\b|$)")


def language() -> str:
    return (os.getenv("VOICE_LANGUAGE", "fr").strip().lower() or "fr")[:2]


class VoiceChoice(EmbeddingChoice):
    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind  # "stt" (dictée) ou "tts" (lecture vocale)
        self.setting = f"voice_choice_{kind}"
        self.table = STT if kind == "stt" else TTS
        self.builtin = None  # fournisseur intégré (Whisper, Kokoro) : appareil et raison du choix de sa taille
        self.reading = dict(READING_DEFAULT)  # lecture : voix, vitesse, prononciation, mélange, pauses

    def attach(self, store) -> None:
        super().attach(store)
        if self.kind == "tts":
            self.reading = {**READING_DEFAULT, **(store.get_setting(READING_SETTING) or {})}

    def set_reading(self, reading: dict) -> None:
        self.reading = reading
        if self.store:
            self.store.set_setting(READING_SETTING, reading)

    def language(self) -> str:
        return language()

    def profile_of(self, info: ModelInfo) -> dict:
        m = info.id.lower()
        for pattern, label, quality, multilingual, memory in self.table:
            if pattern in m:
                if pattern == "whisper-1" and info.hosting == "local":
                    break  # alias d'un serveur local (LocalAI…) : on ne sait pas quel Whisper il cache
                english = multilingual and bool(_ENGLISH.search(m))  # medium.en, distil-large-v3-en…
                return {
                    "label": label,
                    "quality": quality,
                    "multilingual": False if english else multilingual,
                    "memory_gb": memory,
                    "known": True,
                }
        return {"label": info.id, "quality": UNKNOWN_QUALITY, "multilingual": None, "memory_gb": None, "known": False}

    def place(self, info: ModelInfo, p: dict) -> tuple[str, float]:
        if info.provider in (LOCAL_WHISPER, LOCAL_TTS):  # intégré : il tourne là où il a été chargé
            device = getattr(self.builtin, "device", "cpu") if info.provider == LOCAL_WHISPER else "cpu"
            if device == "cuda":
                return "vram", 0.0
            return "cpu", CPU_PENALTY if (p["memory_gb"] or 0) >= 2 else 0.0
        return super().place(info, p)

    def order(self, models: list[ModelInfo], wanted: str | None, price) -> list[ModelInfo]:
        """Ordre d'essai : le modèle demandé ou fixé, puis le meilleur pour ce PC ; à qualité voisine (même
        vingtième), le Kokoro intégré si les réglages de lecture en ont besoin, puis le serveur local le moins
        occupé ; les trop gros en dernier."""
        prefer = LOCAL_TTS if self.kind == "tts" and needs_builtin(self.reading) else None
        rated = [(m, self.assess(m)) for m in models]

        def key(pair):
            m, a = pair
            return (
                wanted is not None and m.id.lower() != wanted,
                bool(self.pinned) and m.ref != self.pinned,
                a["fit"] == "too_big",
                m.hosting != "local",
                -round(a["score"] * 20),
                prefer is not None and m.provider != prefer,
                voice_split.load(m),
                price(m),
                m.ref,
            )

        return [m for m, _ in sorted(rated, key=key)]


# ---------- voix de lecture (Kokoro) : réglages de la page Modèles ----------
READING_SETTING = "voice_reading"
READING_DEFAULT = {
    "voice": None,
    "speed": 1.0,
    "pronunciation": "text",
    "blend_voice": None,
    "blend_share": 0.3,
    "pause": None,
}
# voix de Kokoro 1.0 (préfixe : langue, puis f/m : femme/homme) ; la liste du modèle intégré prime quand il est chargé
KOKORO_VOICES = (
    "af_alloy af_aoede af_bella af_heart af_jessica af_kore af_nicole af_nova af_river af_sarah af_sky am_adam "
    "am_echo am_eric am_fenrir am_liam am_michael am_onyx am_puck am_santa bf_alice bf_emma bf_isabella bf_lily "
    "bm_daniel bm_fable bm_george bm_lewis ef_dora em_alex em_santa ff_siwis hf_alpha hf_beta hm_omega hm_psi "
    "if_sara im_nicola jf_alpha jf_gongitsune jf_nezumi jf_tebukuro jm_kumo pf_dora pm_alex pm_santa zf_xiaobei "
    "zf_xiaoni zf_xiaoxiao zf_xiaoyi zm_yunjian zm_yunxi zm_yunxia zm_yunyang"
).split()
VOICE_LANGS = {
    "a": "anglais (US)",
    "b": "anglais (UK)",
    "e": "espagnol",
    "f": "français",
    "h": "hindi",
    "i": "italien",
    "j": "japonais",
    "p": "portugais",
    "z": "chinois",
}
# langue du texte (code de l'interface) → langue de prononciation de Kokoro
TEXT_LANGS = {"fr": "fr-fr", "en": "en-us", "es": "es", "it": "it", "pt": "pt-br", "ja": "ja", "zh": "cmn", "hi": "hi"}


def voice_label(voice: str) -> dict:
    lang, gender = VOICE_LANGS.get(voice[:1], "autre"), {"f": "femme", "m": "homme"}.get(voice[1:2], "")
    name = voice.split("_", 1)[-1].replace("_", " ").title()
    return {"id": voice, "label": f"{name} ({gender})" if gender else name, "language": lang}


def check_reading(body: dict, voices: list[str]) -> dict:
    """Réglages envoyés par la page Modèles, vérifiés ; ValueError avec un message clair sinon."""
    unknown = set(body) - set(READING_DEFAULT)
    if unknown:
        raise ValueError(f"Champ(s) inconnu(s) : {', '.join(sorted(unknown))}.")
    out = {**READING_DEFAULT, **body}
    for key in ("voice", "blend_voice"):
        if out[key] is not None and out[key] not in voices:
            raise ValueError(f"'{key}' : voix inconnue de Kokoro.")
    if out["pronunciation"] not in ("text", "voice"):
        raise ValueError("'pronunciation' : « text » (langue du texte) ou « voice » (langue de la voix).")
    for key, low, high in (("speed", 0.5, 2.0), ("blend_share", 0.1, 0.9), ("pause", 0.0, 1.5)):
        v = out[key]
        if key == "pause" and v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not low <= v <= high:
            raise ValueError(f"'{key}' : nombre entre {low} et {high}.")
        out[key] = round(float(v), 2)
    if out["blend_voice"] == out["voice"]:
        out["blend_voice"] = None  # mélange d'une voix avec elle-même : rien à mélanger
    return out


def needs_builtin(reading: dict) -> bool:
    """Accent, mélange ou pauses demandés : seul le Kokoro intégré sait les faire."""
    return bool(
        reading.get("voice")
        or reading.get("blend_voice")
        or reading.get("pause") is not None
        or reading.get("pronunciation") == "voice"
    )


def reading_payload(reading: dict, voice: str, language: str, builtin: bool) -> dict:
    """Champs ajoutés à la synthèse quand le client n'impose pas de voix : vitesse pour tout modèle ; prononciation,
    mélange et pauses pour le Kokoro intégré (les serveurs à part ne les connaissent pas)."""
    out = {}
    if reading.get("speed") not in (None, 1.0):
        out["speed"] = reading["speed"]
    if builtin:
        if reading.get("pronunciation", "text") == "text" and language[:2] in TEXT_LANGS:
            out["lang"] = TEXT_LANGS[language[:2]]  # une voix étrangère lit le texte avec son accent
        if reading.get("blend_voice"):
            out["blend"] = {"voice": reading["blend_voice"], "share": reading["blend_share"]}
        if reading.get("pause") is not None:
            out["pause"] = reading["pause"]
    return out
