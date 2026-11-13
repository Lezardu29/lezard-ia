"""Didacticiel pour débuter (tutorial.html) et visite guidée du chat (tour.js), dans l'application seulement : pour
des personnes qui ne connaissent rien à l'IA. Les leçons se lisent sans clé ; rien n'est envoyé tout seul."""

import re

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.core import WEB_DIR
from gateway.main import create_app
from gateway.store import DocumentStore

PAGE = (WEB_DIR / "tutorial.html").read_text(encoding="utf-8")


def test_la_page_se_lit_sans_cle_et_respecte_les_regles_des_pages():
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        r = c.get("/didacticiel")  # aucune clé envoyée
        assert r.status_code == 200 and "script-src 'self'" in r.headers["content-security-policy"]
        assert "<script>" not in r.text and " style=" not in r.text and " onclick=" not in r.text
        for name in ("tutorial.css", "tutorial.js", "tour.js", "tour.css"):
            assert c.get(f"/static/{name}").status_code == 200, name
        for image in re.findall(r'src="(/static/tuto/[^"]+)"', r.text):
            assert c.get(image).status_code == 200, image


def test_dix_lecons_chacune_avec_ce_qu_il_faut_retenir_et_son_bouton():
    lessons = re.findall(r'<article class="lesson" id="lecon-(\d+)" data-lesson="(\d+)">(.*?)</article>', PAGE, re.S)
    assert [int(a) for a, _, _ in lessons] == list(range(1, 11)) and all(a == b for a, b, _ in lessons)
    for n, _, body in lessons:
        assert "<h2>" in body and 'class="lesson-keep"' in body and "À retenir" in body, n
        assert body.count('class="btn btn-primary lesson-done"') == 1, n
    assert PAGE.count('href="#lecon-') == 11  # le plan (10) et le bouton « Commencer »
    for image in re.findall(r"<img [^>]+>", PAGE):
        if "/static/tuto/" in image:
            assert re.search(r'alt="[^"]{30,}"', image) and "width=" in image and "height=" in image, image  # décrite
    # les mots du métier sont expliqués dans le lexique
    for word in ("Modèle", "Carte graphique", "Token", "Ollama", "Marketplace", "Licence", "Serveur MCP"):
        assert f"<dt>{word}" in PAGE, word


def test_chaque_question_a_une_bonne_reponse_parmi_ses_choix():
    quizzes = re.findall(r'<fieldset class="quiz" data-answer="(\w)">(.*?)</fieldset>', PAGE, re.S)
    assert len(quizzes) >= 3
    for answer, body in quizzes:
        values = re.findall(r'<input type="radio" name="\w+" value="(\w)">', body)
        assert answer in values and len(values) == 3 and "data-good=" in body and "data-bad=" in body


def test_une_demande_preparee_est_ecrite_dans_la_saisie_jamais_envoyee():
    chat = (WEB_DIR / "static" / "chat.js").read_text(encoding="utf-8")
    block = chat[chat.index('params.get("demande")') : chat.index("cadre d'une autre origine")]
    assert '$("prompt").value = asked' in block and "submit(" not in block and "generate(" not in block
    assert 'params.delete("demande")' in block and "slice(0, 2000)" in chat  # l'adresse est nettoyée, la taille bornée
    for link in re.findall(r'href="/app\?demande=([^"]+)"', PAGE):
        assert len(link) < 600 and "<" not in link


def test_la_visite_guidee_ne_montre_que_des_elements_qui_existent():
    tour = (WEB_DIR / "static" / "tour.js").read_text(encoding="utf-8")
    app = (WEB_DIR / "app.html").read_text(encoding="utf-8")
    targets = re.findall(r'\{ target: "([\w-]+)"', tour)
    assert len(targets) >= 6
    for target in targets:
        assert f'id="{target}"' in app, target
    assert ".click()" not in tour.split("export function startTour")[0]  # elle montre, elle n'agit pas
    for wanted in ('id="first-steps"', 'id="tour-start"', 'id="tuto-link"', 'id="tour-btn"', 'href="/didacticiel"'):
        assert wanted in app, wanted
    js = (WEB_DIR / "static" / "app.js").read_text(encoding="utf-8")
    assert "initTour()" in js and app.index("/static/tour.css") < app.index("/static/mobile.css")
