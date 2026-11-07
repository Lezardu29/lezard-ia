"""python -m gateway.diagnostic : sur un PC Ryzen + Radeon RX 9070 XT avec Ollama démarré, le rapport nomme la carte
qui sert aux modèles, écarte la partie graphique du processeur et donne les bons conseils ; --json pour un bug."""

import json

import httpx

from gateway import diagnostic, gpus, hardware

from .test_gpus import RYZEN_9070XT


def ryzen_with_ollama(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda name: None)
    monkeypatch.setattr(hardware, "other_gpus", lambda: gpus.from_dxgi(RYZEN_9070XT))
    monkeypatch.setattr(diagnostic, "ollama_executable", lambda: "C:/Ollama/ollama.exe")
    monkeypatch.setattr(
        hardware, "cpu_ram_info", lambda ps, first: ({}, {"total": 32 * 1024**3, "available": 24 * 1024**3})
    )
    tags = {"models": [{"name": "qwen2.5:14b"}]}
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=tags) if r.url.path == "/api/tags" else httpx.Response(404)
    )
    real = httpx.Client
    monkeypatch.setattr(diagnostic.httpx, "Client", lambda *a, **k: real(transport=transport))


def test_report_names_the_radeon_and_ignores_the_ryzen_graphics(monkeypatch, capsys):
    ryzen_with_ollama(monkeypatch)
    assert diagnostic.main([]) == 0
    out = capsys.readouterr().out
    assert "AMD Radeon RX 9070 XT (amd, carte dédiée) : 16,0 Go, 1,0 Go utilisés" in out
    assert "AMD Radeon(TM) Graphics (amd, partie graphique du processeur, ignorée)" in out
    assert "→ modèles placés sur : AMD Radeon RX 9070 XT" in out
    assert "Ollama : démarré" in out and "qwen2.5:14b" in out
    assert "dans la carte, rapide" in out and "GGML_VK_VISIBLE_DEVICES" in out  # conseil AMD + partie intégrée
    assert "pas installé" not in out


def test_json_report_for_a_bug_report(monkeypatch, capsys):
    ryzen_with_ollama(monkeypatch)
    assert diagnostic.main(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["budget"]["gpu"] == "AMD Radeon RX 9070 XT" and report["ollama"]["running"] is True
    assert [g["kind"] for g in report["gpus"]] == ["integrated", "dedicated"]


def test_without_ollama_or_card_it_says_what_to_do(monkeypatch):
    monkeypatch.setattr(diagnostic, "ollama_executable", lambda: None)
    report = {"gpus": [], "ollama": {"installed": False, "running": False}}
    tips = diagnostic.advice(report)
    assert any("ollama.com/download" in t for t in tips) and any("processeur" in t for t in tips)
