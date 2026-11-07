"""Chaque élément de la marketplace, de bout en bout : serveurs MCP déclarés puis retirés, outils et applications
installés puis désinstallés (programmes simulés, aucune commande réelle), bundles jugés sur plusieurs PC.

Les tests élément par élément de test_marketplace.py en vérifient quelques-uns ; ici, tous : un serveur, un outil
ou un bundle ajouté au catalogue est essayé sans qu'il faille penser à écrire son test."""

import json
import re

import pytest

from gateway import installers, marketplace, mcp, model_catalog
from gateway.api import marketplace as market_api

from .test_marketplace import MASTER, client, fake_popen, install_log, wait_job  # noqa: F401

SEMVER = re.compile(r"^\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$")
GB = 1e9


@pytest.fixture
def windows_pc(monkeypatch):
    """PC Windows où tout est déjà là (Node.js 24, Python, winget, Docker…) : seules les étapes de l'élément."""
    monkeypatch.setattr(installers.sys, "platform", "win32")
    monkeypatch.setattr(installers, "refresh_path", lambda: None)
    monkeypatch.setattr(installers, "program", lambda name: f"C:/bin/{name}.cmd")
    monkeypatch.setattr(installers, "_major", lambda exe: 24)


@pytest.fixture
def no_mcp_start(monkeypatch):
    """Serveurs MCP déclarés sans être lancés (npx télécharge le paquet au premier lancement)."""

    async def load(extra=None):
        pass

    async def stop():
        pass

    monkeypatch.setattr(market_api.mcp, "load", load)
    monkeypatch.setattr(market_api.mcp, "stop", stop)


# ---------- serveurs MCP ----------


def test_mcp_catalog_entries_are_well_formed():
    ids = [s["id"] for s in marketplace.MCP_SERVERS]
    assert len(ids) == len(set(ids))
    for s in marketplace.MCP_SERVERS:
        assert SEMVER.match(s["version"]), (s["id"], s["version"])  # version figée, jamais « latest »
        assert s["package"] and " " not in s["package"]
        assert isinstance(s["needs_folder"], bool)
        if s.get("secret"):
            assert re.fullmatch(r"[A-Z][A-Z0-9_]+", s["secret"]["env"]) and s["secret"]["url"].startswith("https://")
            # service en ligne : la page le dit ; une adresse à vous (SearXNG) n'en est pas un
            assert s.get("service") or s["secret"]["env"].endswith("_URL")
        assert all(c["example"].startswith("/mcp ") for c in s["commands"] if c.get("example"))


@pytest.mark.parametrize("server", marketplace.MCP_SERVERS, ids=lambda s: s["id"])
def test_every_mcp_server_is_declared_then_removed(server, client, tmp_path, windows_pc, no_mcp_start):  # noqa: F811
    sid, body = server["id"], {}
    if server["needs_folder"]:
        body["folder"] = str(tmp_path)
    if server.get("secret"):
        r = client.post(f"/v1/admin/marketplace/mcp/{sid}", headers=MASTER, json={})
        assert r.status_code == 400  # clé du service : demandée avant toute installation
        body["secret"] = "cle-de-test-0123456789"
    r = client.post(f"/v1/admin/marketplace/mcp/{sid}", headers=MASTER, json=body)
    assert r.status_code in (200, 202), r.text
    if r.status_code == 202:
        assert wait_job(client)["state"] == "done"
    saved = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    mcp.parse_config(saved)  # le fichier écrit se relit
    cfg = saved["servers"][sid]
    assert f"{server['package']}@{server['version']}" in cfg["args"]
    if server.get("folder_env"):  # dossier donné par une variable plutôt qu'en argument
        assert cfg["env"][server["folder_env"]] == str(tmp_path)
    elif server["needs_folder"]:
        assert cfg["args"][-1] == str(tmp_path)
    if server.get("secret"):
        assert cfg["env"] == {server["secret"]["env"]: "cle-de-test-0123456789"}
        assert "cle-de-test" not in r.text  # la clé ne repart jamais vers la page
    listed = {s["id"]: s for s in client.get("/v1/admin/marketplace", headers=MASTER).json()["mcp"]}
    assert listed[sid]["installed"]
    assert client.delete(f"/v1/admin/marketplace/mcp/{sid}", headers=MASTER).json()["removed"]
    assert sid not in json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))["servers"]


def test_folder_server_refuses_a_missing_or_relative_folder(client, tmp_path, windows_pc, no_mcp_start):  # noqa: F811
    for folder in ("", "Documents", str(tmp_path / "absent")):
        r = client.post("/v1/admin/marketplace/mcp/fichiers", headers=MASTER, json={"folder": folder})
        assert r.status_code == 400, folder


# ---------- outils pour agents et applications ----------

RECIPE_KEYS = sorted(k for k in installers.RECIPES if k.startswith(("tool:", "app:")))  # composants : à part


@pytest.mark.parametrize("key", RECIPE_KEYS)
def test_every_recipe_gives_runnable_commands_on_windows(key, windows_pc):
    argvs = installers.commands(key)
    assert argvs, key
    for argv in argvs:
        assert argv and all(isinstance(a, str) and a for a in argv), (key, argv)
        assert not any(c in " ".join(argv[1:]) for c in "|&;`$<>") or key.startswith("app:"), (key, argv)
    assert installers.missing(key) == []  # tout est là : rien à installer d'abord
    for step in installers.plan(key):
        assert installers.label(step).strip()
    for step in installers.uninstall_steps(key) or ():
        assert step and all(isinstance(a, str) and a for a in step)


def _catalog_key(kind: str, entry: dict) -> str:
    return f"{'tool' if kind == 'tools' else 'app'}:{entry['id']}"


ONE_CLICK = [("tools", t) for t in marketplace.AGENT_TOOLS] + [("apps", a) for a in marketplace.APPS]


@pytest.mark.parametrize("kind,entry", ONE_CLICK, ids=lambda x: x if isinstance(x, str) else x["id"])
def test_every_tool_and_app_installs_then_uninstalls(kind, entry, client, monkeypatch, windows_pc, install_log):  # noqa: F811
    key = _catalog_key(kind, entry)
    if key not in installers.RECIPES:
        pytest.fail(f"{key} : dans le catalogue sans recette d'installation")
    started = []
    fake_popen(monkeypatch, started)
    monkeypatch.setattr(installers, "program", lambda name: f"C:/bin/{name}.cmd")  # après fake_popen / install_log
    r = client.post(f"/v1/admin/marketplace/{kind}/{entry['id']}/install", headers=MASTER)
    assert r.status_code == 202, r.text
    job = wait_job(client)
    assert job["state"] == "done", job["log"]
    assert len(started) == len(installers.commands(key))  # chaque étape lancée, dans l'ordre
    cat = client.get("/v1/admin/marketplace", headers=MASTER).json()
    listed = next(e for e in cat["agent_tools" if kind == "tools" else "apps"] if e["id"] == entry["id"])
    assert listed["installed"]
    if installers.uninstall_steps(key):
        started.clear()
        assert client.post(f"/v1/admin/marketplace/{kind}/{entry['id']}/uninstall", headers=MASTER).status_code == 202
        assert wait_job(client)["state"] == "done"
        assert len(started) == len(installers.uninstall_steps(key))
    else:  # désinstallation hors de la page : elle dit comment faire
        r = client.post(f"/v1/admin/marketplace/{kind}/{entry['id']}/uninstall", headers=MASTER)
        assert r.status_code == 409 and r.json()["error"]["message"]


# ---------- bundles, sur plusieurs PC ----------

PCS = {
    "petit-portable": {"ram": {"total": 8 * GB, "available": 4 * GB}, "gpus": [], "disk": {"free": 50 * GB}},
    "bureau-sans-carte": {"ram": {"total": 16 * GB, "available": 10 * GB}, "gpus": [], "disk": {"free": 200 * GB}},
    "joueur": {
        "ram": {"total": 32 * GB, "available": 24 * GB},
        "gpus": [{"name": "RTX 4070", "vram_total": 12 * GB, "vram_used": 1 * GB}],
        "disk": {"free": 500 * GB},
    },
    "station": {
        "ram": {"total": 64 * GB, "available": 50 * GB},
        "gpus": [{"name": "RTX 4090", "vram_total": 24 * GB, "vram_used": 1 * GB}],
        "disk": {"free": 1000 * GB},
    },
}


@pytest.mark.parametrize("pc", PCS, ids=str)
@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_bundles_are_judged_coherently_on_every_pc(pc, platform):
    hw = PCS[pc]
    reco = model_catalog.recommend(hw)
    with_claude = lambda name: "C:/bin/claude.cmd" if name == "claude" else None  # noqa: E731
    out = marketplace.bundles_for(reco, hw, with_claude, platform)
    assert [b["id"] for b in out] == [b["id"] for b in marketplace.BUNDLES]
    for b in out:
        assert b["suggested"] == (not b["why_not"]), b  # proposé si et seulement si rien ne l'empêche
        for part in b["parts"]:
            if part["kind"] == "model":
                assert part["tag"] and part["label"]  # modèle choisi pour ce PC
        if b["suggested"] and any(
            k == "windows" for k in next(x for x in marketplace.BUNDLES if x["id"] == b["id"])["when"]
        ):
            assert platform == "win32"
    by_id = {b["id"]: b for b in out}
    assert by_id["essentiel"]["suggested"] or not reco.get("best", {}).get(
        "chat"
    )  # le socle, partout où un modèle tient
    if platform != "win32":
        assert not by_id["assistant-pc"]["suggested"] and not by_id["images"]["suggested"]
    if pc == "petit-portable":
        assert not by_id["equipe"]["suggested"]  # 16 Go et une carte graphique


def test_claude_bundles_need_claude_code():
    hw = PCS["station"]
    out = marketplace.bundles_for(model_catalog.recommend(hw), hw, lambda name: None, "win32")
    for b in out:
        if b["id"].startswith("claude"):
            assert not b["suggested"] and any("Claude Code" in w for w in b["why_not"])
