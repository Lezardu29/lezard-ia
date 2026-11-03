"""Panneau d'aperçu du chat (static/preview-panel.js) : page HTML, image SVG ou diagramme écrits par l'IA, affichés
dans /preview, isolée (core.PREVIEW_CSP) : origine opaque, ni la clé ni l'API de la gateway."""

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.core import CSP, PREVIEW_CSP, WEB_DIR
from gateway.main import create_app
from gateway.store import DocumentStore

PANEL = (WEB_DIR / "static" / "preview-panel.js").read_text(encoding="utf-8")
MARKDOWN = (WEB_DIR / "static" / "markdown.js").read_text(encoding="utf-8")


def directives(csp):
    return {d.split()[0]: d.split()[1:] for d in (x.strip() for x in csp.split(";")) if d}


def test_preview_page_is_sandboxed_and_cut_off_from_the_gateway():
    d = directives(PREVIEW_CSP)
    assert d["sandbox"] == ["allow-scripts"]  # origine opaque : jamais allow-same-origin
    assert d["connect-src"] == ["'none'"] and d["form-action"] == ["'none'"] and d["default-src"] == ["'none'"]
    assert "'self'" not in d["script-src"] and "'self'" not in d["style-src"]
    assert d["frame-ancestors"] == ["'self'"]  # affichée seulement dans Lezard IA
    # les pages de l'application gardent leur CSP stricte
    assert "'unsafe-inline'" not in CSP and "sandbox" not in CSP


def test_preview_route_serves_the_isolated_page():
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        r = c.get("/preview")
        assert r.status_code == 200 and r.headers["content-security-policy"] == PREVIEW_CSP
        assert r.headers["x-content-type-options"] == "nosniff"
        # le contenu n'arrive que du cadre parent, une seule fois
        assert "e.source !== parent" in r.text and "shown = true" in r.text and "gw-preview-ready" in r.text


def test_the_panel_loads_content_by_message_in_a_sandboxed_frame():
    assert 'frame.setAttribute("sandbox", "allow-scripts")' in PANEL and 'frame.src = "/preview"' in PANEL
    assert "e.source !== state.frame.contentWindow" in PANEL  # seule la page d'aperçu en cours est écoutée
    assert PANEL.count('setAttribute("sandbox"') == 1  # un seul cadre, sans allow-same-origin
    assert '$("preview-title").textContent = titleOf(item)' in PANEL  # titre tiré du code : jamais interprété
    assert "escapeHtml(code)" in PANEL  # diagramme : code échappé dans la page Mermaid
    assert 'securityLevel: "strict"' in PANEL


def test_only_html_svg_and_mermaid_blocks_get_a_preview():
    assert 'if (lang === "html" || lang === "htm") return "html";' in MARKDOWN
    assert 'if (lang === "mermaid") return "mermaid";' in MARKDOWN
    assert 'data-preview="${id}" data-kind="${kind}"' in MARKDOWN  # kind : une valeur connue, jamais le texte du modèle


def test_the_panel_opens_after_an_answer_and_folds_the_list():
    app = (WEB_DIR / "static" / "app.js").read_text(encoding="utf-8")
    conv = (WEB_DIR / "static" / "conversations-ui.js").read_text(encoding="utf-8")
    html = (WEB_DIR / "app.html").read_text(encoding="utf-8")
    assert "if (meta) previewAnswer(el);" in app and "initPreview({ onOpen: foldForPreview })" in app
    assert "setOpen(false, false)" in conv  # repliée sans changer le choix gardé
    assert 'id="preview-panel"' in html and 'href="/static/preview.css"' in html
    assert html.index('href="/static/preview.css"') < html.index('href="/static/claude.css"')
