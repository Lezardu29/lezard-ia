"""Compatibilité des outils de la marketplace (gateway/compat.py) et outils activés au début d'une conversation."""

from pathlib import Path

import pytest

from gateway import compat, marketplace, marketplace_extra

ROOT = Path(__file__).resolve().parent.parent


def catalog_keys() -> set[str]:
    """Tout le catalogue, supplément signé compris (outils Roblox…)."""
    source = ROOT / "catalogue" / "supplement.json"
    if source.is_file():
        marketplace_extra.apply_source(source)
    keys = {f"component:{c['id']}" for c in marketplace.COMPONENTS} | {
        f"mcp:{s['id']}" for s in marketplace.MCP_SERVERS
    }
    keys |= {f"tool:{t['id']}" for t in marketplace.AGENT_TOOLS} | {f"app:{a['id']}" for a in marketplace.APPS}
    return keys


def test_every_rule_names_real_catalog_entries_and_says_why():
    known = catalog_keys()
    for r in compat.RULES:
        # outils Roblox : fiches du supplément signé, absent du dépôt public
        roblox = {"tool:rokit", "tool:rojo", "tool:wally", "tool:stylua", "tool:selene"}
        supplement = (ROOT / "catalogue" / "supplement.json").is_file()
        missing = [i for i in r["ids"] if i not in known and (supplement or i not in roblox)]
        assert not missing, (r["title"], missing)  # une fiche renommée ou retirée : la règle doit suivre
        assert r["level"] in ("conflit", "attention", "doublon") and len(r["text"]) > 60


def test_notes_for_an_item_list_the_others():
    known = catalog_keys()
    notes = compat.notes_for("tool:superpowers", known)
    assert notes and notes[0]["level"] == "conflit" and "tool:bmad" in notes[0]["with"]
    assert compat.notes_for("mcp:context7", known) == []


def test_report_warns_only_about_what_is_installed_here():
    known = catalog_keys()
    calm = compat.report({"tool:superpowers", "mcp:playwright", "app:openwebui"}, known, {"playwright": 25})
    assert calm["warnings"] == [] and calm["ok"] and calm["mcp_tools"] == 25
    busy = compat.report(
        {"tool:superpowers", "tool:bmad", "mcp:playwright", "mcp:chromedevtools", "mcp:git", "mcp:fichiers",
         "mcp:notion", "mcp:firecrawl"},
        known,
        {"playwright": 25, "chromedevtools": 32, "git": 28, "fichiers": 14, "notion": 24, "firecrawl": 28},
    )  # fmt: skip
    levels = [w["level"] for w in busy["warnings"]]
    assert levels[0] == "conflit" and not busy["ok"]  # le plus grave d'abord
    titles = " | ".join(w["title"] for w in busy["warnings"])
    assert "Méthodes de travail" in titles and "Deux pilotes de navigateur" in titles
    assert "6 serveurs MCP" in titles and "151 outils MCP en tout" in titles


@pytest.mark.parametrize("kind", ["mcp", "app", "tool", "component"])
def test_each_kind_has_a_general_note(kind):
    assert compat.KIND_NOTES[kind] and "casse pas" in compat.GENERAL


def test_marketplace_page_shows_compatibility():
    js = (ROOT / "gateway" / "web" / "static" / "marketplace.js").read_text(encoding="utf-8")
    assert "function compatBlock(it)" in js and "function renderCompat()" in js and "innerHTML" not in js
    html = (ROOT / "gateway" / "web" / "marketplace.html").read_text(encoding="utf-8")
    assert 'id="compat"' in html and 'id="compat-list"' in html


def test_chat_asks_which_tools_to_activate():
    html = (ROOT / "gateway" / "web" / "app.html").read_text(encoding="utf-8")
    assert "Quels outils installés activer pour cette conversation ?" in html and 'id="ct-pop"' in html
    assert html.index('id="conv-tools"') < html.index('id="suggestions"')  # juste sous la saisie, avant les idées
    chat = (ROOT / "gateway" / "web" / "static" / "chat.js").read_text(encoding="utf-8")
    assert "convTools.options()" in chat and "convTools.reset();" in chat and "convTools.keep(r.data.id)" in chat
    tools = (ROOT / "gateway" / "web" / "static" / "conv-tools.js").read_text(encoding="utf-8")
    assert "innerHTML" not in tools and "gateway_tools" in tools
