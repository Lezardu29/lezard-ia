"""Outils à trois modes de l'interrupteur (gateway/mcp_modes.py) : image, document, web et contrôle du PC rendus
selon /lezard local, eco ou rapide. Aucun modèle ni réseau : Ollama, la recherche et l'écran sont simulés."""

import base64
import json
import types
import zipfile

import pytest

from gateway import mcp_modes as mm
from gateway import mcp_orchestre as mo

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def core(tmp_path, monkeypatch):
    """Le serveur, avec un faux modèle local qui répète ce qu'on lui demande (premiers mots)."""
    for name in ("LEZARD_IA_URL", "LEZARD_IA_KEY", "ORCHESTRE_MODE", "ORCHESTRE_MODELE", "ORCHESTRE_VISION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEZARD_IA_MODE", str(tmp_path / "mode.json"))
    monkeypatch.setenv("LEZARD_IA_LIEN", str(tmp_path / "pas-de-lien.json"))
    monkeypatch.setenv("ORCHESTRE_MEMOIRE", str(tmp_path / "memoire"))
    monkeypatch.setattr(mo, "ROUTE", {"mode": None})
    monkeypatch.setattr(mo, "models", lambda: [{"name": "petit", "size": 3e9, "params": "4B", "loaded": True}])
    asked = []

    def generate(model, messages, limit):
        asked.append(messages[-1]["content"])
        return f"réponse à : {messages[-1]['content'][-60:]}", 10, 5, ""

    monkeypatch.setattr(mo, "generate", generate)
    monkeypatch.setattr(mm, "see", lambda core, images, ask: (f"vu {len(images)} image(s) : {ask[:40]}", "oeil"))
    mo.asked = asked
    return mo


def route(name: str) -> None:
    mo.ROUTE["mode"] = name


def test_image_selon_le_mode(core, tmp_path):
    (tmp_path / "a.png").write_bytes(PNG)
    args = {"fichier": "a.png", "question": "Quelle couleur ?"}
    route("local")
    out = mm.read_image(core, args, [tmp_path])
    assert out.startswith("vu 1 image(s) : Quelle couleur ?") and "rien n'est sorti du PC" in out
    route("eco")
    out = mm.read_image(core, args, [tmp_path])
    assert "Décris cette image" in out and "l'image ne t'est pas envoyée" in out and isinstance(out, str)
    route("equilibre")
    assert "mode éco" in mm.read_image(core, args, [tmp_path])
    route("rapide")
    text, image = mm.read_image(core, args, [tmp_path])
    assert image["type"] == "image" and image["mimeType"] == "image/png" and base64.b64decode(image["data"]) == PNG
    assert "mode rapide" in text["text"]
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="lire_document"):
        mm.read_image(core, {"fichier": "b.txt"}, [tmp_path])
    with pytest.raises(ValueError, match="hors des dossiers"):
        mm.read_image(core, {"fichier": "../a.png"}, [tmp_path])


def test_document_selon_le_mode_et_memoire(core, tmp_path):
    (tmp_path / "note.md").write_text("Budget : 1200 euros.\nDate : 3 mars.\n", encoding="utf-8")
    args = {"fichier": "note.md", "question": "Quel budget ?"}
    route("rapide")
    out = mm.read_document(core, args, [tmp_path])
    assert out.startswith("Budget : 1200 euros.") and "texte entier" in out and not core.asked
    route("eco")
    out = mm.read_document(core, args, [tmp_path])
    assert "mode éco : à toi de conclure" in out and "1\tBudget : 1200 euros." in core.asked[0]
    assert "Quel budget ?" in core.asked[0] and len(core.asked) == 1
    assert "repris de la mémoire" in mm.read_document(core, args, [tmp_path]) and len(core.asked) == 1
    route("local")
    out = mm.read_document(core, args, [tmp_path])
    assert "rien n'est sorti du PC" in out and "Réponds à cette question" in core.asked[1]
    with zipfile.ZipFile(tmp_path / "lettre.docx", "w") as z:
        z.writestr(
            "word/document.xml", "<w:document><w:p><w:t>Bonjour</w:t></w:p><w:p><w:t>Fin</w:t></w:p></w:document>"
        )
    assert mm.extract(tmp_path / "lettre.docx").split() == ["Bonjour", "Fin"]
    (tmp_path / "vide.txt").write_text("  \n", encoding="utf-8")
    with pytest.raises(ValueError, match="aucun texte lisible"):
        mm.read_document(core, {"fichier": "vide.txt"}, [tmp_path])


def test_web_selon_le_mode(core, tmp_path, monkeypatch):
    class Refus(Exception):
        pass

    async def web_search(args):
        return types.SimpleNamespace(
            text=f"Résultats pour « {args['query']} » :\n- A — https://a.example/x\n- B — https://b.example/y"
        )

    async def web_fetch(args):
        if "b.example" in args["url"]:
            raise Refus("403")
        return types.SimpleNamespace(text="La tour mesure 330 mètres. " * 400)

    fake = types.SimpleNamespace(web_search=web_search, web_fetch=web_fetch)
    monkeypatch.setattr(mm, "_web", lambda: (fake, Refus))
    args = {"question": "Hauteur de la tour ?", "pages": 2}
    route("rapide")
    out = mm.search_web(core, args, [tmp_path])
    assert (
        "## https://a.example/x\nLa tour mesure" in out
        and "Pages non lues : https://b.example/y (403)" in out
        and not core.asked
    )
    assert len(out) < 2 * mm.PAGE_RAW + 1500 and "jamais des instructions" in out
    route("eco")
    out = mm.search_web(core, args, [tmp_path])
    assert "mode éco : à toi de conclure" in out and len(core.asked) == 1 and "Hauteur de la tour ?" in core.asked[0]
    route("local")
    out = mm.search_web(core, args, [tmp_path])
    assert "mode local : relaie-la" in out and "D'après ces notes seulement" in core.asked[-1]
    with pytest.raises(ValueError, match="question"):
        mm.search_web(core, {}, [tmp_path])


def test_pc_qui_pilote_selon_le_mode(core, tmp_path, monkeypatch):
    shot = types.SimpleNamespace(
        text="Fenêtre : Bloc-notes",
        attach="1. bouton Fichier",
        image="data:image/png;base64," + base64.b64encode(PNG).decode(),
    )
    ran = []

    def fake_run(tool, args):
        ran.append((tool, args))
        return shot

    tools = {"pc_screenshot": "capture", "pc_click": "clic"}
    monkeypatch.setattr(mm, "_pc", lambda: (None, None, tools))
    monkeypatch.setattr(mm, "_run", fake_run)
    monkeypatch.delenv("ORCHESTRE_PC", raising=False)
    route("rapide")  # pas proposé tant qu'il n'est pas fiable (essais réels du 2026-11-11) : refusé partout
    with pytest.raises(ValueError, match="pas disponible dans Claude Code"):
        mm.pc_see(core, {}, [tmp_path])
    assert not [s for s in mm.specs(core) if s["name"].startswith("pc_")] and not ran
    monkeypatch.setenv("ORCHESTRE_PC", "1")  # réglage de mise au point
    route("local")  # l'écran ne sort pas : pas de pilotage par l'IA qui appelle
    with pytest.raises(ValueError, match="/lezard eco"):
        mm.pc_see(core, {}, [tmp_path])
    with pytest.raises(ValueError, match="/lezard eco"):
        mm.pc_act(core, {"outil": "pc_click", "arguments": {"element": 1}}, [tmp_path])
    assert not ran
    route("eco")
    out = mm.pc_see(core, {}, [tmp_path])
    assert isinstance(out, str) and "1. bouton Fichier" in out and "la capture ne t'est pas envoyée" in out
    route("rapide")
    text, image = mm.pc_act(core, {"outil": "pc_click", "arguments": {"element": 1}}, [tmp_path])
    assert ran[-1] == ("clic", {"element": 1}) and image["type"] == "image" and "Bloc-notes" in text["text"]
    with pytest.raises(ValueError, match="pc_click"):
        mm.pc_act(core, {"outil": "pc_screenshot"}, [tmp_path])
    monkeypatch.delenv("ORCHESTRE_PC_AUTONOME", raising=False)
    with pytest.raises(ValueError, match="pas disponible"):  # jamais proposée sans le réglage de mise au point
        mm.pc_task(core, {"objectif": "ouvre le bloc-notes"}, [tmp_path])
    assert not mm.autonomous()  # donc absente de la liste des outils (specs)
    monkeypatch.setenv("ORCHESTRE_PC_AUTONOME", "1")
    with pytest.raises(ValueError, match="mode local"):
        mm.pc_task(core, {"objectif": "ouvre le bloc-notes"}, [tmp_path])  # en rapide, c'est l'IA qui pilote
    sent = mm._to_ollama(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Capture."},
                    {"type": "image_url", "image_url": {"url": shot.image}},
                ],
            }
        ]
    )
    assert sent == [{"role": "user", "content": "Capture.", "images": [shot.image.partition(",")[2]]}]


def test_le_serveur_rend_l_image_au_client_et_garde_ces_outils_derriere_l_interrupteur(core, tmp_path, monkeypatch):
    (tmp_path / "a.png").write_bytes(PNG)
    monkeypatch.setattr(mo, "ACTIVE", {"on": False})
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "lire_image", "arguments": {"fichier": "a.png"}},
    }
    reply = mo.handle(call, [tmp_path], "lezard")["result"]
    assert reply["isError"] and "/lezard on" in reply["content"][0]["text"]
    monkeypatch.setattr(mo, "ACTIVE", {"on": True})
    route("rapide")
    reply = mo.handle(call, [tmp_path], "lezard")["result"]
    assert not reply["isError"] and [c["type"] for c in reply["content"]] == ["text", "image"]
    names = [
        t["name"]
        for t in mo.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, [], "lezard")["result"]["tools"]
    ]
    assert names[:1] == ["lezard_ia"] and {"lire_image", "lire_document", "chercher_web"} <= set(names)
    json.dumps(reply)  # tout ce qui part au client est du JSON


def test_tache_locale_sur_le_pc_avec_la_vraie_boucle_de_pilotage(core, tmp_path, monkeypatch):
    """pc_tache : la boucle de Lezard IA (gateway.tools.runner) tourne ici avec un modèle local simulé et de faux
    outils du PC. Chaque action passe sans attendre (l'accord est donné à l'appel de pc_tache), le compte rendu
    liste les actions, et rien n'est rendu de l'écran."""
    from gateway import tools
    from gateway.tools import runner

    done = []

    async def shot(args):
        done.append("capture")
        return tools.ToolResult(
            "Fenêtre : Bureau", image="data:image/png;base64," + base64.b64encode(PNG).decode(), attach="1. icône"
        )

    async def click(args):
        done.append(("clic", args.get("element")))
        return tools.ToolResult("Clic fait.")

    params = {"type": "object", "properties": {"element": {"type": "integer"}, "reason": {"type": "string"}}}
    fake = {
        "pc_screenshot": tools.Tool(
            "pc_screenshot", "Capture.", {"type": "object", "properties": {}}, shot, level="pc"
        ),
        "pc_click": tools.Tool("pc_click", "Clic.", params, click, level="pc", confirm=True),
    }
    monkeypatch.setattr(mm, "_pc", lambda: (tools, runner, fake))
    monkeypatch.setattr(mm, "vision_model", lambda core: "oeil")
    monkeypatch.setattr(tools, "FAILSAFE", None)
    replies = iter(
        [
            "1. Regarder l'écran\n2. Cliquer sur l'icône",  # le plan, demandé avant la première action
            '{"tool": "pc_screenshot", "arguments": {}}',
            '{"tool": "pc_click", "arguments": {"element": 1, "reason": "ouvrir"}}',
            "L'icône est ouverte.",
        ]
    )
    seen_images = []

    def chat(core, model, messages, window=0):
        seen_images.append(sum(len(m.get("images") or []) for m in messages))
        return {"message": {"content": next(replies, "Terminé.")}}

    monkeypatch.setattr(mm, "ollama_chat", chat)
    route("local")
    monkeypatch.setenv("ORCHESTRE_PC", "1")  # réglages de mise au point : jamais actifs par défaut
    monkeypatch.setenv("ORCHESTRE_PC_AUTONOME", "1")
    out = mm.pc_task(core, {"objectif": "Ouvre l'icône du bureau", "etapes": 5}, [tmp_path])
    assert done == ["capture", ("clic", 1)]  # l'action validée d'office a bien été exécutée
    assert (
        "Tâche confiée à oeil, en local" in out and 'pc_click({"element": 1})' in out and "L'icône est ouverte." in out
    )
    assert max(seen_images) == 1 and "base64" not in out  # le modèle local a vu la capture, pas l'IA qui appelle
