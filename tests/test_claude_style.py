"""Direction artistique « Claude » (static/claude.css) : style seulement. Aucune couleur n'est changée, la feuille est
chargée en dernier sur les pages, et les éléments de la page restent les mêmes."""

import re

from gateway.core import WEB_DIR

CSS = (WEB_DIR / "static" / "claude.css").read_text(encoding="utf-8")
COLOR_PROPS = re.compile(
    r"(?<![\w-])(color|background|background-color|background-image|border-color|outline-color|fill|stroke|"
    r"filter|opacity)\s*:"
)
COLOR_TOKENS = re.compile(
    r"--(bg|surface|surface-2|border|text|text-muted|accent|accent-contrast|accent-soft|danger|danger-soft|success|"
    r"bubble-user|code-bg)\s*:"
)


def test_the_stylesheet_never_changes_a_colour():
    body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)  # les commentaires ne comptent pas
    assert not COLOR_PROPS.search(body), "une propriété de couleur est changée"
    assert not COLOR_TOKENS.search(body), "une couleur du thème est changée"


def test_the_proposed_claude_accent_is_only_declared_not_applied():
    body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert "--accent-claude" in body and "var(--accent-claude)" not in body


def test_every_page_loads_it_last_and_keeps_its_structure():
    from fastapi.testclient import TestClient

    from gateway.main import create_app
    import httpx

    from gateway.config import ProviderConfig
    from gateway.store import DocumentStore

    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        for path in ("/app?frame=1", "/admin?frame=1", "/models?frame=1", "/"):
            html = c.get(path).text
            assert 'href="/static/claude.css"' in html, path
            assert html.index('href="/static/claude.css"') > html.index('href="/static/mobile.css"'), path  # en dernier
        assert c.get("/static/claude.css").status_code == 200
