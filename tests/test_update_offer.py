"""Mises à jour proposées à chaque lancement (version signée lue sur GitHub), clé maître sans copier-coller sur le
PC, installateur sans choix d'outils (sauf le web) : tout le reste s'installe depuis la marketplace."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import updates
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

from .test_permissions import MASTER, Upstream

ROOT = Path(__file__).resolve().parent.parent


def test_updates_are_read_on_github_at_every_launch(monkeypatch):
    monkeypatch.delenv("LEZARD_UPDATE_URL", raising=False)
    assert updates.release_url() == "https://raw.githubusercontent.com/Lezardu29/lezard-ia-telecharger/main/latest.json"
    checks = []

    class Checker(updates.UpdateChecker):
        def check(self):
            checks.append(1)
            raise asyncio.CancelledError  # une seule vérification suffit au test

    monkeypatch.setattr(updates, "STARTUP_DELAY", 0)
    monkeypatch.setenv("GATEWAY_UPDATE_CHECK", "on")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Checker(DocumentStore(":memory:")).run())
    assert checks == [1]  # au lancement, sans attendre un jour


def test_me_offers_the_new_version_to_the_master_key(monkeypatch):
    store = DocumentStore(":memory:")
    store.set_setting("update_check", {"at": 1, "available": True, "version": "9.9.9"})
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(Upstream())),
        plans=PlanResolver({}, "free", master_key="master"),
        store=store,
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        u = c.get("/v1/me", headers=MASTER).json()["update"]
        assert u["available"] and u["version"] == "9.9.9" and u["installable"] is updates.installer_mode()
    js = (ROOT / "gateway" / "web" / "static" / "update-offer.js").read_text(encoding="utf-8")
    assert '"/v1/admin/update/install"' in js and "Plus tard" in js  # proposé, installé seulement sur demande
    assert "offerUpdate(me)" in (ROOT / "gateway" / "web" / "static" / "app.js").read_text(encoding="utf-8")


def test_installer_offers_only_web_choices():
    iss = (ROOT / "installer" / "lezard-ia.iss").read_text(encoding="utf-8-sig")
    tasks = [line.split('"')[1] for line in iss.splitlines() if line.startswith('Name: "') and "Description" in line]
    assert {"web", "lan"} <= set(tasks) and not {"pc", "voix", "multi"} & set(tasks)
    assert "pack install" not in iss  # voix, multi-agents : depuis la marketplace
    assert "Result := 'web=' + OnOff('web');" in iss  # une mise à jour ne touche jamais au contrôle du PC


@pytest.mark.skipif(sys.platform != "win32", reason="lanceur Windows (ctypes, kernel32)")
def test_launcher_opens_lezard_ia_already_signed_in(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("lezard_launcher", ROOT / "installer" / "app" / "lezard.py")
    lz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lz)
    env = tmp_path / ".env"
    env.write_text("GATEWAY_MASTER_KEY=ab+c/d=e\nGATEWAY_PORT=8123\n", encoding="utf-8")
    monkeypatch.setattr(lz, "ENV_FILE", env)
    monkeypatch.setattr(lz, "healthy", lambda p: True)
    opened = []
    monkeypatch.setattr(lz.webbrowser, "open", opened.append)
    assert lz.cmd_open(None) == 0
    assert opened == ["http://localhost:8123/#key=ab%2Bc%2Fd%3De"]  # fragment : jamais envoyé au serveur


@pytest.mark.skipif(sys.platform != "win32", reason="lanceur Windows (ctypes, kernel32)")
def test_service_window_ends_with_the_master_key(tmp_path, monkeypatch, capsys):
    """Fenêtre « Lezard IA » du service : une fois tout lancé, l'adresse puis la clé maître en dernière ligne ; la
    clé n'est jamais écrite dans le journal. Le service s'ouvre dans sa fenêtre (python.exe, pas pythonw)."""
    spec = importlib.util.spec_from_file_location("lezard_launcher", ROOT / "installer" / "app" / "lezard.py")
    lz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lz)
    env = tmp_path / ".env"
    env.write_text("GATEWAY_MASTER_KEY=sk-cle-maitre-de-test\nGATEWAY_PORT=8123\n", encoding="utf-8")
    monkeypatch.setattr(lz, "ENV_FILE", env)
    monkeypatch.setattr(lz, "LOGS", tmp_path / "logs")
    monkeypatch.setattr(lz.sys.stdout, "isatty", lambda: True)
    lz.Supervisor().announce()
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1] == "Clé maître : sk-cle-maitre-de-test"
    assert "http://localhost:8123" in lines[0]
    assert "sk-cle" not in (tmp_path / "logs" / "service.log").read_text(encoding="utf-8")
    started = []
    monkeypatch.setattr(lz.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw["creationflags"])))
    lz.start_service()
    assert started[0][0][0].endswith("python.exe") and started[0][1] == lz.CREATE_NEW_CONSOLE
    iss = (ROOT / "installer" / "lezard-ia.iss").read_text(encoding="utf-8-sig")
    assert 'pythonw.exe"; Parameters: """{app}\\lezard.py"" service' not in iss  # jamais sans fenêtre


def test_updates_are_checked_every_five_minutes(monkeypatch):
    """Au lancement, puis toutes les 5 minutes ; la page ouverte relit aussi l'état toutes les 5 minutes."""
    assert updates.INTERVAL == 5 * 60
    slept, checks = [], []

    async def sleep(seconds):
        slept.append(seconds)
        if len(slept) > 3:
            raise asyncio.CancelledError

    class Checker(updates.UpdateChecker):
        def check(self):
            checks.append(1)

    monkeypatch.setattr(updates.asyncio, "sleep", sleep)
    monkeypatch.setenv("GATEWAY_UPDATE_CHECK", "on")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Checker(DocumentStore(":memory:")).run())
    assert slept == [updates.STARTUP_DELAY, 300, 300, 300] and len(checks) == 3
    js = (ROOT / "gateway" / "web" / "static" / "update-offer.js").read_text(encoding="utf-8")
    assert "const EVERY = 5 * 60 * 1000;" in js and "export function watchUpdates()" in js
    assert 'rawApi("/v1/me")' in js  # jamais de demande de code de double authentification pour ça
    assert "watchUpdates()" in (ROOT / "gateway" / "web" / "static" / "app.js").read_text(encoding="utf-8")
