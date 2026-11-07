"""Diagnostic de ce PC, sans ouvrir l'interface ni démarrer la gateway :

    python -m gateway.diagnostic           ce que Lezard IA voit et ce qu'il en fait, en clair
    python -m gateway.diagnostic --json    le même rapport en JSON, à joindre à un signalement de bug

Cartes graphiques (dédiée ou partie graphique du processeur, et celle qui sert aux modèles), mémoire, disque, Ollama
(installé, démarré, ses modèles) et les modèles conseillés pour ce PC. Rien n'est envoyé nulle part : Ollama est
interrogé sur cette machine seulement (OLLAMA_HOST, sinon 127.0.0.1:11434).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys

import httpx

from . import __version__, hardware, model_catalog
from .local_discovery import ollama_executable

GB = 1024**3


def ollama_status(client: httpx.Client) -> dict:
    """Ollama démarré sur ce PC ? et ses modèles installés."""
    host = os.getenv("OLLAMA_HOST", "127.0.0.1:11434")
    base = host if host.startswith("http") else f"http://{host}"
    out = {"installed": bool(ollama_executable()), "running": False, "url": base, "models": []}
    try:
        r = client.get(f"{base.rstrip('/')}/api/tags", timeout=3)
        if r.status_code == 200:
            out["running"] = True
            out["models"] = sorted(m.get("name", "") for m in r.json().get("models") or [] if m.get("name"))
    except (httpx.HTTPError, ValueError):
        pass
    return out


def collect(client: httpx.Client | None = None) -> dict:
    gpu = hardware.gpu_info(hardware.load_nvml())
    cpu, ram = hardware.cpu_ram_info(hardware.load_psutil(), first=True)
    hw = {"gpus": gpu["gpus"], "ram": ram, "disk": hardware.disk_info()}
    with client or httpx.Client() as c:
        ollama = ollama_status(c)
    reco = model_catalog.recommend(hw, installed=ollama["models"])
    return {
        "lezard_ia": __version__,
        "system": {"os": f"{platform.system()} {platform.release()}", "python": platform.python_version()},
        "gpus": gpu["gpus"],
        "gpu_error": gpu["error"],
        "budget": reco["budget"],
        "cpu": cpu,
        "ram": ram,
        "disk": hw["disk"],
        "ollama": ollama,
        "recommended": {use: reco["best"].get(use) for use in reco["uses"]},
        "uses": reco["uses"],
        "fits": {m["model"]: m["fit"] for m in reco["models"] if m["model"] in reco["best"].values()},
    }


def _gb(n) -> str:
    return f"{n / GB:.1f} Go".replace(".", ",") if n else "?"


def advice(report: dict) -> list[str]:
    """Conseils tirés du rapport (rien si tout va bien)."""
    tips = []
    cards = [g for g in report["gpus"] if g.get("kind") == "dedicated"]
    igpus = [g for g in report["gpus"] if g.get("kind") == "integrated"]
    if not report["ollama"]["installed"] and not report["ollama"]["running"]:
        tips.append("Ollama n'est pas installé : https://ollama.com/download (ou la page Modèles de Lezard IA).")
    elif not report["ollama"]["running"]:
        tips.append("Ollama est installé mais arrêté : lancez-le (menu Démarrer > Ollama), ou « ollama serve ».")
    if any(g.get("vendor") in ("amd", "intel") for g in cards) and igpus:
        tips.append(
            "Carte AMD / Intel et partie graphique du processeur : si Ollama est lent, désignez-lui la carte dédiée "
            "(variable GGML_VK_VISIBLE_DEVICES = numéro de la carte, lu dans le journal d'Ollama), puis relancez-le."
        )
    if not cards:
        tips.append("Aucune carte graphique dédiée : les modèles tournent sur le processeur (choisissez-en de petits).")
    return tips


def text(report: dict) -> str:
    lines = [f"Lezard IA {report['lezard_ia']} · {report['system']['os']} · Python {report['system']['python']}", ""]
    lines.append("Cartes graphiques :")
    for g in report["gpus"] or []:
        role = "carte dédiée" if g.get("kind") == "dedicated" else "partie graphique du processeur, ignorée"
        used = f", {_gb(g['vram_used'])} utilisés" if g.get("vram_used") is not None else ""
        lines.append(f"  - {g['name']} ({g.get('vendor', '?')}, {role}) : {_gb(g.get('vram_total'))}{used}")
    if not report["gpus"]:
        why = f" ({report['gpu_error']})" if report["gpu_error"] else ""
        lines.append(f"  aucune{why}")
    b = report["budget"]
    lines.append(
        f"  → modèles placés sur : {b['gpu']} ({_gb(b['vram'])} utilisables)"
        if b["gpu"]
        else "  → modèles sur le processeur"
    )
    ram, disk = report["ram"], report["disk"]
    lines += ["", f"Mémoire vive : {_gb(ram.get('available'))} libres sur {_gb(ram.get('total'))}"]
    lines.append(f"Disque des modèles : {_gb(disk.get('free'))} libres ({disk.get('path') or '?'})")
    o = report["ollama"]
    state = "démarré" if o["running"] else "installé, arrêté" if o["installed"] else "absent"
    lines += ["", f"Ollama : {state} ({o['url']}) · {len(o['models'])} modèle(s) : {', '.join(o['models']) or 'aucun'}"]
    lines += ["", "Modèles conseillés pour ce PC :"]
    offload = "carte + RAM, plus lent" if b["gpu"] else "RAM, sur le processeur"
    where = {"vram": "dans la carte, rapide", "offload": offload, "too_big": "trop gros"}
    for use, label in report["uses"].items():
        tag = report["recommended"].get(use)
        lines.append(
            f"  - {label} : {tag} ({where.get(report['fits'].get(tag), '?')})" if tag else f"  - {label} : aucun"
        )
    if tips := advice(report):
        lines += ["", "À faire :", *(f"  • {t}" for t in tips)]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gateway.diagnostic", description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="rapport en JSON (pour un signalement de bug)")
    args = ap.parse_args(argv)
    report = collect()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # console Windows : accents et flèches
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
