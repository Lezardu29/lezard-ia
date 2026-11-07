"""Le site lezard-ia.fr dans un vrai navigateur (Chromium piloté par Playwright), servi comme par Cloudflare Pages :
adresses sans « .html », page 404, en-têtes de site/_headers (CSP comprise). Chaque page atteinte par un lien, sur
ordinateur et sur téléphone : aucune erreur JavaScript ni violation de la CSP, aucune ressource introuvable, aucun
défilement de côté, ancres et pages du sitemap qui existent. Ignoré si Playwright, Chromium ou site/ manquent."""

import os
import re
import socket
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pytest

# BROWSER_TESTS=required (job « navigateur » de la CI) : Playwright absent = échec, jamais un test ignoré en silence
if os.getenv("BROWSER_TESTS") == "required":
    import playwright.sync_api as sync_api
else:
    sync_api = pytest.importorskip("playwright.sync_api")

SITE = Path(__file__).resolve().parent.parent / "site"
pytestmark = pytest.mark.skipif(not SITE.is_dir(), reason="site/ : seulement dans le dépôt privé")
VIEWPORTS = {"ordinateur": {"width": 1280, "height": 800}, "téléphone": {"width": 390, "height": 844}}


def headers_for_all() -> dict[str, str]:
    """En-têtes du bloc « /* » de site/_headers."""
    out, block = {}, False
    for line in (SITE / "_headers").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            block = line.strip() == "/*"
        elif block and ":" in line:
            k, v = line.strip().split(":", 1)
            out[k.strip()] = v.strip()
    return out


class Pages(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        for k, v in headers_for_all().items():
            if k != "Strict-Transport-Security":
                self.send_header(k, v)
        super().end_headers()

    def send_head(self):
        path = urlsplit(self.path).path
        target = SITE / path.lstrip("/")
        if path != "/" and not target.exists():
            pretty = SITE / (path.lstrip("/") + ".html")
            self.path = path + ".html" if pretty.exists() else "/404.html"
        return super().send_head()


@pytest.fixture(scope="module")
def site_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Pages, directory=str(SITE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except sync_api.Error as e:
            pytest.skip(f"Chromium introuvable pour Playwright : {e.message.splitlines()[0]}")
        yield b
        b.close()


CHECKS = """() => {
  const out = [], w = document.documentElement.clientWidth;
  if (document.documentElement.scrollWidth > w + 1) out.push(`défilement de côté : ${document.documentElement.scrollWidth} px pour ${w}`);
  for (const a of document.querySelectorAll("a[href^='#']")) {
    const id = decodeURIComponent(a.getAttribute("href").slice(1));
    if (id && !document.getElementById(id)) out.push(`ancre absente : #${id}`);
  }
  for (const img of document.querySelectorAll("img")) if (!img.hasAttribute("alt")) out.push(`image sans alt : ${img.src}`);
  if (document.querySelectorAll("h1").length !== 1) out.push(`${document.querySelectorAll("h1").length} titres h1`);
  if (!document.querySelector('meta[name="description"]')) out.push("sans meta description");
  return out;
}"""


def crawl(browser, base: str, viewport: dict) -> dict[str, list[str]]:
    ctx = browser.new_context(viewport=viewport, locale="fr-FR")
    todo, seen, report = ["/"], set(), {}
    while todo:
        path = todo.pop(0)
        if path in seen:
            continue
        seen.add(path)
        page, issues = ctx.new_page(), []
        page.on("console", lambda m, i=issues: m.type == "error" and i.append(f"console : {m.text[:200]}"))
        page.on("pageerror", lambda e, i=issues: i.append(f"exception : {e}"))
        page.on(
            "response",
            lambda r, i=issues: r.status >= 400 and r.url.startswith(base) and i.append(f"HTTP {r.status} {r.url}"),
        )
        res = page.goto(base + path, wait_until="networkidle")
        if res.status >= 400:
            issues.append(f"page {res.status}")
        issues += page.evaluate(CHECKS)
        for href in page.eval_on_selector_all("a[href]", "els => els.map(a => a.getAttribute('href'))"):
            if not href or re.match(r"^(https?:|mailto:|#|javascript:)", href):
                continue
            target = urlsplit(urljoin(base + path, href))
            page_link = target.path.endswith((".html", "/")) or "." not in target.path.rsplit("/", 1)[-1]
            if f"{target.scheme}://{target.netloc}" == base and page_link:  # pages ; pas les téléchargements
                todo.append(target.path)
        report[path] = issues
        page.close()
    ctx.close()
    return report


@pytest.mark.parametrize("device", VIEWPORTS)
def test_every_page_of_the_site_works(browser, site_url, device):
    report = crawl(browser, site_url, VIEWPORTS[device])
    assert len(report) >= 7, sorted(
        report
    )  # accueil, aide, marketplace, web, téléchargement, mentions, confidentialité
    problems = {p: i for p, i in report.items() if i}
    assert not problems, problems


def test_sitemap_lists_existing_pages(browser, site_url):
    ctx = browser.new_context()
    sitemap = (SITE / "sitemap.xml").read_text(encoding="utf-8")
    for loc in re.findall(r"<loc>([^<]+)</loc>", sitemap):
        r = ctx.request.get(site_url + urlsplit(loc).path)
        assert r.status == 200 and "Page introuvable" not in r.text(), loc
    ctx.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_site_text_is_readable_in_both_themes(browser, site_url, scheme):
    from .contrast_check import CONTRAST

    ctx = browser.new_context(color_scheme=scheme, viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    bad = {}
    for path in ("/", "/aide.html", "/marketplace/", "/web.html", "/telecharger.html"):
        page.goto(site_url + path, wait_until="networkidle")
        if found := page.evaluate(CONTRAST):
            bad[path] = found
    ctx.close()
    assert not bad, bad  # avant : vert sur vert pâle à 4,25:1 (accroche de l'accueil, licences de la marketplace)
