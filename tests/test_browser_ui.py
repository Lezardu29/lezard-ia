"""Le chat dans un vrai navigateur (Chrome piloté par Playwright), à 320 et 375 px, dans les deux interfaces : pas de
défilement de côté, menu ☰ / menu du compte, aide Ctrl+/, texte d'aide de la saisie sur une ligne, bouton
« Télécharger » des blocs de code. Une vraie gateway tourne sur un port libre (base dans un dossier temporaire, fausse
IA). Ignoré si Playwright ou Chrome manquent."""

import json
import os
import socket
import threading
import time

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from gateway.config import ProviderConfig  # noqa: E402
from gateway.main import create_app  # noqa: E402
from gateway.plans import PlanResolver  # noqa: E402
from gateway.store import DocumentStore  # noqa: E402

ANSWER = "Voici le code :\n\n```python\nprint('bonjour')\n```\n"
WIDTHS = (320, 375)


def fake_llm(request: httpx.Request):
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
    body = json.loads(request.content)
    if not body.get("stream"):  # titre de la conversation
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": body.get("model"),
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "Titre"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            },
        )
    chunk = {"id": "x", "model": body.get("model"), "choices": [{"index": 0, "delta": {"content": ANSWER}}]}
    end = {
        "id": "x",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }
    sse = f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(end)}\n\ndata: [DONE]\n\n"
    return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})


@pytest.fixture(scope="module")
def gateway_url(tmp_path_factory):
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(fake_llm)),
        plans=PlanResolver({}, "free", master_key="master"),
        store=DocumentStore(str(tmp_path_factory.mktemp("donnees") / "gateway.db")),
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # ws="none" : le chat n'utilise pas de WebSocket, et websockets.legacy (chargé par uvicorn sinon) lève un
    # DeprecationWarning, une erreur en CI (-W error::DeprecationWarning) qui arrêtait la gateway de test
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="none"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            pytest.fail("la gateway de test n'a pas démarré")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome")
        except sync_api.Error as e:
            pytest.skip(f"Chrome introuvable pour Playwright : {e.message.splitlines()[0]}")
        yield b
        b.close()


def open_chat(browser, url, width, ui):
    """Chat connecté avec la clé maître : la page passe par le cadre principal (frame.js y renvoie sinon)."""
    ctx = browser.new_context(viewport={"width": width, "height": 740}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + "/")
    page.evaluate("sessionStorage.setItem('gw.apiKey', 'master')")
    page.goto(url + "/app")
    page.wait_for_function("[...document.querySelectorAll('iframe')].some(f => f.contentWindow?.GatewayTheme)")
    frame = next(f for f in page.frames if "frame=1" in f.url)
    frame.wait_for_selector("#prompt")
    frame.evaluate(f"GatewayTheme.setUi('{ui}')")
    frame.wait_for_timeout(300)
    return ctx, page, frame, errors


FITS = """sel => [...document.querySelectorAll(sel)].filter(e => e.offsetParent !== null).every(e => {
  const r = e.getBoundingClientRect();
  return r.left >= -1 && r.right <= innerWidth + 1;
})"""
NO_SIDE_SCROLL = "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
SETTLED = "() => document.getAnimations().every(a => a.playState !== 'running')"  # tiroir arrivé, menu posé
# texte d'aide mesuré avec la police du champ : il doit tenir dans la largeur utile, sur une ligne
HINT_FITS = """() => {
  const t = document.getElementById('prompt'), cs = getComputedStyle(t);
  const ctx = document.createElement('canvas').getContext('2d');
  ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  const room = t.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  return {hint: t.placeholder, width: Math.round(ctx.measureText(t.placeholder).width), room: Math.round(room)};
}"""


@pytest.mark.parametrize("ui", ["classic", "claude"])
@pytest.mark.parametrize("width", WIDTHS)
def test_chat_fits_small_phones(browser, gateway_url, width, ui):
    ctx, page, frame, errors = open_chat(browser, gateway_url, width, ui)
    shots = os.environ.get("UI_SCREENSHOTS")  # dossier où enregistrer des captures à relire (facultatif)

    def shot(step):
        if shots:
            page.screenshot(path=os.path.join(shots, f"{width}-{ui}-{step}.png"))

    try:
        assert frame.evaluate(NO_SIDE_SCROLL), "défilement de côté à l'accueil"
        hint = frame.evaluate(HINT_FITS)
        assert hint["width"] <= hint["room"] + 1, f"texte d'aide coupé : {hint}"
        assert frame.evaluate(FITS, ".suggestion, .composer, .topbar, .topbar > *")

        # menu : ☰ en interface classique, menu du compte (barre latérale) en interface Claude
        if ui == "classic":
            frame.locator(".menu-btn").click()
            frame.wait_for_selector("#drawer:not([hidden])")
            frame.wait_for_function(SETTLED)
            shot("menu")
            assert frame.locator("#drawer #theme-btn").is_visible()
            assert frame.evaluate(FITS, "#drawer, #drawer .icon-btn, #drawer .btn")
            frame.locator("#drawer .drawer-head button").click()
        else:
            frame.locator("#conv-btn").click()
            frame.locator("#acct-btn").click()
            frame.wait_for_selector("#acct-menu:not([hidden])")
            frame.wait_for_function(SETTLED)
            shot("menu")
            assert frame.locator("#acct-menu #theme-btn").is_visible()
            assert frame.evaluate(FITS, "#acct-menu, #acct-menu .icon-btn")
            page.keyboard.press("Escape")  # le menu du compte
            page.keyboard.press("Escape")  # puis la barre latérale, posée par-dessus le bouton qui l'a ouverte
            frame.wait_for_selector("#conv-panel.open", state="detached")
        frame.wait_for_timeout(300)

        # aide des raccourcis
        page.keyboard.press("Control+/")
        frame.wait_for_selector(".shortcuts-dialog[open]")
        frame.wait_for_function(SETTLED)
        shot("raccourcis")
        assert frame.evaluate(FITS, ".shortcuts-dialog, .shortcuts kbd")
        assert frame.evaluate(
            "(() => { const d = document.querySelector('.shortcuts-dialog');"
            " return d.scrollWidth <= d.clientWidth; })()"
        ), "l'aide déborde de côté"
        page.keyboard.press("Escape")

        # réponse avec un bloc de code : « Télécharger » et « Copier » visibles, dans l'écran
        frame.locator("#prompt").fill("Un exemple de code")
        frame.locator("#prompt").press("Enter")
        frame.wait_for_selector(".msg.assistant:not(.streaming) .code-dl")
        frame.wait_for_function(SETTLED)
        shot("code")
        assert frame.locator(".code-dl").last.is_visible() and frame.locator(".code .copy").last.is_visible()
        assert frame.evaluate(FITS, ".code-head, .code-dl, .code .copy, .topbar > *")  # bouton d'export en plus
        assert frame.evaluate("[...document.querySelectorAll('.topbar .pill')].every(p => p.offsetHeight < 30)"), (
            "la pastille de l'offre passe sur deux lignes"
        )
        assert frame.evaluate("[...document.querySelectorAll('.code-head')].every(h => h.scrollWidth <= h.clientWidth)")
        assert frame.evaluate(NO_SIDE_SCROLL), "défilement de côté avec une réponse"
        assert not errors, errors
    finally:
        ctx.close()
