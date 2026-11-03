"""Voix répartie entre plusieurs serveurs locaux, comme le découpage des longs messages (13.4).

Dictée : un enregistrement long (TRANSCRIPTION_SPLIT_SECONDS, 60 s par défaut) est coupé aux silences en morceaux
d'environ SEGMENT_SECONDS, transcrits en parallèle (un worker par serveur local de transcription, chacun prenant le
morceau suivant dans la file), puis recollés dans l'ordre. Un seul serveur local : pas de découpage, Whisper lit un
long audio d'un tenant et le couper ne ferait que l'allonger. Audio décodé par PyAV (installé avec faster-whisper),
sinon WAV 16 bits seulement ; rien n'est écrit sur le disque.
Lecture : les morceaux de texte que le chat demande en même temps vont au serveur local le moins occupé (`load`).
"""

from __future__ import annotations

import io
import os
import wave
from array import array
from collections import Counter
from contextlib import contextmanager

from .registry import ModelInfo

RATE = 16_000  # Whisper travaille en 16 kHz mono
SEGMENT_SECONDS = 30  # morceau visé ; la coupure est cherchée au silence le plus net autour
SEARCH_SECONDS = 8  # de part et d'autre de la coupure visée
FRAME_SECONDS = 0.02
QUIET_STEP = 512  # niveaux arrondis : les petites différences de la compression ne priment pas sur la proximité

IN_FLIGHT: Counter[str] = Counter()  # demandes de voix en cours, par fournisseur


def split_seconds() -> float:
    return float(os.getenv("TRANSCRIPTION_SPLIT_SECONDS", "60"))


@contextmanager
def using(provider: str):
    IN_FLIGHT[provider] += 1
    try:
        yield
    finally:
        IN_FLIGHT[provider] -= 1
        if IN_FLIGHT[provider] <= 0:
            del IN_FLIGHT[provider]


def load(info: ModelInfo) -> int:
    """Clé de tri : à rang égal, le serveur local qui a le moins de demandes en cours passe en premier."""
    return IN_FLIGHT[info.provider] if info.hosting == "local" else 0


def local_servers(chain: list[ModelInfo]) -> list[ModelInfo]:
    """Un modèle par serveur local, dans l'ordre de la chaîne : les workers du découpage."""
    seen, out = set(), []
    for m in chain:
        if m.hosting == "local" and m.provider not in seen:
            seen.add(m.provider)
            out.append(m)
    return out


# ---------- audio ----------
def decode(data: bytes, kind: str) -> bytes | None:
    """PCM 16 bits mono 16 kHz, ou None si le format ne peut pas être décodé ici."""
    try:
        import av
    except ImportError:
        return _wav_pcm(data) if kind == "wav" else None
    try:
        out = bytearray()
        with av.open(io.BytesIO(data)) as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
            for frame in container.decode(audio=0):
                for f in resampler.resample(frame):
                    out += bytes(f.planes[0])[: f.samples * 2]
            for f in resampler.resample(None):
                out += bytes(f.planes[0])[: f.samples * 2]
        return bytes(out)
    except Exception:  # fichier abîmé ou codec absent : transcrit d'un seul tenant, comme avant
        return None


def _wav_pcm(data: bytes) -> bytes | None:
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1 or w.getframerate() != RATE:
                return None
            return w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return None


def cuts(samples: array, rate: int = RATE) -> list[tuple[int, int]]:
    """Morceaux (début, fin) d'environ SEGMENT_SECONDS, coupés au passage le plus calme autour de chaque coupure
    visée (amplitude crête à crête par tranche de 20 ms, arrondie ; à égalité, la plus proche)."""
    n, target = len(samples), int(SEGMENT_SECONDS * rate)
    frame, reach = int(FRAME_SECONDS * rate), int(SEARCH_SECONDS * rate)
    out, start = [], 0
    while n - start > target * 1.5:
        aim = start + target
        best, cut = None, aim
        for pos in range(max(start + frame, aim - reach), min(n - frame, aim + reach), frame):
            chunk = samples[pos : pos + frame]
            score = ((max(chunk) - min(chunk)) // QUIET_STEP, abs(pos - aim))
            if best is None or score < best:
                best, cut = score, pos + frame // 2
        out.append((start, cut))
        start = cut
    out.append((start, n))
    return out


def wav(pcm: bytes, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def split_audio(data: bytes, kind: str) -> list[tuple[bytes, float]] | None:
    """Morceaux WAV (données, durée en s) d'un enregistrement assez long pour être découpé ; None sinon."""
    pcm = decode(data, kind)
    if not pcm or len(pcm) / 2 / RATE < split_seconds():
        return None
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    return [(wav(samples[a:b].tobytes()), (b - a) / RATE) for a, b in cuts(samples)]
