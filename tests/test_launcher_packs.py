"""Lanceur de l'installateur (installer/app/lezard.py) : composants facultatifs téléchargés seulement s'ils sont
choisis, empreintes vérifiées, roues décompressées sans sortir de leur dossier, Ollama jamais touché s'il n'a pas
été installé par Lezard IA. Hors réseau : les téléchargements sont simulés."""

import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="lanceur Windows (ctypes, kernel32)")
LAUNCHER = Path(__file__).resolve().parent.parent / "installer" / "app" / "lezard.py"


@pytest.fixture
def lz(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("lezard_launcher", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "PACKS", tmp_path / "packs")
    monkeypatch.setattr(mod, "OLLAMA_MANAGED", tmp_path / "packs" / "ollama.installed")
    monkeypatch.setattr(mod, "LOGS", tmp_path / "logs")
    monkeypatch.setattr(mod, "service_running", lambda: False)
    (tmp_path / "packs").mkdir()
    return mod


def wheel(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def serve(monkeypatch, lz, files: dict[str, bytes]):
    """urlopen simulé : l'adresse donne le contenu."""

    def urlopen(url, timeout=None):
        return io.BytesIO(files[url])

    monkeypatch.setattr(lz.urllib.request, "urlopen", urlopen)


def entry(url: str, data: bytes) -> dict:
    return {"name": "demo", "version": "1.0", "url": url, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def test_a_wheel_is_extracted_like_pip_and_never_outside_its_folder(lz, tmp_path):
    good = tmp_path / "good.whl"
    good.write_bytes(
        wheel({"demo/__init__.py": b"x = 1", "demo-1.0.data/purelib/extra.py": b"", "demo-1.0.data/scripts/s.exe": b""})
    )
    target = tmp_path / "t"
    lz.extract_wheel(good, target)
    assert (target / "demo" / "__init__.py").read_bytes() == b"x = 1" and (target / "extra.py").exists()
    assert not (target / "s.exe").exists() and not (target / "scripts").exists()  # scripts : inutiles ici
    evil = tmp_path / "evil.whl"
    evil.write_bytes(wheel({"../../en-dehors.py": b"!"}))
    with pytest.raises(RuntimeError, match="chemin refusé"):
        lz.extract_wheel(evil, tmp_path / "t2")
    assert not (tmp_path / "en-dehors.py").exists()


def test_a_download_with_a_wrong_hash_or_address_is_refused(lz, monkeypatch, tmp_path):
    url, data = lz.PYPI + "aa/demo-1.0-py3-none-any.whl", b"contenu"
    serve(monkeypatch, lz, {url: data})
    dest = tmp_path / "demo.whl"
    lz.download(url, hashlib.sha256(data).hexdigest(), len(data), dest, lz.PYPI)
    assert dest.read_bytes() == data
    bad = tmp_path / "bad.whl"
    with pytest.raises(RuntimeError, match="empreinte"):
        lz.download(url, "0" * 64, len(data), bad, lz.PYPI)
    assert not bad.exists() and not bad.with_name("bad.whl.part").exists()
    with pytest.raises(RuntimeError, match="adresse refusée"):
        lz.download("https://exemple.com/demo.whl", "0" * 64, 1, bad, lz.PYPI)


def test_a_pack_is_installed_then_replaced_in_one_block(lz, monkeypatch):
    v1 = wheel({"demo/__init__.py": b"v = 1"})
    v2 = wheel({"demo/__init__.py": b"v = 2"})
    u1, u2 = lz.PYPI + "1/demo-1.0-py3-none-any.whl", lz.PYPI + "2/demo-2.0-py3-none-any.whl"
    serve(monkeypatch, lz, {u1: v1, u2: v2})
    (lz.PACKS / "voix.json").write_text(json.dumps({"pack": "voix", "wheels": [entry(u1, v1)]}), encoding="utf-8")
    lz.install_pack("voix")
    assert lz.pack_current("voix") and (lz.PACKS / "voix" / "demo" / "__init__.py").read_bytes() == b"v = 1"
    # nouvelle version de Lezard IA : manifeste changé ; un échec laisse l'ancienne version en place
    (lz.PACKS / "voix.json").write_text(
        json.dumps({"pack": "voix", "wheels": [{**entry(u2, v2), "sha256": "0" * 64}]}), encoding="utf-8"
    )
    with pytest.raises(RuntimeError):
        lz.install_pack("voix")
    assert (lz.PACKS / "voix" / "demo" / "__init__.py").read_bytes() == b"v = 1"
    (lz.PACKS / "voix.json").write_text(json.dumps({"pack": "voix", "wheels": [entry(u2, v2)]}), encoding="utf-8")
    lz.install_pack("voix")
    assert (lz.PACKS / "voix" / "demo" / "__init__.py").read_bytes() == b"v = 2"
    assert not list(lz.PACKS.glob(".*"))  # ni dossier temporaire ni cache laissés


def test_refresh_updates_only_the_packs_already_chosen(lz, monkeypatch):
    data = wheel({"demo/__init__.py": b""})
    url = lz.PYPI + "1/demo-1.0-py3-none-any.whl"
    serve(monkeypatch, lz, {url: data})
    for name in ("voix", "multi"):
        (lz.PACKS / f"{name}.json").write_text(json.dumps({"pack": name, "wheels": [entry(url, data)]}), "utf-8")
    args = lz.argparse.Namespace(action="refresh", names=[], notify=False)
    assert lz.cmd_pack(args) == 0 and not (lz.PACKS / "voix").exists()  # rien de choisi : rien de téléchargé
    lz.install_pack("voix")
    (lz.PACKS / "voix" / "pack.json").write_text("{}", encoding="utf-8")  # ancienne version
    assert lz.cmd_pack(args) == 0
    assert lz.pack_current("voix") and not (lz.PACKS / "multi").exists()


def test_ollama_installed_by_the_user_is_never_touched(lz, monkeypatch, tmp_path):
    (lz.PACKS / "ollama.json").write_text(
        json.dumps(
            {"version": "0.40.4", "url": lz.OLLAMA_RELEASES + "v0.40.4/OllamaSetup.exe", "sha256": "0", "size": 1}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(lz, "ollama_exe", lambda: "C:/ollama.exe")
    ran = []
    monkeypatch.setattr(lz.subprocess, "run", lambda *a, **k: ran.append(a))
    assert "déjà installé" in lz.install_ollama() and ran == []


def test_ollama_installed_by_lezard_is_updated_and_started_by_the_service_only(lz, monkeypatch, tmp_path):
    setup = b"installateur"
    url = lz.OLLAMA_RELEASES + "v0.40.4/OllamaSetup.exe"
    m = {"version": "0.40.4", "url": url, "sha256": hashlib.sha256(setup).hexdigest(), "size": len(setup)}
    (lz.PACKS / "ollama.json").write_text(json.dumps(m), encoding="utf-8")
    lz.OLLAMA_MANAGED.write_text("0.40.1", encoding="utf-8")
    serve(monkeypatch, lz, {url: setup})
    monkeypatch.setattr(lz, "ollama_exe", lambda: "C:/ollama.exe")
    monkeypatch.setattr(lz, "ollama_version", lambda exe: "0.40.1")
    monkeypatch.setattr(lz, "url_ok", lambda *a, **k: True)  # Ollama répond déjà : rien à démarrer
    startup = tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup.mkdir(parents=True)
    (startup / "Ollama.lnk").write_text("")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    ran = []
    monkeypatch.setattr(lz.subprocess, "run", lambda cmd, **k: ran.append(cmd))
    assert lz.install_ollama() == "Ollama 0.40.4 installé"
    assert ran[0][1:] == ["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"]
    assert not (startup / "Ollama.lnk").exists()  # une seule application : plus de démarrage à part
    assert lz.OLLAMA_MANAGED.read_text() == "0.40.4" and not (lz.PACKS / "OllamaSetup.exe").exists()
    monkeypatch.setattr(lz, "ollama_version", lambda exe: "0.40.4")
    assert lz.install_ollama() == "Ollama 0.40.4 à jour"


def test_tools_chosen_at_install_are_written_without_touching_the_rest(lz, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("GATEWAY_MASTER_KEY=gw-secret\nTOOLS_PC=true\n", encoding="utf-8")
    monkeypatch.setattr(lz, "ENV_FILE", env)
    assert lz.cmd_tools(lz.argparse.Namespace(choices=["web=off", "pc=off"])) == 0
    text = env.read_text(encoding="utf-8")
    assert "GATEWAY_MASTER_KEY=gw-secret" in text and "TOOLS_PC=false" in text and "TOOLS_WEB=false" in text
    assert text.count("TOOLS_PC=") == 1
    assert lz.cmd_tools(lz.argparse.Namespace(choices=["web=peut-être"])) == 2


def test_uninstall_leaves_another_installation_alone(lz, tmp_path, monkeypatch):
    """Une copie de test (ou un 2e dossier) désinstallée ne retire ni les raccourcis ni l'entrée Windows d'une autre."""
    import uuid
    import winreg

    ran = []
    monkeypatch.setattr(lz.subprocess, "run", lambda argv, **k: ran.append(argv))
    lz.remove_shortcuts(tmp_path)
    script = ran[0][-1]
    assert f"$root = '{tmp_path.resolve()}\\'" in script and ".StartsWith($root" in script  # cible dans CE dossier
    assert (
        "Remove-Item -Force (Join-Path" not in script and "Remove-Item -Recurse" not in script
    )  # plus d'effacement aveugle
    key = rf"Software\LezardIA-tests\{uuid.uuid4().hex}"
    monkeypatch.setattr(lz, "UNINSTALL_KEY", key)
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, str(tmp_path / "autre"))
        assert not lz.registered_here(tmp_path) and lz.registered_here(tmp_path / "autre")
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\LezardIA-tests")
        except OSError:
            pass
    assert not lz.registered_here(tmp_path)  # pas d'entrée : rien à retirer
