"""Priorité et bridage d'énergie (mode économie) : essayés pour de vrai sur un processus enfant jetable, jamais sur
un autre programme."""

import os
import subprocess
import sys

import pytest

from gateway import power


@pytest.fixture
def child():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield p.pid
    finally:
        p.kill()
        p.wait()


def test_find_processes_sees_python():
    exe = os.path.basename(sys.executable).lower()
    names = {exe, exe.removesuffix(".exe")}
    if sys.platform != "win32":  # /proc/<pid>/comm : nom court du programme (python3.13…)
        with open(f"/proc/{os.getpid()}/comm", encoding="utf-8") as f:
            names.add(f.read().strip().lower())
    assert os.getpid() in power.find_processes(names)
    assert power.find_processes({"programme-qui-n-existe-pas.exe"}) == []


def test_lower_then_restore_a_child(child):
    before = power.get_priority(child)
    assert before is not None
    assert power.set_priority(child, power.low_value())
    assert power.get_priority(child) == power.low_value()
    if sys.platform == "win32":
        assert power.set_power_throttling(True, child) and power.set_power_throttling(None, child)
        assert power.set_priority(child, before) and power.get_priority(child) == before
    else:  # sans droits, Linux interdit de redescendre « nice » : la restauration échoue proprement
        assert power.set_power_throttling(True, child) is False
        assert power.set_priority(child, before) in (True, False)


def test_unknown_pid_is_harmless():
    assert power.get_priority(2**22 + 7) is None
    assert power.set_priority(2**22 + 7, power.low_value()) is False
    assert power.set_power_throttling(True, 2**22 + 7) is False


def test_own_process_throttling():
    expected = sys.platform == "win32"
    assert power.set_power_throttling(False) is expected
    assert power.set_power_throttling(None) is expected
