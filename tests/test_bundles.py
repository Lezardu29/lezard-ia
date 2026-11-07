"""Marketplace : bundles (éléments installés ensemble, proposés selon ce PC et ce qu'on y utilise) et commandes
ajoutées par chaque élément (fenêtre « Détails »)."""

import json
import re
from pathlib import Path

import pytest

from gateway import marketplace, marketplace_export, model_catalog
from gateway.api import marketplace as market_api

from .test_marketplace import ACCOUNT, MASTER, client  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "gateway" / "web" / "static"
GB = 1e9


def reco(fit: str = "vram") -> dict:
    """Meilleur modèle par usage, comme model_catalog.recommend : tous tiennent (fit) sur ce PC."""
    best = {use: f"modele-{use}:7b" for use in model_catalog.USES}
    return {"best": best, "models": [{"model": t, "label": t, "fit": fit} for t in best.values()]}


def judged(hw: dict, programs=(), platform: str = "win32", r: dict | None = None) -> dict:
    out = marketplace.bundles_for(r or reco(), hw, lambda p: f"C:/bin/{p}.exe" if p in programs else None, platform)
    return {b["id"]: b for b in out}


BIG = {"ram": {"total": 32 * GB}, "gpus": [{"vram_total": 12 * GB}]}
SMALL = {"ram": {"total": 4 * GB}, "gpus": []}


def test_every_bundle_points_at_the_catalog():
    known = {
        "component": {c["id"] for c in marketplace.COMPONENTS},
        "mcp": {s["id"] for s in marketplace.MCP_SERVERS},
        "tool": {t["id"] for t in marketplace.AGENT_TOOLS},
        "app": {a["id"] for a in marketplace.APPS},
        "model": set(model_catalog.USES),
    }
    assert len({b["id"] for b in marketplace.BUNDLES}) == len(marketplace.BUNDLES) >= 6
    for b in marketplace.BUNDLES:
        assert b["name"] and b["description"] and b["for"] and len(b["items"]) >= 2
        for key in b["items"]:
            kind, _, item = key.partition(":")
            assert item in known[kind], key
        models = [i for i in b["items"] if i.startswith("model:")]
        if models:  # le moteur d'abord : les modèles s'installent dans Ollama
            assert b["items"].index("component:ollama") < b["items"].index(models[0]), b["id"]
    for kind in ("component", "mcp", "tool", "app"):  # chaque sorte d'élément figure dans au moins un bundle
        assert any(i.startswith(kind + ":") for b in marketplace.BUNDLES for i in b["items"]), kind


def test_claude_tools_only_for_those_who_use_claude_code():
    without = judged(BIG)
    assert not without["claude-code"]["suggested"] and not without["claude-video"]["suggested"]
    assert "Claude Code n'est pas installé" in without["claude-code"]["why_not"][0]
    with_it = judged(BIG, programs=("claude",))
    assert with_it["claude-code"]["suggested"] and with_it["claude-video"]["suggested"]
    assert with_it["claude-code"]["why"] == ["Claude Code est installé sur ce PC."]


def test_bundles_follow_the_performance_of_this_pc():
    big, small = judged(BIG), judged(SMALL)
    assert all(big[b]["suggested"] for b in ("essentiel", "voix", "equipe", "code-local", "navigateur", "images"))
    assert small["essentiel"]["suggested"]  # le socle : pour tout le monde
    assert not small["voix"]["suggested"] and "8 Go de mémoire vive (ce PC : 4 Go)" in small["voix"]["why_not"][0]
    assert not small["images"]["suggested"] and "aucune sur ce PC" in small["images"]["why_not"][0]
    assert not small["equipe"]["suggested"]
    # modèle qui ne tient pas dans la carte graphique : un bundle « rapide » n'est pas proposé
    slow = judged(BIG, r=reco(fit="offload"))
    assert not slow["code-local"]["suggested"] and "il serait lent" in slow["code-local"]["why_not"][0]
    assert slow["essentiel"]["suggested"]  # le modèle de discussion peut déborder en RAM
    # aucun modèle de cet usage ne tient : pas proposé, et dit pourquoi
    none = reco()
    none["best"]["code"] = None
    assert "Aucun modèle « Code » ne tient sur ce PC." in judged(BIG, r=none)["code-local"]["why_not"]
    # le contrôle du PC : Windows seulement
    assert not judged(BIG, platform="linux")["assistant-pc"]["suggested"]
    assert judged(BIG)["assistant-pc"]["suggested"]


def test_models_are_picked_for_this_pc():
    parts = judged(BIG)["code-local"]["parts"]
    code = next(p for p in parts if p["kind"] == "model" and p["use"] == "code")
    assert code["tag"] == "modele-code:7b" and code["id"] == "modele-code" and code["use_label"] == "Code"
    public = {b["id"]: b for b in marketplace.public_bundles()}  # visiteurs : l'usage, pas encore le modèle
    assert {"kind": "model", "use": "code", "use_label": "Code"} in public["code-local"]["parts"]
    assert all("when" not in b and "items" not in b for b in public.values())


# commandes : chaque élément dit comment s'en servir dans « Détails » (commande, raccourci ou où l'ouvrir)
ADDS_COMMANDS = {
    "component": {c["id"] for c in marketplace.COMPONENTS},
    "mcp": {s["id"] for s in marketplace.MCP_SERVERS},
    "app": {a["id"] for a in marketplace.APPS},
    "tool": {t["id"] for t in marketplace.AGENT_TOOLS},
}


def test_every_element_that_adds_commands_explains_them():
    entries = {
        "component": marketplace.COMPONENTS,
        "mcp": marketplace.MCP_SERVERS,
        "app": marketplace.APPS,
        "tool": marketplace.AGENT_TOOLS,
    }
    for kind, ids in ADDS_COMMANDS.items():
        for e in entries[kind]:
            if e["id"] not in ids:
                continue
            assert e.get("commands"), f"{kind}:{e['id']}"
            for c in e["commands"]:
                assert c["use"] and c["what"].endswith((".", "»", ")")) and c["where"] in marketplace.WHERE.values()


def test_chat_commands_exist_in_the_chat():
    """Une commande du chat montrée dans la marketplace est une vraie commande « / » du chat."""
    js = (STATIC / "slash-commands.js").read_text(encoding="utf-8")
    real = set(re.findall(r'\{ name: "(\w+)"', js))
    shown = {
        c["use"].split()[0].lstrip("/")
        for e in (*marketplace.COMPONENTS, *marketplace.MCP_SERVERS, *marketplace.APPS)
        for c in e.get("commands", [])
        if c["where"] == marketplace.WHERE["chat"] and c["use"].startswith("/")  # pas les boutons (micro…)
    }
    assert shown and shown <= real, shown - real
    assert "competence" in real  # compétences : /competence <id>, ajoutée par la page


def test_catalogs_carry_bundles_and_commands(client, monkeypatch):  # noqa: F811
    public = client.get("/v1/marketplace").json()
    assert [b["id"] for b in public["bundles"]] == [b["id"] for b in marketplace.BUNDLES]
    assert "suggested" not in public["bundles"][0]  # rien de ce PC pour les visiteurs
    assert next(c for c in public["components"] if c["id"] == "web")["commands"][0]["use"] == "/web <question>"
    monkeypatch.setattr(market_api.installers, "program", lambda name: None)  # pas de Claude Code ici
    assert client.get("/v1/admin/marketplace", headers=ACCOUNT).status_code == 403
    judged_here = {b["id"]: b for b in client.get("/v1/admin/marketplace", headers=MASTER).json()["bundles"]}
    assert set(judged_here) == {b["id"] for b in marketplace.BUNDLES}
    assert judged_here["claude-code"]["suggested"] is False and judged_here["claude-code"]["why_not"]
    assert all(isinstance(b["suggested"], bool) for b in judged_here.values())


def test_site_export_has_bundles():
    cat = marketplace_export.static_catalog()
    assert cat["bundles"] == marketplace.public_bundles()
    assert next(t for t in cat["agent_tools"] if t["id"] == "watch")["commands"][0]["where"] == "Claude Code"
    json.dumps(cat)  # écrit tel quel dans site/marketplace/catalog.json


SITE_JS = ROOT / "site" / "assets" / "marketplace.js"


@pytest.mark.parametrize(
    "path",
    [
        STATIC / "marketplace.js",
        pytest.param(SITE_JS, marks=pytest.mark.skipif(not SITE_JS.is_file(), reason="site/ : dépôt privé seulement")),
    ],
)
def test_pages_show_bundles_and_commands(path):
    js = path.read_text(encoding="utf-8")
    assert '"bundle", "Bundles"' in js and "Commandes et utilisation" in js and "Commandes que vous aurez ensuite" in js
    assert "/competence " in js  # compétences : la commande qui les active


def test_local_page_installs_a_bundle_one_element_after_another():
    js = (STATIC / "marketplace.js").read_text(encoding="utf-8")
    assert "async function installBundle" in js and "for (const [i, p] of todo.entries())" in js
    assert "!it.hidden" in js  # bundle pas adapté à ce PC : pas proposé
    for route in ("/components/", "/mcp/", "/tools/", "/apps/"):  # les routes habituelles, une à la fois
        assert f"/v1/admin/marketplace{route}" in js
