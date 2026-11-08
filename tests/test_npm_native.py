"""Outils de la marketplace qui ne s'installaient pas : scripts d'installation introuvables dans l'installation Windows
(code livré compilé), et outils npm dont better-sqlite3 12 n'a pas de version toute prête pour Node.js 24."""

import json
import sys
from pathlib import Path

from gateway import installers, npm_native


def test_install_scripts_are_found_in_the_compiled_windows_install(tmp_path, monkeypatch):
    """Installation Windows : seul le .pyc existe (sources retirées) ; Python le lance tel quel."""
    fake = tmp_path / "installers.pyc"
    (tmp_path / "setup_download.pyc").write_bytes(b"")
    monkeypatch.setattr(installers, "__file__", str(fake))
    assert installers._script("setup_download") == str(tmp_path / "setup_download.pyc")
    (tmp_path / "setup_download.py").write_text("", encoding="utf-8")
    assert installers._script("setup_download") == str(tmp_path / "setup_download.py")  # depuis le code : la source
    for name in ("setup_download", "librechat_setup", "npm_native"):  # depuis le code, chacun existe
        assert Path(
            getattr(
                installers, {"setup_download": "SETUP", "librechat_setup": "LIBRECHAT_SETUP"}.get(name, "NPM_NATIVE")
            )
        ).name.startswith(name)


def test_tools_with_better_sqlite3_are_installed_without_compiling():
    for key, package, command in (
        ("tool:ccrouter", "@musistudio/claude-code-router", "ccr"),
        ("tool:cloudcli", "@cloudcli-ai/cloudcli", "cloudcli"),
    ):
        steps = installers.recipe(key)["steps"]
        assert steps[0][:4] == ("npm", "install", "--global", "--ignore-scripts")  # sans compiler (Visual Studio)
        assert steps[1] == (installers.PYTHON, installers.NPM_NATIVE, package, command)
        assert installers.uninstall_steps(key) == [("npm", "uninstall", "--global", package)]  # la réparation part avec


def module(root: Path, name: str, scripts: dict | None = None, version="1.0.0") -> Path:
    d = root / "node_modules" / name
    d.mkdir(parents=True)
    (d / "package.json").write_text(json.dumps({"name": name, "version": version, "scripts": scripts or {}}), "utf-8")
    return d


def test_native_modules_are_repaired(tmp_path, monkeypatch):
    """better-sqlite3 remplacé par sa version toute prête, les autres modules natifs reconstruits, puis le script
    d'installation de l'outil lui-même."""
    root = tmp_path / "npm" / "node_modules"
    tool = root / "@x" / "outil"
    tool.mkdir(parents=True)
    (tool / "package.json").write_text(
        json.dumps({"name": "@x/outil", "scripts": {"postinstall": "node fix.js"}}), "utf-8"
    )
    old = module(tool, "better-sqlite3", {"install": "prebuild-install || node-gyp rebuild"}, "12.11.1")
    module(tool, "bcrypt", {"install": "node-gyp-build"})
    module(tool, "pur-js")
    (tool / "node_modules" / "pur-js" / "test").mkdir()
    (tool / "node_modules" / "pur-js" / "test" / "package.json").write_text(
        '{"name": "faux", "scripts": {"install": "x"}}'
    )
    (tmp_path / "npm" / "outil.cmd").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        npm_native.shutil, "which", lambda name: str(tmp_path / "npm" / name) if name == "outil.cmd" else "npm"
    )
    ran = []

    def fake_run(argv, cwd=None):
        ran.append((argv[1:], cwd))
        if argv[1] == "install":  # better-sqlite3 13 installé à part, avec sa version toute prête
            fresh = Path(argv[3]) / "node_modules" / "better-sqlite3"
            fresh.mkdir(parents=True)
            (fresh / "package.json").write_text(json.dumps({"name": "better-sqlite3", "version": "13.0.3"}), "utf-8")
            (fresh / "build").mkdir()

    monkeypatch.setattr(npm_native, "run", fake_run)
    npm_native.repair("@x/outil", "outil")
    assert json.loads((old / "package.json").read_text("utf-8"))["version"] == npm_native.BETTER_SQLITE3
    assert ran[0][0][-1] == f"better-sqlite3@{npm_native.BETTER_SQLITE3}"
    assert ran[1] == (["rebuild", "--foreground-scripts", "bcrypt"], tool)  # pas « faux » (dossier de test)
    assert ran[2] == (["run", "postinstall"], tool)
    assert npm_native.BETTER_SQLITE3 == "13.0.3" and sys.platform  # 12.11.1 : pas de version pour Node.js 24
