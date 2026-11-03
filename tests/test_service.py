"""Démarrage automatique et supervision (18.1) : redémarrage demandé (code 3), lancer.ps1 -Install / -Service."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.serve import RESTART_EXIT
from tests.test_lan_tls import free_port

ROOT = Path(__file__).resolve().parents[1]
MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}


def make(peer=("127.0.0.1", 50000)):
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
    )
    return app, TestClient(app, client=peer)


def test_restart_endpoint():
    app, c = make()
    with c:
        assert c.get("/v1/admin/backups", headers=MASTER).json()["can_restart"] is False
        r = c.post("/v1/admin/restart", headers=MASTER)
        assert r.status_code == 409 and "superviseur" in r.text  # lancée à la main : pas de relance
        calls = []
        app.state.request_restart = lambda: calls.append(time.time())
        assert c.get("/v1/admin/backups", headers=MASTER).json()["can_restart"] is True
        assert c.post("/v1/admin/restart").status_code in (401, 403)  # clé maître requise
        asked = time.time()
        assert c.post("/v1/admin/restart", headers=MASTER).json() == {"restarting": True}
        for _ in range(50):
            if calls:
                break
            time.sleep(0.05)
        assert calls and calls[0] - asked >= 0.4  # la réponse part avant l'arrêt


def test_restart_from_this_pc_only():
    app, c = make(peer=("203.0.113.5", 50000))
    with c:
        app.state.request_restart = lambda: pytest.fail("redémarrage déclenché à distance")
        r = c.post("/v1/admin/restart", headers=MASTER)
        assert r.status_code == 403 and "ce PC" in r.text


def run_serve(tmp_path, supervised: bool):
    port = free_port()
    env = {
        **os.environ,
        "GATEWAY_DB": str(tmp_path / "g.db"),
        "GATEWAY_MASTER_KEY": MASTER_KEY,
        "GATEWAY_PORT": str(port),
        "GATEWAY_BACKUP": "off",
        "GATEWAY_BIND": "127.0.0.1",
    }
    env.pop("GATEWAY_TLS_PORT", None)
    if supervised:
        env["GATEWAY_SUPERVISED"] = "1"
    else:
        env.pop("GATEWAY_SUPERVISED", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "gateway.serve", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                return proc, base
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    proc.kill()
    pytest.fail(f"gateway.serve n'a pas démarré : {proc.stderr.read().decode(errors='replace')[-2000:]}")


def test_supervised_serve_exits_with_restart_code(tmp_path):
    """Vrai serveur : POST /v1/admin/restart → arrêt propre avec le code 3, que lancer.ps1 / systemd relancent."""
    proc, base = run_serve(tmp_path, supervised=True)
    try:
        assert httpx.get(f"{base}/v1/admin/backups", headers=MASTER).json()["can_restart"] is True
        assert httpx.post(f"{base}/v1/admin/restart", headers=MASTER).json() == {"restarting": True}
        assert proc.wait(timeout=30) == RESTART_EXIT == 3
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stderr.close()


def test_unsupervised_serve_refuses_restart(tmp_path):
    proc, base = run_serve(tmp_path, supervised=False)
    try:
        assert httpx.post(f"{base}/v1/admin/restart", headers=MASTER).status_code == 409
        assert proc.poll() is None  # toujours en marche
    finally:
        proc.kill()
        proc.wait(timeout=10)
        proc.stderr.close()


# ---------- lancer.ps1 ----------


def launcher() -> str:
    return (ROOT / "lancer.ps1").read_text(encoding="utf-8-sig")


def test_launcher_parses():
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell absent")
    path = str(ROOT / "lancer.ps1").replace("'", "''")
    script = (
        f"$e = $null; [System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$null, [ref]$e)"
        ' | Out-Null; if ($e) { $e | ForEach-Object { "$($_.Extent.StartLineNumber): $($_.Message)" }; exit 1 }'
    )
    r = subprocess.run([shell, "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_launcher_service_install():
    text = launcher()
    for switch in ("$Service", "$Install", "$Uninstall", "$Stop", "$Status"):
        assert f"[switch]{switch}" in text
    # tâche à l'ouverture de session, sous le compte de l'utilisateur, sans droits administrateur ni limite de durée
    assert "New-ScheduledTaskTrigger -AtLogOn -User $user" in text and "-RunLevel Limited" in text
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in text and "-MultipleInstances IgnoreNew" in text
    assert 'GetFolderPath("Startup")' in text  # repli sans Planificateur
    # la commande du service ne contient aucun secret (visible dans le Planificateur de tâches)
    args = next(line for line in text.splitlines() if line.strip().startswith("$serviceArgs = "))
    assert "-Service" in args and "-WindowStyle Minimized" in args and "$env:" not in args
    # un seul terminal : tout y est attaché (le fermer arrête tout), SearXNG et ComfyUI compris, relancés au besoin
    assert text.count("-WindowStyle Hidden") == 1 and text.count("-NoNewWindow") >= 5  # 1 : tâche du jour
    assert "SEARXNG_HOME" in text and "COMFYUI_HOME" in text and "Watch-Companions" in text
    assert "--listen 127.0.0.1" in text  # ComfyUI jamais exposé au réseau
    assert "$old = Stop-RecordedProcesses" in text  # restes d'une fenêtre fermée : nettoyés au démarrage
    assert "DAILY_SCRIPT" in text and "AddHours(1)" in text  # tâche du jour : nouvel essai 1 h après un échec
    # superviseur : gateway.serve, relance immédiate sur le code 3, attente croissante sinon
    assert "-m gateway.serve --port $Port" in text and "-m uvicorn" not in text
    assert '$env:GATEWAY_SUPERVISED = "1"' in text and "if ($code -eq 3)" in text
    assert "[Math]::Min($delay * 2, 300)" in text
    # -Stop ne tue que les processus du service, reconnus à leur heure de démarrage (PID réutilisés)
    assert 'StartTime.ToString("yyyyMMddHHmmssfffffff") -eq' in text
    # sans surveillance : pas d'installation de la dictée locale (centaines de Mo)
    assert "$LASTEXITCODE -ne 0 -and -not $Service" in text


@pytest.mark.skipif(not (ROOT / "deploy").is_dir(), reason="deploy/ : seulement dans le dépôt privé")
def test_systemd_unit():
    unit = (ROOT / "deploy" / "gateway.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/smart-ai-gateway/.venv/bin/python -m gateway.serve" in unit
    assert "Environment=GATEWAY_SUPERVISED=1" in unit and "Restart=always" in unit
    assert "ReadWritePaths=/opt/smart-ai-gateway/data" in unit and "ProtectSystem=strict" in unit
