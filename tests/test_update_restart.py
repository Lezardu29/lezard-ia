"""Mise à jour depuis le bandeau : l'installateur n'est jamais arrêté avec la gateway, et Lezard IA redémarre.

Avant : l'installateur, lancé par la gateway, appelait « lezard.py stop », qui arrête la gateway avec TOUT son arbre
(taskkill /T) : l'installateur était arrêté avec elle, sans rien installer ni relancer Lezard IA."""

import ctypes
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from gateway import updates

ROOT = Path(__file__).resolve().parents[1]
ISS = (ROOT / "installer" / "lezard-ia.iss").read_text(encoding="utf-8-sig")


def load_stopper():
    """Le script d'arrêt de l'installateur, chargé sur tout système : hors Windows, kernel32 est un faux (les
    tests remplacent is_python et stop_one, seuls à l'appeler), pour vérifier l'ordre d'arrêt dans la CI Linux."""
    spec = importlib.util.spec_from_file_location("arret_maj", ROOT / "installer" / "arret-maj.py")
    mod = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as mp:
        if not hasattr(ctypes, "WinDLL"):
            mp.setattr(ctypes, "WinDLL", lambda *a, **k: mock.MagicMock(), raising=False)
        spec.loader.exec_module(mod)
    return mod


def test_installer_stops_each_process_alone_never_the_tree():
    prepare = ISS.split("function PrepareToInstall")[1].split("\nend;")[0]
    assert "ExtractTemporaryFile('arret-maj.py')" in prepare and "arret-maj.py" in prepare
    assert 'lezard.py" stop' not in prepare  # « stop » arrête l'arbre de la gateway, installateur compris
    assert 'Source: "arret-maj.py"; Flags: dontcopy' in ISS  # fourni par l'installateur, quelle que soit la version
    script = (ROOT / "installer" / "arret-maj.py").read_text(encoding="utf-8")
    assert '["taskkill"' not in script and '"tasklist"' not in script  # ni arbre (/T), ni sortie vide
    assert "kernel32.TerminateProcess(h, 1)" in script  # ce processus seul
    # relancé à la fin, même sans démarrage automatique
    assert "Check: WizardSilent and not WizardIsTaskSelected('autostart')" in ISS


def test_stopper_stops_the_service_first_then_the_gateway(tmp_path, monkeypatch):
    stopper = load_stopper()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "launcher.json").write_text(json.dumps({"pid": 111, "port": 8790, "gateway": 222}))
    (tmp_path / "data" / "launcher.stop").write_text("1")
    alive, killed = {111, 222}, []

    def stop_one(pid):
        killed.append(pid)
        alive.discard(pid)

    monkeypatch.setattr(stopper, "is_python", lambda pid: pid in alive)
    monkeypatch.setattr(stopper, "stop_one", stop_one)
    assert stopper.main(tmp_path) == 0
    assert killed == [111, 222]  # le service d'abord : il relancerait la gateway
    assert not (tmp_path / "data" / "launcher.stop").exists()


def test_stopper_without_a_running_service(tmp_path, monkeypatch):
    stopper = load_stopper()
    monkeypatch.setattr(stopper, "stop_one", lambda pid: pytest.fail("rien à arrêter"))
    assert stopper.main(tmp_path) == 0  # première installation : aucun état


def test_gateway_launches_the_installer_outside_its_tree(tmp_path, monkeypatch):
    calls = []

    class Proc:
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(updates.subprocess, "Popen", lambda argv, **kw: calls.append(argv) or Proc())
    updates.launch_installer(tmp_path / "LezardIA-Setup-9.9.9.exe", tmp_path / "installation.log")
    argv = calls[0]
    assert argv[:2] == [sys.executable, "-c"] and "0x8 | 0x200 | 0x1000000" in argv[2]  # intermédiaire détaché
    assert argv[3].endswith("LezardIA-Setup-9.9.9.exe") and "/VERYSILENT" in argv
    if sys.platform == "win32":  # drapeaux de création de Windows (DETACHED_PROCESS…) : sans objet ailleurs
        monkeypatch.undo()  # le vrai Popen : l'intermédiaire lancé pour de bon
        out = subprocess.run([sys.executable, "-c", argv[2], sys.executable, "-c", "pass"], timeout=30)
        assert out.returncode == 0  # l'intermédiaire rend la main aussitôt


def test_stopper_also_stops_other_programs_run_with_the_installed_python(tmp_path, monkeypatch):
    """1.0.23 : le serveur MCP « lezard » que Claude Code gardait ouvert tenait python/libcrypto-3.dll ; l'installateur
    abandonnait et Lezard IA restait arrêté. Tout programme lancé avec le Python de l'installation est arrêté."""
    stopper = load_stopper()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "launcher.json").write_text(json.dumps({"pid": 111, "gateway": 222}))
    mine = stopper.os.getpid()
    python = str(tmp_path / "python" / "python.exe")
    running = {111: python, 222: python, 333: python, mine: python, 444: r"C:\Autre\python\python.exe", 555: ""}
    killed = []

    def stop_one(pid):
        killed.append(pid)
        running.pop(pid)

    monkeypatch.setattr(stopper, "all_pids", lambda: list(running))
    monkeypatch.setattr(stopper, "program", lambda pid: running.get(pid, ""))
    monkeypatch.setattr(stopper, "is_python", lambda pid: pid in running)
    monkeypatch.setattr(stopper, "stop_one", stop_one)
    assert stopper.others(tmp_path) == [111, 222, 333]  # jamais ce script, ni le Python d'un autre dossier
    assert stopper.main(tmp_path) == 0
    assert killed == [111, 222, 333] and set(running) == {mine, 444, 555}
    # sans service lancé (fenêtre fermée à la main) : le serveur MCP resté ouvert est arrêté quand même
    (tmp_path / "data" / "launcher.json").unlink()
    running[666] = python
    assert stopper.main(tmp_path) == 0 and killed[-1] == 666


def test_stopper_that_cannot_free_the_files_says_so(tmp_path, monkeypatch):
    stopper = load_stopper()
    python = str(tmp_path / "python" / "python.exe")
    monkeypatch.setattr(stopper, "all_pids", lambda: [777])
    monkeypatch.setattr(stopper, "program", lambda pid: python)
    monkeypatch.setattr(stopper, "stop_one", lambda pid: "arrêt refusé")
    monkeypatch.setattr(stopper.time, "sleep", lambda s: None)
    assert stopper.main(tmp_path) == 1
    assert "toujours en cours : [777]" in (tmp_path / "data" / "logs" / "arret-maj.log").read_text(encoding="utf-8")


def test_installer_restarts_lezard_ia_when_the_copy_fails_after_the_stop():
    assert "procedure DeinitializeSetup();" in ISS and "StoppedApp := App;" in ISS
    net = ISS.split("procedure DeinitializeSetup();")[1].split("\nend;")[0]
    assert "(StoppedApp <> '') and not Installed" in net and 'lezard.py" service' in net and "ewNoWait" in net
    assert "if CurStep = ssPostInstall then\n    Installed := True;" in ISS.replace("\r\n", "\n")


@pytest.mark.skipif(sys.platform != "win32", reason="instantané des processus de Windows")
def test_real_process_list_sees_this_python():
    stopper = load_stopper()
    assert stopper.os.getpid() in stopper.all_pids()
    assert Path(stopper.program(stopper.os.getpid())).name.lower().startswith("python")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        app = Path(sys.executable).parent.parent / "inexistant"  # others() compare un dossier : aucun ici
        assert stopper.others(app) == [] and child.pid in stopper.all_pids()
    finally:
        child.kill()
