"""Installation depuis le zip, sans installateur : la version signée annonce aussi son zip, vérifié (taille, SHA-256)
puis extrait ; mise_a_jour.py remplace le programme sans toucher aux données, et remet l'ancien si le nouveau ne
répond pas."""

import hashlib
import importlib.util
import io
import json
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gateway import release, updates
from gateway.license import public_key_b64
from gateway.release import ReleaseError, sign, verify
from gateway.store import DocumentStore
from gateway.updates import UpdateChecker, UpdateError

HERE = Path(__file__).resolve().parent.parent
SETUP = b"MZ installateur factice"


def archive(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


ZIP = archive(
    {
        "Lezard IA/python/python.exe": b"MZ python",
        "Lezard IA/python/python313.dll": b"MZ dll",
        "Lezard IA/python/Lib/site-packages/dep.py": b"# dependance",
        "Lezard IA/gateway/__init__.pyc": b"code",
        "Lezard IA/lezard.py": b"# lanceur",
        "Lezard IA/mise_a_jour.py": b"# mise a jour",
        "Lezard IA/arret-maj.py": b"# arret",
        "Lezard IA/version.txt": b"1.1.0\n",
    }
)


@pytest.fixture
def key(monkeypatch):
    k = Ed25519PrivateKey.generate()
    monkeypatch.setattr(release, "PUBLIC_KEYS", {"u1": public_key_b64(k)})
    monkeypatch.setenv("LEZARD_UPDATE_URL", "https://compte.test/api/updates/latest")
    return k


def announce(key, version, zip_data=ZIP, with_zip=True, signed=False):
    manifest = {"version": version, "url": f"https://dl.test/LezardIA-Setup-{version}.exe",
                "sha256": hashlib.sha256(SETUP).hexdigest(), "size": len(SETUP), "notes": "",
                "published": int(time.time())}  # fmt: skip
    if signed:
        manifest["signed"] = True
    if with_zip:
        manifest["zip"] = {"url": f"https://dl.test/LezardIA-{version}.zip",
                           "sha256": hashlib.sha256(zip_data).hexdigest(), "size": len(zip_data)}  # fmt: skip
    return sign(key, "u1", manifest)


def server(token, files):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "compte.test":
            return httpx.Response(200, json={"release": token})
        data = files.get(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, content=data) if data is not None else httpx.Response(404)

    return httpx.MockTransport(handle)


def checker(tmp_path, transport, launched, package="zip"):
    c = UpdateChecker(
        DocumentStore(":memory:"),
        transport=transport,
        launcher=lambda path, log, *extra: launched.append(("exe", path, *extra)),
        zip_launcher=lambda *args: launched.append(("zip", *args)),
        folder=tmp_path / "updates",
    )
    c.app = tmp_path / "app"
    c._info = {"version": "1.0.0", "commit": None, "source": "installer", "package": package}
    return c


def test_announcement_with_zip_is_verified(key):
    data = verify(announce(key, "1.1.0"))
    assert data["zip"] == {"url": "https://dl.test/LezardIA-1.1.0.zip", "sha256": hashlib.sha256(ZIP).hexdigest(),
                           "size": len(ZIP)}  # fmt: skip
    assert verify(announce(key, "1.1.0", with_zip=False))["zip"] is None  # versions publiées avant le zip
    forged = {"version": "1.1.0", "url": "https://dl.test/a.exe", "sha256": "0" * 64, "size": 1,
              "zip": {"url": "http://pirate.test/a.zip", "sha256": "0" * 64, "size": 1}}  # fmt: skip
    with pytest.raises(ReleaseError, match="zip"):
        verify(sign(key, "u1", forged))


def test_zip_update_is_downloaded_verified_extracted_then_launched(key, tmp_path):
    launched = []
    c = checker(tmp_path, server(announce(key, "1.1.0"), {"LezardIA-1.1.0.zip": ZIP}), launched)
    checked = c.check()
    assert checked["available"] and checked["size"] == len(ZIP)  # taille du zip, pas de l'installateur
    assert c.install("latest")["installing"] == "1.1.0"
    folder = tmp_path / "updates"
    new = folder / "nouvelle" / "Lezard IA"
    runner = folder / "runner"
    assert launched == [("zip", runner / "python" / "python.exe", runner / "mise_a_jour.py", tmp_path / "app", new)]
    # Python de la NOUVELLE version, sans ses dépendances : le python\ installé peut être remplacé
    assert sorted(p.name for p in (runner / "python").iterdir()) == ["python.exe", "python313.dll"]
    assert (runner / "arret-maj.py").read_bytes() == b"# arret" and (new / "gateway" / "__init__.pyc").exists()


def test_installer_installation_still_updates_with_the_installer(key, tmp_path):
    launched = []
    files = {"LezardIA-1.1.0.zip": ZIP, "LezardIA-Setup-1.1.0.exe": SETUP}
    c = checker(tmp_path, server(announce(key, "1.1.0"), files), launched, package="exe")
    assert c.check()["size"] == len(SETUP)
    c.install("latest")
    assert launched == [("exe", tmp_path / "updates" / "LezardIA-Setup-1.1.0.exe")]


def test_signed_installer_takes_over_a_zip_installation(key, tmp_path, monkeypatch):
    """Installateur signé : l'installation zip passe à l'installateur, dans son dossier et avec ses choix (sans
    « lan » : la 1re installation silencieuse cocherait tout, et le pare-feu demanderait l'accord de Windows)."""
    startup, desktop = tmp_path / "Démarrage", tmp_path / "Bureau"
    startup.mkdir()
    desktop.mkdir()
    (startup / "Lezard IA.lnk").write_bytes(b"lnk")
    monkeypatch.setattr(updates, "_shell_folder", {7: startup, 0x10: desktop}.get)
    monkeypatch.delenv("TOOLS_WEB", raising=False)
    launched = []
    files = {"LezardIA-1.1.0.zip": ZIP, "LezardIA-Setup-1.1.0.exe": SETUP}
    c = checker(tmp_path, server(announce(key, "1.1.0", signed=True), files), launched)
    assert c.check()["size"] == len(SETUP)  # c'est l'installateur qui sera téléchargé
    c.install("latest")
    app = tmp_path / "app"
    assert launched == [
        ("exe", tmp_path / "updates" / "LezardIA-Setup-1.1.0.exe", (f"/DIR={app}", "/TASKS=autostart,web"))
    ]
    # recherche web coupée et raccourci de bureau : repris tels quels
    monkeypatch.setenv("TOOLS_WEB", "false")
    (desktop / "Lezard IA.lnk").write_bytes(b"lnk")
    (startup / "Lezard IA.lnk").unlink()
    assert updates.migration_args(app, {7: startup, 0x10: desktop}.get) == (f"/DIR={app}", "/TASKS=desktopicon")
    # installateur pas encore signé : le zip, comme avant ; installation par l'installateur : sans /DIR ni /TASKS
    assert verify(announce(key, "1.1.0"))["signed"] is False
    launched.clear()
    c = checker(tmp_path, server(announce(key, "1.1.0", signed=True), files), launched, package="exe")
    c.check()
    c.install("latest")
    assert launched == [("exe", tmp_path / "updates" / "LezardIA-Setup-1.1.0.exe")]


def test_modified_zip_or_unsafe_paths_are_never_launched(key, tmp_path):
    launched = []
    c = checker(tmp_path, server(announce(key, "1.1.0"), {"LezardIA-1.1.0.zip": ZIP[:-1] + b"!"}), launched)
    c.check()
    with pytest.raises(UpdateError, match="ne correspond pas"):
        c.install("latest")
    evil = archive({"../../evil.py": b"x", "Lezard IA/python/python.exe": b"MZ"})
    c = checker(tmp_path, server(announce(key, "1.1.0", evil), {"LezardIA-1.1.0.zip": evil}), launched)
    c.check()
    with pytest.raises(UpdateError, match="chemin interdit"):
        c.install("latest")
    incomplete = archive({"Lezard IA/lezard.py": b"# lanceur"})
    c = checker(tmp_path, server(announce(key, "1.1.0", incomplete), {"LezardIA-1.1.0.zip": incomplete}), launched)
    c.check()
    with pytest.raises(UpdateError, match="incomplet"):
        c.install("latest")
    assert not launched and not (tmp_path / "evil.py").exists()


def test_version_without_zip_asks_for_a_manual_download(key, tmp_path):
    launched = []
    c = checker(tmp_path, server(announce(key, "1.1.0", with_zip=False), {}), launched)
    c.check()
    with pytest.raises(UpdateError, match="lezard-ia.fr/telecharger"):
        c.install("latest")
    assert not launched


def test_release_cli_signs_the_zip_too(key, tmp_path):
    pem = tmp_path / "u.key"
    pem.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    setup, z, out = tmp_path / "LezardIA-Setup-1.3.0.exe", tmp_path / "LezardIA-1.3.0.zip", tmp_path / "r.lzu"
    setup.write_bytes(SETUP)
    z.write_bytes(ZIP)
    args = ["sign", "--key", str(pem), "--installer", str(setup), "--url", "https://dl.test/LezardIA-Setup-1.3.0.exe",
            "--out", str(out), "--zip", str(z)]  # fmt: skip
    assert release.main(args) == 2  # adresse du zip manquante
    assert release.main([*args, "--zip-url", "https://dl.test/LezardIA-1.3.0.zip"]) == 0
    assert verify(out.read_text(encoding="ascii"))["zip"]["size"] == len(ZIP)
    assert verify(out.read_text(encoding="ascii"))["signed"] is False
    assert release.main([*args, "--zip-url", "https://dl.test/LezardIA-1.3.0.zip", "--signed"]) == 0
    assert verify(out.read_text(encoding="ascii"))["signed"] is True


def test_packaging_follows_the_uninstaller(tmp_path):
    assert updates.packaging(tmp_path) == "zip"
    (tmp_path / "unins000.exe").write_bytes(b"MZ")
    assert updates.packaging(tmp_path) == "exe"


# ---------- mise_a_jour.py : remplacement du programme ----------
@pytest.fixture
def maj(monkeypatch):
    spec = importlib.util.spec_from_file_location("mise_a_jour", HERE / "installer" / "app" / "mise_a_jour.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []
    monkeypatch.setattr(mod, "stop", lambda app, scripts: calls.append("stop") or 0)
    monkeypatch.setattr(mod, "start", lambda app: calls.append("start"))
    monkeypatch.setattr(mod, "lezard", lambda app, *args, timeout=0: calls.append(" ".join(args)) or 0)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    mod.calls = calls
    return mod


def tree(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")


def install_and_update(tmp_path):
    app = tmp_path / "app"
    tree(app, {"python/python.exe": "ancien", "python/Lib/old.py": "ancien", "gateway/a.pyc": "ancien",
               "lezard.py": "ancien", "packs/voix.json": "ancien", "packs/voix/pack.json": "installé",
               "data/gateway.db": "conversations", ".env": "GATEWAY_PORT=8123\n"})  # fmt: skip
    new = app / "data" / "updates" / "nouvelle" / "Lezard IA"
    tree(new, {"python/python.exe": "nouveau", "gateway/a.pyc": "nouveau", "gateway/b.pyc": "nouveau",
               "lezard.py": "nouveau", "packs/voix.json": "nouveau", "version.txt": "1.1.0\n"})  # fmt: skip
    return app, new


def test_update_replaces_the_program_and_keeps_the_data(maj, tmp_path, monkeypatch):
    app, new = install_and_update(tmp_path)
    monkeypatch.setattr(maj, "healthy", lambda p: p == 8123)  # port de .env
    assert maj.main(app, new, wait=1) == 0
    read = lambda name: (app / name).read_text(encoding="utf-8")  # noqa: E731
    assert (
        read("python/python.exe") == read("lezard.py") == read("gateway/b.pyc") == read("packs/voix.json") == "nouveau"
    )
    assert not (app / "python" / "Lib").exists()  # l'ancien python\ entier est remplacé
    assert read("data/gateway.db") == "conversations" and read("packs/voix/pack.json") == "installé"
    assert read(".env") == "GATEWAY_PORT=8123\n"
    assert maj.calls == ["stop", "pack refresh", "register", "start"]
    assert not (app / "data" / "updates" / "ancienne").exists() and not new.parent.exists()
    assert json.loads(read("data/updates/resultat.json"))["ok"] is True


def test_new_version_that_does_not_answer_is_rolled_back(maj, tmp_path, monkeypatch):
    app, new = install_and_update(tmp_path)
    monkeypatch.setattr(maj, "healthy", lambda p: maj.calls.count("start") == 2)  # seule l'ancienne répond
    assert maj.main(app, new, wait=0.1) == 1
    read = lambda name: (app / name).read_text(encoding="utf-8")  # noqa: E731
    assert read("python/python.exe") == read("lezard.py") == read("packs/voix.json") == "ancien"
    assert (app / "python" / "Lib" / "old.py").exists() and not (app / "gateway" / "b.pyc").exists()
    assert read("data/gateway.db") == "conversations"
    assert maj.calls[-3:] == ["start", "stop", "start"]
    result = json.loads(read("data/updates/resultat.json"))
    assert result["ok"] is False and "ancienne version remise" in result["message"]
    assert not (app / "data" / "updates" / "ancienne").exists() and not new.parent.exists()  # rien ne traîne


def test_service_that_does_not_stop_cancels_the_update(maj, tmp_path, monkeypatch):
    app, new = install_and_update(tmp_path)
    monkeypatch.setattr(maj, "stop", lambda app, scripts: 1)
    assert maj.main(app, new, wait=0.1) == 1
    assert (app / "lezard.py").read_text(encoding="utf-8") == "ancien"


@pytest.mark.skipif(sys.platform != "win32", reason="lanceur Windows")
def test_first_install_copies_the_program_only(maj, tmp_path):
    src, dst = tmp_path / "extrait", tmp_path / "installe"
    tree(src, {"python/python.exe": "nouveau", "lezard.py": "nouveau", "packs/multi.json": "m",
               "mise_a_jour.py": "m", "LISEZ-MOI.txt": "pas du programme"})  # fmt: skip
    tree(dst, {"lezard.py": "ancien", "data/x.db": "garde", "packs/multi/pack.json": "installé"})
    maj.copy_program(src, dst)
    assert (dst / "lezard.py").read_text(encoding="utf-8") == "nouveau" and (dst / "packs" / "multi.json").exists()
    assert (dst / "data" / "x.db").read_text(encoding="utf-8") == "garde" and (dst / "packs" / "multi").is_dir()
    assert not (dst / "LISEZ-MOI.txt").exists()


def test_zip_is_built_from_the_installer_stage_and_reproducible(tmp_path):
    spec = importlib.util.spec_from_file_location("zip_build", HERE / "installer" / "zip.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    stage = tmp_path / "stage"
    tree(
        stage,
        {"python/python.exe": "py", "gateway/__init__.pyc": "code", "lezard.py": "lanceur", "packs/voix.json": "v"},
    )
    first = mod.build(stage, "1.2.0", tmp_path)
    data = first.read_bytes()
    assert mod.build(stage, "1.2.0", tmp_path).read_bytes() == data  # même contenu, même zip
    with zipfile.ZipFile(first) as z:
        names = set(z.namelist())
        assert z.read("Lezard IA/version.txt") == b"1.2.0\n"
    assert {"Lezard IA/python/python.exe", "Lezard IA/lezard.py", "Lezard IA/packs/voix.json",
            "Lezard IA/Installer Lezard IA.cmd", "Lezard IA/mise_a_jour.py", "Lezard IA/arret-maj.py"} <= names  # fmt: skip
