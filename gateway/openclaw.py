"""OpenClaw dans le chat (/openclaw, chat_tools.py) : ses réglages pour Lezard IA, à part dans data/openclaw (jamais
ceux de ~/.openclaw, que l'utilisateur garde pour son propre usage dans un terminal), et la lecture de sa réponse.
La clé de l'appelant n'est donnée qu'à la tâche (variable LEZARD_API_KEY), jamais écrite sur le disque."""

from __future__ import annotations

import json
import os
from pathlib import Path

TIMEOUT = 600  # secondes : une tâche d'agent sur une IA locale peut être longue


def home() -> Path:
    return Path(os.getenv("GATEWAY_DATA_DIR", "data")) / "openclaw"


def write_config(port: int, model: str | None) -> Path:
    """Réglages d'OpenClaw pour Lezard IA, réécrits à chaque demande (port et modèle du moment)."""
    entry = lambda mid, name: {  # noqa: E731
        "id": mid,
        "name": name,
        "contextWindow": 32768,
        "compat": {"supportsReasoningEffort": False},
    }
    models = [entry("auto", "Lezard IA (auto)"), entry("auto/economy", "Lezard IA (éco : IA locales d'abord)")]
    if model and model not in ("auto", "auto/economy"):
        models.append(entry(model, model))
    config = {
        "models": {
            "providers": {
                "lezard": {
                    "baseUrl": f"http://127.0.0.1:{port}/v1",
                    "apiKey": "${LEZARD_API_KEY}",  # donnée à la tâche seulement
                    "api": "openai-completions",
                    "timeoutSeconds": TIMEOUT,
                    "models": models,
                }
            }
        },
        "agents": {"defaults": {"model": {"primary": f"lezard/{model or 'auto'}"}, "timeoutSeconds": TIMEOUT}},
    }
    path = home() / "openclaw.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def answer_of(stdout: str) -> tuple[str, str | None, str | None]:
    """(texte de la réponse, erreur, modèle) d'après la sortie JSON d'« openclaw agent --json »."""
    try:
        data = json.loads(stdout[stdout.find("{") :]) if "{" in stdout else {}
    except ValueError:
        data = {}
    meta = data.get("meta") or {}
    parts = [p for p in data.get("payloads") or [] if isinstance(p, dict)]
    text = "\n\n".join(str(p.get("text") or "") for p in parts if not p.get("isError")).strip()
    error = next((str(p.get("text")) for p in parts if p.get("isError") and p.get("text")), None)
    if not text and not error:
        error = str(meta.get("error") or "") or None
    model = (meta.get("executionTrace") or {}).get("winnerModel")
    return text, error, model
