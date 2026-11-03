"""Un clic sur « Installer » d'un modèle sans Ollama : Ollama installé s'il manque (installateur de Lezard IA),
démarré, ajouté à la gateway ; la page télécharge ensuite le modèle avec ce fournisseur."""

import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.api import admin as admin_api
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER = {"Authorization": "Bearer master"}
WEB = Path(__file__).resolve().parents[1] / "gateway" / "web" / "static"
OLLAMA = {"server": "ollama", "base_url": "http://localhost:11434/v1", "models": []}


@pytest.fixture
def pc(monkeypatch, tmp_path):
    """Ce PC : Ollama absent au départ ; « installé » par lezard.py, « démarré » par start_ollama."""
    state = {"exe": None, "running": False, "calls": []}
    monkeypatch.setattr(admin_api, "ollama_executable", lambda: state["exe"])

    async def servers(client, instance_id=None):
        return [dict(OLLAMA)] if state["running"] else []

    async def start(exe, client, logs, wait_s=20):
        state["calls"].append(("start", exe))
        state["running"] = True
        return True

    def install(argv, **kw):
        state["calls"].append(("install", argv[-1]))
        state["exe"] = "C:/Ollama/ollama.exe"
        return 0

    monkeypatch.setattr(admin_api, "find_servers", servers)
    monkeypatch.setattr(admin_api, "start_ollama", start)
    monkeypatch.setattr(admin_api.subprocess, "call", install)
    monkeypatch.setattr(admin_api.updates, "ROOT", tmp_path)
    app = create_app(
        [],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))),
        plans=PlanResolver({}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        yield c, state


def wait(c):
    for _ in range(100):
        r = c.get("/v1/admin/ollama/ready", headers=MASTER).json()
        if r["state"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("toujours en cours")


def test_installs_starts_and_adds_ollama(pc, monkeypatch):
    c, state = pc
    monkeypatch.setattr(admin_api.updates, "installer_mode", lambda: True)
    assert c.post("/v1/admin/ollama/ready").status_code in (401, 403)  # clé maître seulement
    r = c.post("/v1/admin/ollama/ready", headers=MASTER)
    assert r.status_code == 202
    done = wait(c)
    assert done["state"] == "done" and done["provider"] == "ollama", done
    assert state["calls"] == [("install", "ollama-install"), ("start", "C:/Ollama/ollama.exe")]
    providers = c.get("/v1/admin/providers", headers=MASTER).json()
    names = [p["name"] for p in (providers.get("data") or providers.get("providers") or providers)]
    assert "ollama" in names  # ajouté : le modèle se télécharge aussitôt avec ce fournisseur
    # deuxième clic : rien à réinstaller ni à redémarrer
    c.post("/v1/admin/ollama/ready", headers=MASTER)
    assert wait(c)["provider"] == "ollama" and len(state["calls"]) == 2


def test_already_installed_is_only_started(pc, monkeypatch):
    c, state = pc
    state["exe"] = "C:/Ollama/ollama.exe"
    monkeypatch.setattr(admin_api.updates, "installer_mode", lambda: False)
    c.post("/v1/admin/ollama/ready", headers=MASTER)
    assert wait(c)["state"] == "done" and state["calls"] == [("start", "C:/Ollama/ollama.exe")]


def test_without_the_windows_installer_says_where_to_get_it(pc, monkeypatch):
    c, state = pc
    monkeypatch.setattr(admin_api.updates, "installer_mode", lambda: False)
    c.post("/v1/admin/ollama/ready", headers=MASTER)
    r = wait(c)
    assert r["state"] == "error" and "ollama.com" in r["error"] and not state["calls"]


def test_pages_install_the_model_with_ollama_in_one_click():
    market = (WEB / "marketplace.js").read_text(encoding="utf-8")
    models = (WEB / "models.js").read_text(encoding="utf-8")
    pull = (WEB / "ollama-pull.js").read_text(encoding="utf-8")
    assert "export async function ensureOllama" in pull and '"/v1/admin/ollama/ready"' in pull
    for js in (market, models):
        assert '"Installer + Ollama"' in js and "ensureOllama(" in js
    assert "Ollama, le moteur qui fait tourner les IA sur ce PC, puis le modèle" in market  # dit avant le clic
    assert "Vous installez aussi Ollama" in models
