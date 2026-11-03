"""Banc d'essai de l'agent PC (27.2) : une dizaine de tâches de référence sur un faux bureau Windows, jouées par la
vraie boucle de l'agent (guide, planificateur, état de la tâche, règle « la souris d'abord », détection d'écran
inchangé), avec le modèle local choisi. Rien n'est fait sur le vrai PC.

    python -m gateway.tools.bench                         gemma3:12b et gemma4:e4b, toutes les tâches
    python -m gateway.tools.bench --models gemma4:e4b --tasks bloc_notes,discord_message --repeat 3

Le faux bureau dessine des captures (fenêtres, barre des tâches, menu Démarrer, numéros jaunes) et joint la même
liste d'éléments et le même texte de contexte que les vrais outils. Chaque tâche a un critère de réussite vérifié
sur l'état final du bureau, jamais sur ce que le modèle affirme. Résultats : taux de réussite, actions sur le PC
et durée par modèle, à l'écran et dans data/bench/*.json.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import string
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote_plus, unquote_plus

import httpx

from . import PC_GUIDE, TOOLS, Tool, ToolError, ToolResult, runner
from .rules import mouse_first
from .state import TaskState

W, H, TASKBAR, COLS = 1280, 720, 48, 16
TOP = 32  # barre de titre de la fenêtre au premier plan
BOTTOM = H - TASKBAR


def plain(text: str) -> str:
    return unicodedata.normalize("NFKD", str(text).lower()).encode("ascii", "ignore").decode()


@dataclass
class Element:
    kind: str
    name: str
    box: tuple[int, int, int, int]
    act: Callable[[str], None] | None = None  # bouton : left | right | double
    value: str = ""  # texte affiché dans un champ


@dataclass
class Window:
    app: str
    title: str
    data: dict = field(default_factory=dict)


APPS = {  # application -> (nom affiché, noms reconnus par pc_open et la recherche du menu Démarrer)
    "notepad": ("Bloc-notes", ("bloc-notes", "bloc notes", "notepad", "bloc")),
    "calc": ("Calculatrice", ("calculatrice", "calculator", "calc")),
    "edge": ("Microsoft Edge", ("microsoft edge", "edge", "navigateur", "navigateur web", "internet", "browser")),
    "explorer": ("Explorateur de fichiers", ("explorateur de fichiers", "explorateur", "explorer", "fichiers")),
    "discord": ("Discord", ("discord",)),
}
FOLDERS = {
    "Documents": ["rapport.pdf", "notes.txt"],
    "Téléchargements": ["setup.exe", "photo.jpg"],
    "Images": ["vacances.png"],
}
KEYS = {"enter", "tab", "esc", "escape", "win", "backspace", "delete", "space", "up", "down", "left", "right", "home",
        "end", "ctrl", "alt", "shift", "f4", "f5", "a", "c", "l", "n", "s", "t", "v", "w", "x"}  # fmt: skip


def find_app(name: str) -> str | None:
    want = plain(name).strip()
    if not want:
        return None
    for app, (label, aliases) in APPS.items():
        if want == plain(label) or want in aliases or any(want in a or a in want for a in aliases if len(a) > 3):
            return app
    return None


class Desktop:
    """État du faux bureau ; ses méthodes sont les outils pc_* du banc d'essai."""

    def __init__(self, open_apps=(), foreground: str | None = None):
        self.windows: list[Window] = []  # la dernière est au premier plan
        self.start: dict | None = None  # menu Démarrer ouvert : {"search": texte}
        self.focus: tuple | None = None  # (fenêtre | "start", champ)
        self.last = {"kind": None, "refused": 0}  # règle « la souris d'abord » (rules.py)
        self.elements: list[Element] = []
        self.sig, self.unchanged = None, False
        self.opened_files: list[str] = []
        self.actions = 0  # actions sur le PC exécutées
        for app in open_apps:
            self.open_app(app)
        if foreground:
            self.bring(next(w for w in self.windows if w.app == foreground))
        self.focus = None
        self._shoot()  # première empreinte

    # ---------- état ----------
    @property
    def front(self) -> Window | None:
        return self.windows[-1] if self.windows and self.start is None else None

    def window(self, app: str) -> Window | None:
        return next((w for w in self.windows if w.app == app), None)

    def bring(self, w: Window) -> None:
        self.windows.remove(w)
        self.windows.append(w)
        self.start = None
        self.focus = (w, "address") if w.app == "edge" else (w, "text") if w.app == "notepad" else None

    def open_app(self, app: str) -> None:
        w = self.window(app)
        if w is None:
            data = {
                "notepad": {"text": ""},
                "edge": {"url": "", "address": ""},
                "explorer": {"folder": None, "search": "", "results": None},
                "discord": {"conv": None, "draft": "", "sent": []},
            }.get(app, {})
            title = {"notepad": "Sans titre - Bloc-notes", "edge": "Nouvel onglet - Microsoft Edge",
                     "explorer": "Accueil - Explorateur de fichiers"}.get(app, APPS.get(app, (app,))[0])  # fmt: skip
            w = Window(app, title, data)
            self.windows.append(w)
        self.bring(w)

    def open_file(self, name: str) -> None:
        self.opened_files.append(name)
        app = "pdf" if name.endswith(".pdf") else "file"
        self.windows.append(Window(app, f"{name} - {'Lecteur PDF' if app == 'pdf' else 'Visionneuse'}", {"file": name}))
        self.start, self.focus = None, None

    def close(self, w: Window) -> None:
        self.windows.remove(w)
        self.focus = None

    def go(self, w: Window, address: str) -> None:
        text = address.strip()
        url = text if text.startswith(("http://", "https://")) else None
        if url is None and "." in text and " " not in text:
            url = "https://" + text
        w.data["url"] = url or "https://www.bing.com/search?q=" + quote_plus(text)
        w.data["address"] = w.data["url"]
        w.title = f"{w.data['url'].split('//')[1].split('/')[0]} - Microsoft Edge"

    def submit(self) -> None:
        """Entrée dans le champ qui a le focus."""
        if self.focus is None:
            return
        target, fld = self.focus
        if target == "start":
            found = self.search_results()
            if found:
                self.open_app(found[0])
            return
        if target.app == "notepad":
            target.data["text"] += "\n"
        elif target.app == "edge" and target.data["address"].strip():
            self.go(target, target.data["address"])
        elif target.app == "explorer" and fld == "search":
            q = plain(target.data["search"]).strip()
            target.data["results"] = [f for files in FOLDERS.values() for f in files if q and q in plain(f)]
            target.title = f"Résultats de la recherche - {target.data['search']}"
        elif target.app == "discord" and fld == "draft" and target.data["draft"].strip():
            target.data["sent"].append((target.data["conv"], target.data["draft"]))
            target.data["draft"] = ""

    def search_results(self) -> list[str]:
        q = (self.start or {}).get("search", "")
        app = find_app(q) if q.strip() else None
        return [app] if app else []

    # ---------- disposition : éléments cliquables de l'écran affiché ----------
    def layout(self) -> list[Element]:
        els: list[Element] = []
        add = lambda *a, **k: els.append(Element(*a, **k))  # noqa: E731
        w = self.front
        if w is not None:
            add("bouton", "Fermer", (W - 46, 0, W, TOP), lambda b: self.close(w))
            getattr(self, f"_layout_{w.app}", lambda w, add: None)(w, add)
        elif self.start is None:
            add("élément", "Corbeille", (20, 20, 100, 100))
        if self.start is not None:
            add("champ", "Rechercher des applications, des paramètres et des documents", (20, 196, 580, 232),
                lambda b: setattr(self, "focus", ("start", "search")), self.start["search"])  # fmt: skip
            results = self.search_results()
            if results:
                for i, app in enumerate(results):
                    add("élément", f"{APPS[app][0]} · Application", (20, 250 + 44 * i, 580, 290 + 44 * i),
                        lambda b, a=app: self.open_app(a))  # fmt: skip
            else:
                for i, app in enumerate(("notepad", "calc", "edge", "explorer")):
                    x = 20 + 140 * i
                    add("bouton", APPS[app][0], (x, 260, x + 130, 340), lambda b, a=app: self.open_app(a))
        # barre des tâches
        add("bouton", "Démarrer", (0, BOTTOM, 100, H), lambda b: self._toggle_start())
        add("bouton", "Rechercher", (104, BOTTOM, 230, H), lambda b: self._toggle_start(force=True))
        x = 240
        for app in ("explorer", "edge"):
            if self.window(app) is None:
                add("bouton", APPS[app][0], (x, BOTTOM, x + 150, H), lambda b, a=app: self.open_app(a))
                x += 154
        for win in self.windows:
            add("bouton", f"{win.title} - 1 fenêtre ouverte", (x, BOTTOM, x + 170, H), lambda b, v=win: self.bring(v))
            x += 174
        return els

    def _toggle_start(self, force: bool = False) -> None:
        if self.start is not None and not force:
            self.start, self.focus = None, None
        else:
            self.start, self.focus = {"search": ""}, ("start", "search")

    def _layout_notepad(self, w: Window, add) -> None:
        for i, menu in enumerate(("Fichier", "Modifier", "Affichage")):
            add("menu", menu, (10 + 90 * i, TOP + 4, 90 + 90 * i, TOP + 28))
        add("champ", "Zone de texte", (10, TOP + 36, W - 10, BOTTOM - 10),
            lambda b: setattr(self, "focus", (w, "text")), w.data["text"])  # fmt: skip

    def _layout_calc(self, w: Window, add) -> None:
        for i, key in enumerate(("7", "8", "9", "+", "4", "5", "6", "=")):
            x, y = 400 + 120 * (i % 4), 200 + 100 * (i // 4)
            add("bouton", key, (x, y, x + 110, y + 90))

    def _layout_edge(self, w: Window, add) -> None:
        add("onglet", w.title.removesuffix(" - Microsoft Edge"), (10, TOP + 2, 260, TOP + 30))
        add("champ", "Barre d'adresse et de recherche", (120, TOP + 36, W - 200, TOP + 68),
            lambda b: setattr(self, "focus", (w, "address")), w.data["address"])  # fmt: skip

    def _layout_explorer(self, w: Window, add) -> None:
        add("champ", "Rechercher", (W - 330, TOP + 6, W - 60, TOP + 36),
            lambda b: setattr(self, "focus", (w, "search")), w.data["search"])  # fmt: skip
        for i, name in enumerate(["Accueil", *FOLDERS]):

            def nav(b, n=name):
                w.data.update(folder=None if n == "Accueil" else n, results=None)
                w.title = f"{n} - Explorateur de fichiers"

            add("élément", name, (10, TOP + 60 + 40 * i, 220, TOP + 94 + 40 * i), nav)
        if w.data["results"] is not None:
            items, folder = w.data["results"], None
        elif w.data["folder"]:
            items, folder = FOLDERS[w.data["folder"]], w.data["folder"]
        else:
            items, folder = list(FOLDERS), None
        for i, name in enumerate(items):

            def open_item(b, n=name, home=w.data["results"] is None and folder is None):
                if b != "double":
                    return  # un clic sélectionne seulement, comme dans Windows
                if home:
                    w.data["folder"], w.title = n, f"{n} - Explorateur de fichiers"
                else:
                    self.open_file(n)

            add("élément", name, (240, TOP + 60 + 40 * i, 900, TOP + 94 + 40 * i), open_item)

    def _layout_discord(self, w: Window, add) -> None:
        for i, name in enumerate(("Velistu", "Marc")):

            def pick(b, n=name):
                w.data["conv"], w.data["draft"] = n, ""
                self.focus = None

            add("élément", name, (10, TOP + 60 + 44 * i, 240, TOP + 98 + 44 * i), pick)
        if w.data["conv"]:
            add("champ", f"Envoyer un message à @{w.data['conv']}", (260, BOTTOM - 60, W - 20, BOTTOM - 16),
                lambda b: setattr(self, "focus", (w, "draft")), w.data["draft"])  # fmt: skip

    # ---------- capture ----------
    def context(self) -> str:
        active = "(bureau ou menu Démarrer)" if self.front is None else self.front.title
        others = [w.title for w in self.windows if w.title != active]
        text = f"Fenêtre au premier plan : « {active} ». Autres fenêtres ouvertes : " + (
            "; ".join(f"« {t} »" for t in others) or "aucune"
        )
        if self.unchanged:
            text += (
                ". ATTENTION : l'écran n'a pas changé depuis la capture précédente, la dernière action n'a servi à rien"
            )
        return text + "."

    def elements_text(self) -> str:
        return "Éléments cliquables (numéros jaunes sur la capture) : " + "; ".join(
            f"[{i}] {e.kind} « {e.name} »" for i, e in enumerate(self.elements, 1)
        )

    def _signature(self) -> str:
        wins = [(w.app, w.title, json.dumps(w.data, ensure_ascii=False, default=str)) for w in self.windows]
        return json.dumps([wins, self.start, str(self.focus and self.focus[1])], ensure_ascii=False)

    def _shoot(self) -> str:
        sig = self._signature()
        self.unchanged = self.sig is not None and sig == self.sig
        self.sig = sig
        self.elements = self.layout()
        return render(self)

    def _after(self) -> ToolResult:
        image = self._shoot()
        return ToolResult(f"Action faite. Nouvelle capture jointe. {self.context()}", image, self.elements_text(),
                          self.unchanged)  # fmt: skip

    def _target(self, args: dict) -> Element:
        if args.get("element") not in (None, ""):
            try:  # « "2" » accepté, comme les vrais outils
                n = int(args["element"])
            except (TypeError, ValueError):
                n = 0
            if not 1 <= n <= len(self.elements):
                raise ToolError(f"'element' : numéro 1 à {len(self.elements)} de la dernière capture.")
            return self.elements[n - 1]
        if args.get("cell"):
            cell = str(args["cell"]).strip().upper()
            col, row = string.ascii_uppercase.find(cell[:1]), cell[1:]
            if col < 0 or col >= COLS or not row.isdigit():
                raise ToolError(f"Case inconnue : {cell}.")
            size = W / COLS
            x, y = (col + 0.5) * size, (int(row) - 0.5) * size
        elif isinstance(args.get("x"), (int, float)) and isinstance(args.get("y"), (int, float)):
            x, y = args["x"], args["y"]
        else:
            raise ToolError("Indiquez 'element' (numéro), 'cell' (ex. « C4 ») ou 'x' et 'y'.")
        hit = [e for e in self.elements if e.box[0] <= x <= e.box[2] and e.box[1] <= y <= e.box[3]]
        return hit[-1] if hit else Element("vide", "", (0, 0, 0, 0))

    # ---------- outils ----------
    async def screenshot(self, args: dict) -> ToolResult:
        image = self._shoot()
        return ToolResult(f"Capture jointe ({W}×{H}). {self.context()}", image, self.elements_text())

    async def click(self, args: dict) -> ToolResult:
        button = args.get("button") or "left"
        if button not in ("left", "right", "double"):
            raise ToolError("'button' : left, right ou double.")
        e = self._target(args)
        self.actions += 1
        if e.act:
            e.act(button)
        self.last.update(kind="click", refused=0)
        return self._after()

    async def type_text(self, args: dict) -> ToolResult:
        mouse_first("type", self.last, bool(self.elements), self.unchanged)
        text = str(args.get("text") or "")
        if not text or len(text) > 500:
            raise ToolError("'text' requis (500 caractères max).")
        self.actions += 1
        if self.focus is not None:
            target, fld = self.focus
            if target == "start":
                self.start["search"] += text
            else:
                target.data[fld] = target.data.get(fld, "") + text
                if target.app == "explorer" and fld == "search":  # Windows 11 cherche pendant la frappe
                    self.submit()
        self.last.update(kind="type")
        return self._after()

    async def press(self, args: dict) -> ToolResult:
        mouse_first("keyboard", self.last, bool(self.elements), self.unchanged)
        names = [k.strip().lower() for k in str(args.get("keys") or "").split("+") if k.strip()]
        if not names or len(names) > 4 or any(n not in KEYS for n in names):
            raise ToolError("'keys' : touches séparées par +, ex. « ctrl+s », « enter », « alt+f4 ».")
        self.actions += 1
        combo = "+".join(names)
        if combo == "enter":
            self.submit()
        elif combo == "win":
            self._toggle_start()
        elif combo in ("esc", "escape"):
            self.start = None
        elif combo == "alt+f4" and self.front is not None:
            self.close(self.front)
        elif combo == "ctrl+l" and self.front is not None and self.front.app == "edge":
            self.focus = (self.front, "address")
        self.last.update(kind="keyboard")
        return self._after()

    async def open_app_tool(self, args: dict) -> ToolResult:
        name = str(args.get("name") or "").strip()
        if not name or len(name) > 60:
            raise ToolError("'name' requis : nom de l'application, ex. « bloc-notes » (60 caractères max).")
        self.actions += 1
        app = find_app(name)
        if app:
            self.open_app(app)
        self.last.update(kind="command")
        return self._after()

    async def open_url(self, args: dict) -> ToolResult:
        url = str(args.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ToolError("'url' : adresse http(s)://… attendue.")
        self.actions += 1
        self.open_app("edge")
        self.go(self.window("edge"), url)
        self.last.update(kind="command")
        return self._after()

    async def focus_window(self, args: dict) -> ToolResult:
        wanted = plain(args.get("title") or "").strip()
        if not wanted:
            raise ToolError("'title' requis : une partie du titre de la fenêtre.")
        w = next((w for w in self.windows if wanted in plain(w.title)), None)
        if w is None:
            raise ToolError(f"Aucune fenêtre ne contient « {wanted} ». {self.context()}")
        self.actions += 1
        self.bring(w)
        self.last.update(kind="command")
        return self._after()

    async def scroll(self, args: dict) -> ToolResult:
        self.actions += 1
        self.last.update(kind="scroll")
        return self._after()

    async def zoom(self, args: dict) -> ToolResult:
        return ToolResult("Vue agrandie indisponible sur ce bureau d'essai : vise avec 'element'.")

    def tools(self) -> dict[str, Tool]:
        """Mêmes noms, descriptions et paramètres que les vrais outils (quand ils sont chargés, sous Windows)."""
        runs = {"pc_screenshot": self.screenshot, "pc_click": self.click, "pc_type": self.type_text,
                "pc_key": self.press, "pc_open": self.open_app_tool, "pc_open_url": self.open_url,
                "pc_focus": self.focus_window, "pc_scroll": self.scroll, "pc_zoom": self.zoom}  # fmt: skip
        out = {}
        for name, run in runs.items():
            real = TOOLS.get(name)
            desc, params, confirm = (real.description, real.parameters, real.confirm) if real else FALLBACK[name]
            out[name] = Tool(name, desc, params, run, level="pc", confirm=confirm)
        return out


_R = {"reason": {"type": "string", "description": "Ce que tu vois sur la capture et pourquoi cette action"}}


def _spec(desc: str, props: dict, required=(), confirm=True) -> tuple:
    return desc, {"type": "object", "properties": {**_R, **props}, "required": ["reason", *required]}, confirm


FALLBACK = {  # hors Windows (tests) : les vrais outils ne sont pas chargés
    "pc_screenshot": ("Capture l'écran du PC.", {"type": "object", "properties": {}}, False),
    "pc_click": _spec(
        "Clique : 'element' = numéro jaune.",
        {
            "element": {"type": "integer"},
            "cell": {"type": "string"},
            "button": {"type": "string", "enum": ["left", "right", "double"]},
        },
    ),  # fmt: skip
    "pc_type": _spec("Tape un texte.", {"text": {"type": "string"}}, ("text",)),
    "pc_key": _spec("Appuie sur une touche, ex. « enter ».", {"keys": {"type": "string"}}, ("keys",)),
    "pc_open": _spec("Ouvre une application par son nom.", {"name": {"type": "string"}}, ("name",)),
    "pc_open_url": _spec("Ouvre une adresse web.", {"url": {"type": "string"}}, ("url",)),
    "pc_focus": _spec("Met une fenêtre au premier plan.", {"title": {"type": "string"}}, ("title",)),
    "pc_scroll": _spec("Fait défiler.", {"direction": {"type": "string"}}, ("direction",)),
    "pc_zoom": ("Agrandit une zone.", {"type": "object", "properties": {"cell": {"type": "string"}}}, False),
}


def _font():
    """Police de Windows (accents corrects) ; à défaut, celle de Pillow."""
    from PIL import ImageFont

    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, 14)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=14)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def render(d: Desktop) -> str:
    """Capture dessinée : fenêtre au premier plan, menu Démarrer, barre des tâches, numéros jaunes."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), (30, 64, 110))
    draw = ImageDraw.Draw(img)
    font = _font()
    w = d.front
    if w is not None:
        draw.rectangle((0, 0, W, BOTTOM), fill=(246, 246, 246))
        draw.rectangle((0, 0, W, TOP), fill=(222, 222, 226))
        draw.text((12, 9), w.title, fill=(20, 20, 20), font=font)
        if w.app == "discord" and w.data["conv"]:
            for i, (_conv, text) in enumerate(m for m in w.data["sent"] if m[0] == w.data["conv"]):
                draw.text((270, TOP + 70 + 26 * i), f"Vous : {text}", fill=(30, 30, 30), font=font)
        if w.app == "edge" and w.data["url"]:
            draw.text((40, TOP + 110), f"Page affichée : {w.data['url']}", fill=(30, 30, 30), font=font)
        if w.app in ("pdf", "file"):
            draw.text((40, TOP + 60), f"Fichier ouvert : {w.data['file']}", fill=(30, 30, 30), font=font)
    if d.start is not None:
        draw.rectangle((0, 180, 600, BOTTOM), fill=(44, 44, 52))
    draw.rectangle((0, BOTTOM, W, H), fill=(24, 24, 28))
    for i, e in enumerate(d.elements, 1):
        x0, y0, x1, y1 = e.box
        on_bar = y0 >= BOTTOM or (d.start is not None and x1 <= 600 and y0 >= 180)
        fill = (60, 60, 70) if on_bar else (255, 255, 255) if e.kind == "champ" else (232, 238, 247)
        draw.rectangle(e.box, fill=fill, outline=(130, 130, 140))
        label = "X" if e.name == "Fermer" else e.value if e.kind == "champ" and e.value else e.name
        while label and draw.textlength(label, font=font) > x1 - x0 - 30:  # coupé à la largeur de l'élément
            label = label[:-2] + "…" if len(label) > 2 else ""
        draw.text((x0 + 26, y0 + 6), label, fill=(235, 235, 235) if on_bar else (25, 25, 25), font=font)
        draw.rectangle((x0, y0, x0 + 22, y0 + 17), fill=(255, 214, 0))
        draw.text((x0 + 3, y0 + 1), str(i), fill=(0, 0, 0), font=font)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------- tâches de référence ----------
@dataclass(frozen=True)
class Task:
    id: str
    goal: str
    check: Callable[[Desktop], bool]
    open_apps: tuple = ()
    foreground: str | None = None


def _edge_url(d: Desktop) -> str:
    w = d.window("edge")
    return plain(unquote_plus(w.data["url"])) if w else ""


TASKS = [
    Task("bloc_notes", "Ouvre le bloc-notes et écris « bonjour ».",
         lambda d: any(w.app == "notepad" and "bonjour" in plain(w.data["text"]) for w in d.windows)),
    Task("calculatrice", "Ouvre la calculatrice.", lambda d: d.window("calc") is not None),
    Task("page_web", "Ouvre la page https://fr.wikipedia.org dans le navigateur.",
         lambda d: "wikipedia.org" in _edge_url(d)),
    Task("premier_plan", "Mets Discord au premier plan.", lambda d: d.front is not None and d.front.app == "discord",
         ("discord", "notepad"), "notepad"),
    Task("discord_message", "Envoie « salut » à Velistu sur Discord.",
         lambda d: any(c == "Velistu" and "salut" in plain(t) for c, t in (d.window("discord") or Window("", "", {"sent": []})).data["sent"]),
         ("discord", "notepad"), "notepad"),
    Task("telechargements", "Ouvre le dossier Téléchargements dans l'explorateur de fichiers.",
         lambda d: (d.window("explorer") is not None and d.window("explorer").data["folder"] == "Téléchargements")),
    Task("chercher_fichier", "Trouve le fichier rapport.pdf avec l'explorateur de fichiers et ouvre-le.",
         lambda d: "rapport.pdf" in d.opened_files),
    Task("fermer", "Ferme le bloc-notes.", lambda d: d.window("notepad") is None, ("notepad",), "notepad"),
    Task("recherche_web", "Recherche « météo Brest » sur Internet dans le navigateur.",
         lambda d: "meteo" in _edge_url(d) and "brest" in _edge_url(d)),
    Task("rien_a_faire", "Combien font 12 fois 12 ? Réponds sans rien faire sur le PC.", lambda d: d.actions == 0,
         ("notepad",), "notepad"),
]  # fmt: skip


# ---------- modèle : Ollama de ce PC, outils natifs ou décrits en texte (gemma3) ----------
class Ollama:
    def __init__(self, model: str, base_url: str, client: httpx.AsyncClient, num_ctx: int = runner.PC_CONTEXT):
        self.model, self.base, self.client, self.num_ctx = model, base_url.rstrip("/"), client, num_ctx or 16384
        self.emulated, self.names, self.all_specs = False, set(), []

    async def _chat(self, msgs: list, specs: list | None) -> dict:
        from ..providers.ollama_native import from_native, to_native

        payload = {"model": self.model, "messages": msgs, **({"tools": specs} if specs else {})}
        body = to_native(payload, {"num_ctx": self.num_ctx}, None)
        r = await self.client.post(f"{self.base}/api/chat", json=body, timeout=600)
        if r.status_code == 400 and specs and "support" in r.text and "tools" in r.text:
            raise _NoTools()
        r.raise_for_status()
        return from_native(r.json(), self.model)

    async def __call__(self, msgs: list, specs: list | None) -> tuple[dict, str]:
        if specs:
            self.all_specs, self.names = specs, {s["function"]["name"] for s in specs}
        if not self.emulated:
            try:
                return await self._chat(msgs, specs), "local"
            except _NoTools:
                self.emulated = True
        resp = await self._chat(runner.emulated(msgs, self.all_specs if specs else []), None)
        return runner.parse_emulated(resp, self.names), "local"


class _NoTools(Exception):
    pass


async def run_task(task: Task, call, max_steps: int) -> dict:
    desktop = Desktop(task.open_apps, task.foreground)
    approvals = runner.Approvals()
    state = TaskState(task.goal, max_steps)
    started, text, error, trail = time.monotonic(), "", None, []
    try:
        async for chunk in runner.run(
            call=call,
            tools=desktop.tools(),
            messages=[{"role": "system", "content": PC_GUIDE}, {"role": "user", "content": task.goal}],
            approvals=approvals,
            identity="bench",
            audit=lambda *a: None,
            max_steps=max_steps,
            state=state,
        ):
            for choice in chunk.get("choices") or []:
                text += (choice.get("delta") or {}).get("content") or ""
            ev = chunk.get("x_gateway_tool") or {}
            if ev.get("type") == "approval":  # pas de validation humaine sur le faux bureau
                approvals.decide(ev["approval"], "bench", True)
            if ev.get("type") == "call":  # trace : comprendre un échec
                args = {k: v for k, v in (ev.get("args") or {}).items() if k != "reason"}
                trail.append(f"{ev['name']} {json.dumps(args, ensure_ascii=False)}")
            elif ev.get("type") == "result" and trail and not ev.get("ok"):
                trail[-1] += f" → {ev.get('text', '')[:120]}"
    except (httpx.HTTPError, ValueError) as e:
        error = f"{type(e).__name__}: {e}"
    return {
        "task": task.id,
        "success": error is None and task.check(desktop),
        "actions": desktop.actions,
        "turns": state.steps + 1,
        "plan": state.plan,
        "seconds": round(time.monotonic() - started, 1),
        "answer": text.strip()[:300],
        "error": error,
        "trail": trail,
    }


def summary(results: list[dict]) -> dict:
    n = len(results) or 1
    ok = [r for r in results if r["success"]]
    return {
        "tasks": len(results),
        "success": len(ok),
        "success_rate": round(len(ok) / n, 3),
        "mean_actions_success": round(sum(r["actions"] for r in ok) / len(ok), 1) if ok else None,
        "mean_seconds": round(sum(r["seconds"] for r in results) / n, 1),
    }


async def bench(models: list[str], tasks: list[Task], repeat: int, base_url: str, max_steps: int, log=print) -> dict:
    out = {"date": time.strftime("%Y-%m-%d %H:%M"), "max_steps": max_steps, "models": {}}
    async with httpx.AsyncClient() as client:
        for model in models:
            results = []
            for task in tasks:
                for _ in range(repeat):
                    r = await run_task(task, Ollama(model, base_url, client), max_steps)
                    results.append(r)
                    mark = "réussie" if r["success"] else "ÉCHEC"
                    log(f"  {model:<18} {task.id:<18} {mark:<8} {r['actions']:>2} actions  {r['seconds']:>6.1f} s"
                        + (f"  ({r['error']})" if r["error"] else ""))  # fmt: skip
            out["models"][model] = {"summary": summary(results), "results": results}
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Banc d'essai de l'agent PC (faux bureau, rien n'est fait sur ce PC).")
    p.add_argument("--models", default="gemma3:12b,gemma4:e4b")
    p.add_argument("--tasks", default="", help="identifiants séparés par des virgules (défaut : toutes)")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--base-url", default=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"))
    p.add_argument("--max-steps", type=int, default=int(os.getenv("TOOLS_MAX_STEPS", "12")))
    p.add_argument("--out", default=os.path.join("data", "bench"))
    a = p.parse_args(argv)
    wanted = {t for t in a.tasks.split(",") if t}
    tasks = [t for t in TASKS if not wanted or t.id in wanted]
    if wanted - {t.id for t in TASKS}:
        p.error(f"tâches inconnues : {', '.join(sorted(wanted - {t.id for t in TASKS}))}")
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    print(f"Banc d'essai : {len(tasks)} tâches × {a.repeat}, modèles {', '.join(models)}")
    report = asyncio.run(bench(models, tasks, a.repeat, a.base_url, a.max_steps))
    print("\nModèle              Réussite   Actions (réussies)   Durée moyenne")
    for model, data in report["models"].items():
        s = data["summary"]
        actions = "—" if s["mean_actions_success"] is None else f"{s['mean_actions_success']:.1f}"
        print(f"{model:<19} {s['success']:>2}/{s['tasks']:<2} ({s['success_rate']:.0%})   {actions:>8}"
              f"             {s['mean_seconds']:.1f} s")  # fmt: skip
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, time.strftime("bench-%Y%m%d-%H%M%S.json"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\nDétail : {path}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
