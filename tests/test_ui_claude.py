"""Interface « Claude » (static/ui-claude.css + theme.js) : une deuxième interface, au choix, sur les mêmes pages.
Elle ne s'applique qu'avec <html data-ui="claude"> : l'interface Lezard IA reste telle quelle."""

import re

from gateway import preferences
from gateway.core import WEB_DIR

CSS = (WEB_DIR / "static" / "ui-claude.css").read_text(encoding="utf-8")
THEME = (WEB_DIR / "static" / "theme.js").read_text(encoding="utf-8")


def test_every_rule_is_scoped_to_the_claude_interface():
    body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    for selectors in re.findall(r"([^{}]+)\{", body):
        if selectors.strip().startswith("@media"):  # bloc pour téléphone : ses règles sont vérifiées une à une
            continue
        for selector in selectors.split(","):
            assert selector.strip().startswith(':root[data-ui="claude"]'), selector


def test_the_claude_theme_exists_on_both_sides():
    assert '"claude"' in THEME and "fixed:" in THEME
    assert "claude" in preferences.PRESETS
    assert preferences.validate_theme({"mode": "auto", "preset": "claude"})["preset"] == "claude"


def test_the_interface_choice_is_offered_and_read_once_from_the_address():
    panel = (WEB_DIR / "static" / "theme-panel.js").read_text(encoding="utf-8")
    assert '["claude", "Claude"]' in panel and "update({ ui: value })" in panel  # envoyé au compte avec le thème
    assert "askedUi" in THEME and 'setAttribute("data-ui"' in THEME


def test_every_page_loads_it_after_claude_css():
    for page in ("app.html", "admin.html", "models.html", "login.html"):
        html = (WEB_DIR / page).read_text(encoding="utf-8")
        assert html.index('href="/static/ui-claude.css"') > html.index('href="/static/claude.css"'), page


def test_the_interface_follows_the_account_like_the_theme():
    import pytest

    assert preferences.validate_theme({"mode": "dark", "preset": "claude", "ui": "claude"})["ui"] == "claude"
    # l'interface Claude est celle par défaut : le choix de l'interface Lezard IA est donc gardé
    assert preferences.validate_theme({"mode": "auto", "preset": "default", "ui": "classic"})["ui"] == "classic"
    assert "ui" not in preferences.validate_theme({"mode": "auto", "preset": "default"})
    with pytest.raises(preferences.PreferenceError):
        preferences.validate_theme({"mode": "auto", "preset": "default", "ui": "autre"})


def test_claude_interface_is_the_default_with_colours_to_choose():
    assert 'var DEFAULT_THEME = { mode: "auto", preset: "claude", ui: "claude" };' in THEME
    assert ': "claude";' in THEME.split("function getUi()")[1].split("}")[0]  # aucune interface choisie : Claude
    assert "var legacy = " in THEME  # un thème choisi s'applique aussi dans l'interface Claude


def test_the_model_chips_open_the_model_picker():
    bar = (WEB_DIR / "static" / "settings-bar.js").read_text(encoding="utf-8")
    picker = (WEB_DIR / "static" / "model-picker.js").read_text(encoding="utf-8")
    assert 'import("./model-picker.js")' in bar and "openModelPicker" in bar
    # mêmes réglages que /mode, /modele, /effort et /multi
    for setter in ("setMode", "setForcedModel", "setEffort", "setMulti"):
        assert setter in picker
    assert "textContent = text" in picker and "innerHTML = CHECK" in picker  # noms des modèles jamais interprétés


def test_long_model_lists_fold_behind_show_more():
    picker = (WEB_DIR / "static" / "model-picker.js").read_text(encoding="utf-8")
    assert "Afficher plus" in picker and "Afficher moins" in picker and "FOLDED" in picker
    assert "m.id === forced" in picker  # le modèle imposé reste visible, même replié


def test_the_context_ring_measures_the_context_and_compacts_on_tap():
    ring = (WEB_DIR / "static" / "context-ring.js").read_text(encoding="utf-8")
    chat = (WEB_DIR / "static" / "chat.js").read_text(encoding="utf-8")
    panel = (WEB_DIR / "static" / "usage-panel.js").read_text(encoding="utf-8")
    assert "context_window" in ring and "/compacte" in ring
    assert 'mountContextRing($("toolbar"), () => compact())' in chat  # « Compacter » : le même compactage que /compacte
    assert 'button.addEventListener("click", () => openUsagePanel())' in ring  # un appui : le panneau « Utilisation »
    assert "Compacter la conversation" in panel and 'api("/v1/me")' in panel
    assert "contextStart()" in chat.split("function contextUsage")[1].split("\n}")[0]  # depuis le dernier résumé


def test_the_ring_shows_its_percentage_and_compacts_itself_when_full():
    ring = (WEB_DIR / "static" / "context-ring.js").read_text(encoding="utf-8")
    chat = (WEB_DIR / "static" / "chat.js").read_text(encoding="utf-8")
    assert "SHOW_PCT = 0.5" in ring and "FULL = 0.9" in ring and "export const AUTO_COMPACT = FULL" in ring
    assert "return size ? last.tokens / size : 0" in ring  # fenêtre inconnue : jamais de compactage automatique
    assert 'contextRatio() >= AUTO_COMPACT) compact("", true)' in chat


def test_the_model_picker_offers_the_private_mode():
    picker = (WEB_DIR / "static" / "model-picker.js").read_text(encoding="utf-8")
    assert "Conversation privée" in picker and "setPrivate(on)" in picker  # même bascule que /prive
    assert 'r.hosting !== "cloud"' in picker and 'hosting === "local"' in picker  # selon les droits de la clé


def test_the_model_picker_and_slash_command_toggle_skills():
    picker = (WEB_DIR / "static" / "model-picker.js").read_text(encoding="utf-8")
    slash = (WEB_DIR / "static" / "slash-commands.js").read_text(encoding="utf-8")
    bar = (WEB_DIR / "static" / "settings-bar.js").read_text(encoding="utf-8")
    assert "skillsGroup()" in picker and "setSkill(s.id, checked)" in picker and "Awesome Claude Skills" in picker
    assert 'name: "competence"' in slash and "setSkill(s.id, next)" in slash
    assert "skills: settings.skills" in bar  # envoyées avec chaque message


def test_the_context_ring_stays_hidden_until_the_context_fills_and_the_panel_stays_reachable():
    ring = (WEB_DIR / "static" / "context-ring.js").read_text(encoding="utf-8")
    html = (WEB_DIR / "app.html").read_text(encoding="utf-8")
    assert "SHOW_RING = 0.25" in ring and "button.hidden = ratio < SHOW_RING;" in ring
    assert (
        'id="usage-btn"' in html
        and 'getElementById("usage-btn")?.addEventListener("click", () => openUsagePanel())' in ring
    )


def test_the_sidebar_holds_the_account_menu_and_gives_everything_back():
    html = (WEB_DIR / "app.html").read_text(encoding="utf-8")
    side = (WEB_DIR / "static" / "sidebar.js").read_text(encoding="utf-8")
    mobile = (WEB_DIR / "static" / "mobile.js").read_text(encoding="utf-8")
    conv = (WEB_DIR / "static" / "conversations.css").read_text(encoding="utf-8")
    for id_ in ("sb-toggle", "sb-new", "sb-search", "acct-btn", "acct-menu", "conv-foot"):
        assert f'id="{id_}"' in html, id_
    assert ".sb { display: none; }" in conv  # interface Lezard IA : ni barre latérale, ni menu du compte
    # mêmes nœuds déplacés (écouteurs gardés), y compris depuis le tiroir du téléphone ; rendus à leur place
    assert 'querySelectorAll("[data-drawer]:not(#who)")' in side and "holdDrawer(true)" in side
    assert "state.marks[i].after(item)" in side and "holdDrawer(false)" in side
    assert "T?.onChange?.(" in side  # interface changée dans le panneau Apparence : sans recharger
    assert "btn.hidden = hold.held" in mobile and "item.parentNode === body" in mobile
    assert '$("acct-name").textContent = name' in side  # nom de la clé : jamais interprété


def test_the_sidebar_docks_earlier_and_folds_into_a_rail():
    conv = (WEB_DIR / "static" / "conversations-ui.js").read_text(encoding="utf-8")
    assert '"(min-width: 900px)"' in conv and '"(min-width: 1100px)"' in conv
    assert '"conv-rail", !on && wide.matches && claudeUi()' in conv
    assert "if (wide.matches && ui.usable() && remember) write(DOCK_KEY" in conv  # choix gardé intact
    assert "body.conv-rail .conv-panel" in CSS and "body.conv-docked { padding-left: 288px; }" in CSS


def test_reasoning_is_folded_and_never_sent_back():
    chat = (WEB_DIR / "static" / "chat.js").read_text(encoding="utf-8")
    assert r"const THINK = /^\s*<think>" in chat and '<details class="reasoning"' in chat
    assert "delta?.reasoning_content ?? chunk.choices?.[0]?.delta?.reasoning" in chat
    assert chat.count("stripCommand(content) : forModel(content)") == 2  # réponse et compactage
    assert "copyText(answerOf(" in chat and "toggleSpeech(listen, answerOf(" in chat
    assert "renderMarkdown(m[1].trim())" in chat  # la réflexion passe par le même rendu échappé que la réponse


def test_the_account_footer_shows_the_profile_name():
    side = (WEB_DIR / "static" / "sidebar.js").read_text(encoding="utf-8")
    assert 'displayName() || $("who").textContent.trim()' in side  # le prénom des Paramètres, sinon la clé
    assert "onProfileChange(() => showName(me))" in side  # mis à jour quand on le change


def test_answer_metrics_keep_only_the_model_name_until_hovered():
    css = (WEB_DIR / "static" / "ui-claude.css").read_text(encoding="utf-8")
    assert ".metrics:not([open]) summary .m:not(.m-local):not(.m-cloud) { display: none; }" in css
    assert ".msg:hover .metrics summary .m" in css


def test_the_edit_pencil_shows_on_hover_or_tap_only():
    css = (WEB_DIR / "static" / "ui-claude.css").read_text(encoding="utf-8")
    chat = (WEB_DIR / "static" / "chat.js").read_text(encoding="utf-8")
    assert ':root[data-ui="claude"] .msg.user .msg-actions { opacity: 0;' in css
    assert ".msg.user.acts .msg-actions" in css and ".msg-actions:focus-within" in css  # clavier : jamais invisible
    assert ".msg-actions:has(.msg-versions)" in css  # les flèches ‹ 1/2 › restent visibles
    assert 'mine.classList.toggle("acts")' in chat


def test_retry_and_other_model_share_one_menu():
    chat = (WEB_DIR / "static" / "chat.js").read_text(encoding="utf-8")
    menu = (WEB_DIR / "static" / "row-menu.js").read_text(encoding="utf-8")
    assert "Autre modèle" not in chat and "msg-model" not in chat
    assert 'menu.className = "row-menu align-start msg-retry"' in chat and 'box.append(pick("Réessayer"))' in chat
    assert "regenerate(i, model)" in chat and "initRowMenus();" in chat
    assert "if (ready) return;" in menu  # appelé par le chat et par la liste des conversations


def test_the_plus_button_offers_photo_or_file_on_touch_screens():
    html = (WEB_DIR / "app.html").read_text(encoding="utf-8")
    att = (WEB_DIR / "static" / "attachments.js").read_text(encoding="utf-8")
    assert 'id="camera"' not in html and 'id="camera-input"' in html  # un seul « + »
    assert "attachSheet(fileInput, cameraInput).showModal()" in att and "fileInput.click()" in att
    assert '"Prendre une photo"' in att


def test_the_input_hint_stays_on_one_line_on_phones():
    app = (WEB_DIR / "static" / "app.js").read_text(encoding="utf-8")
    css = (WEB_DIR / "static" / "chat.css").read_text(encoding="utf-8")
    # le texte le plus long qui tient dans le champ lui-même (barre latérale, zoom), pas selon la largeur de l'écran
    assert "new ResizeObserver(() => showPlaceholder())" in app and "measureText(h).width <= room" in app
    assert '"Posez votre question…"' in app and '"Votre question…"' in app
    assert "white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" in css  # aussi en interface classique


def test_classic_chat_tucks_the_top_bar_buttons_into_a_menu():
    mobile = (WEB_DIR / "static" / "mobile.js").read_text(encoding="utf-8")
    css = (WEB_DIR / "static" / "mobile.css").read_text(encoding="utf-8")
    assert 'const menuPage = document.body.classList.contains("chat-page");' in mobile  # le chat seulement
    assert "else if (compact.matches || menuPage) body.append(...items);" in mobile
    assert "body.chat-page .menu-btn:not([hidden]) { display: grid; }" in css


def test_conversations_are_grouped_by_period():
    conv = (WEB_DIR / "static" / "conversations-ui.js").read_text(encoding="utf-8")
    for label in ('"Aujourd\'hui"', '"Hier"', '"7 derniers jours"', '"30 derniers jours"'):
        assert label in conv
    assert "const p = ui.query ? null : period(c.updated_at);" in conv  # recherche : pas de groupes


def test_welcome_suggestions_vary():
    chat = (WEB_DIR / "static" / "chat.js").read_text(encoding="utf-8")
    html = (WEB_DIR / "app.html").read_text(encoding="utf-8")
    assert 'class="suggestion"' not in html and "showSuggestions();" in chat
    assert chat.count("Math.random()") >= 1


def test_shortcuts_help_and_code_download():
    shortcuts = (WEB_DIR / "static" / "shortcuts.js").read_text(encoding="utf-8")
    app = (WEB_DIR / "static" / "app.js").read_text(encoding="utf-8")
    md = (WEB_DIR / "static" / "markdown.js").read_text(encoding="utf-8")
    save = (WEB_DIR / "static" / "save-file.js").read_text(encoding="utf-8")
    assert 'e.key === "/"' in shortcuts and "initShortcutsHelp();" in app
    assert 'class="code-dl" data-download="${id}" data-lang="${escapeHtml(lang)}"' in md
    assert "export function saveCode(button)" in save and 'python: "py"' in save
    for page in ("chat.js", "share.js"):
        assert "saveCode(dl)" in (WEB_DIR / "static" / page).read_text(encoding="utf-8")


def test_bug_report_link_opens_a_prefilled_github_form():
    html = (WEB_DIR / "app.html").read_text(encoding="utf-8")
    app = (WEB_DIR / "static" / "app.js").read_text(encoding="utf-8")
    form = (WEB_DIR.parents[1] / ".github" / "ISSUE_TEMPLATE" / "bug.yml").read_text(encoding="utf-8")
    # dans la barre du haut (data-drawer) : menu ☰ et menu du compte de l'interface Claude
    assert 'href="https://lezard-ia.fr/signaler.html"' in html  # formulaire du site : aucun compte demandé
    assert 'id="bug-link"' in html and 'rel="noopener"' in html and "Signaler un bug" in html
    assert 'fillBugLink($("bug-link"), me);' in app
    # champs préremplis : ceux du formulaire ; la version seulement pour la clé maître (/v1/me ne la donne qu'à elle)
    assert 'url.searchParams.set("systeme", system);' in app and "if (me.version)" in app
    assert "id: version" in form and "id: systeme" in form and "jamais ici de clé" in form
