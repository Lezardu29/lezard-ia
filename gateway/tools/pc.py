"""Contrôle du PC (agent Computer Use, phases 22 et 23) : capture annotée d'une grille de repérage, clic, saisie,
touches, défilement. Windows uniquement, sans dépendance (ctypes + Pillow).

Sécurité : chaque action attend la validation de l'utilisateur (aperçu de la cible) ; arrêt d'urgence en
plaçant la souris dans le coin haut gauche de l'écran ; un seul agent à la fois (verrou du runner)."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import difflib
import io
import os
import string
import subprocess
import sys
import time
import unicodedata
from ctypes import wintypes
from pathlib import Path

from . import StopRun, Tool, ToolError, ToolResult, register
from .rules import mouse_first

MAX_WIDTH = 1280
COLS = 16
ZOOM_GRID = 6  # sous-cases par côté dans pc_zoom
ZOOM_SIZE = 720
MAX_ELEMENTS = 80  # éléments numérotés par capture (au-delà : trop de tokens, image illisible)
FRESH = 20  # secondes : une capture plus récente est encore valable (pas de nouvelle capture sans action)
_user32 = ctypes.windll.user32
_shot: dict = {}  # dernière capture : image, échelle, grille


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


VK = {
    "ctrl": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "enter": 0x0D,
    "esc": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
    "delete": 0x2E,
    "space": 0x20,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
}
VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})
VK.update({c: ord(c.upper()) for c in string.ascii_lowercase + string.digits})


def _dpi_aware() -> None:
    try:  # coordonnées physiques : la capture et la souris parlent les mêmes pixels (écran à 125 %, 150 %)
        _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass


EMERGENCY = "Arrêt d'urgence : la souris est dans le coin haut gauche de l'écran."


def in_corner() -> bool:
    """Souris dans le coin haut gauche : de l'écran principal, ou du bureau entier quand un autre écran est à
    gauche ou au-dessus (ses coordonnées sont alors négatives). Surveillé en continu pendant une tâche (runner)."""
    p = wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(p)):
        return False  # bureau sécurisé (UAC, verrouillage) : position inconnue
    left, top = _user32.GetSystemMetrics(76), _user32.GetSystemMetrics(77)  # SM_X/YVIRTUALSCREEN
    return (abs(p.x) <= 2 and abs(p.y) <= 2) or (p.x <= left + 2 and p.y <= top + 2)


def _check_failsafe() -> None:
    if in_corner():
        raise StopRun(EMERGENCY)


def _label(col: int, row: int) -> str:
    return f"{string.ascii_uppercase[col]}{row + 1}"


def _encode(img) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _title(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value.strip()


def _windows() -> list[tuple[int, str]]:
    """Fenêtres visibles avec un titre (ordre de l'écran : premier plan d'abord)."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        title = _title(hwnd)
        if _user32.IsWindowVisible(hwnd) and title and title not in ("Program Manager", "Paramètres"):
            found.append((hwnd, title))
        return True

    _user32.EnumWindows(each, 0)
    return found[:15]


def foreground() -> tuple[int, str]:
    """La fenêtre au premier plan : (identifiant, titre)."""
    hwnd = _user32.GetForegroundWindow()
    return int(hwnd or 0), _title(hwnd)


def open_windows() -> set[int]:
    """Identifiants de toutes les fenêtres visibles, pour savoir plus tard lesquelles étaient déjà là."""
    found: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if _user32.IsWindowVisible(hwnd):
            found.add(int(hwnd or 0))
        return True

    _user32.EnumWindows(each, 0)
    return found


def _context() -> str:
    """Texte joint à chaque capture : les petits modèles lisent mal l'écran, les titres de fenêtres les ancrent."""
    active = _title(_user32.GetForegroundWindow()) or "(bureau ou menu Démarrer)"
    others = [t for _, t in _windows() if t != active]
    text = f"Fenêtre au premier plan : « {active} ». Autres fenêtres ouvertes : " + (
        "; ".join(f"« {t[:60]} »" for t in others) or "aucune"
    )
    if _shot.get("unchanged"):
        text += ". ATTENTION : l'écran n'a pas changé depuis la capture précédente, la dernière action n'a servi à rien"
    return text + "."


KINDS = {"Button": "bouton", "SplitButton": "bouton", "MenuItem": "menu", "Hyperlink": "lien", "Edit": "champ",
         "ComboBox": "liste", "ListItem": "élément", "TabItem": "onglet", "TreeItem": "arborescence",
         "CheckBox": "case à cocher", "RadioButton": "option", "DataItem": "ligne"}  # fmt: skip
_SCRIPT = Path(__file__).with_name("ui_elements.ps1")
_RING = Path(__file__).with_name("click_ring.pyw")


def _elements() -> list[dict]:
    """Éléments cliquables réels (UI Automation de Windows, via PowerShell : rien à installer). [] en cas d'échec."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(_SCRIPT)],
            capture_output=True, timeout=6, creationflags=0x08000000,  # CREATE_NO_WINDOW
        ).stdout.decode("utf-8", "replace")  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        parts = line.lstrip("﻿").split("\t")
        if len(parts) == 6 and all(p.lstrip("-").isdigit() for p in parts[2:]):
            x, y, w, h = map(int, parts[2:])
            found.append({"kind": KINDS.get(parts[0], parts[0].lower()), "name": parts[1][:60], "x": x, "y": y,
                          "w": w, "h": h})  # fmt: skip
    return found[:MAX_ELEMENTS]


def elements_text() -> str:
    """Liste jointe à la capture (et retirée avec elle du contexte quand elle vieillit)."""
    items = _shot.get("elements") or []
    if not items:
        return "Aucun élément détecté automatiquement : vise avec la grille (colonne lettre + ligne chiffre)."
    return "Éléments cliquables (numéros jaunes sur la capture) : " + "; ".join(
        f"[{i}] {e['kind']} « {e['name'] or 'sans nom'} »" for i, e in enumerate(items, 1)
    )


def _capture() -> str:
    from PIL import ImageDraw, ImageFont, ImageGrab

    _dpi_aware()
    raw = ImageGrab.grab()
    sig = raw.resize((64, 36)).convert("L").tobytes()  # empreinte : l'écran a-t-il changé ?
    old = _shot.get("sig")
    unchanged = old is not None and sum(abs(a - b) for a, b in zip(old, sig)) / len(sig) < 1.5
    scale = min(1.0, MAX_WIDTH / raw.width)
    img = raw.resize((round(raw.width * scale), round(raw.height * scale))) if scale < 1 else raw.copy()
    cell = img.width / COLS
    rows = max(1, round(img.height / cell))
    cell_h = img.height / rows
    draw = ImageDraw.Draw(img, "RGBA")
    try:
        font = ImageFont.load_default(size=11)
    except TypeError:
        font = ImageFont.load_default()
    # grille de secours discrète (lettres en haut, numéros à gauche) : les éléments numérotés passent devant
    for c in range(COLS):
        draw.line((c * cell, 0, c * cell, img.height), fill=(255, 0, 80, 45))
        draw.text((c * cell + 3, 1), string.ascii_uppercase[c], fill=(255, 0, 80, 220), font=font)
    for r in range(rows):
        draw.line((0, r * cell_h, img.width, r * cell_h), fill=(255, 0, 80, 45))
        draw.text((2, r * cell_h + 2), str(r + 1), fill=(255, 0, 80, 220), font=font)
    elements = _elements()
    for i, e in enumerate(elements, 1):  # Set-of-Mark : chaque élément cliquable porte son numéro
        x0, y0 = e["x"] * scale, e["y"] * scale
        draw.rectangle((x0, y0, x0 + e["w"] * scale, y0 + e["h"] * scale), outline=(255, 200, 0, 230), width=2)
        w = 7 * len(str(i)) + 4
        draw.rectangle((x0, y0, x0 + w, y0 + 13), fill=(255, 200, 0, 235))
        draw.text((x0 + 2, y0), str(i), fill=(0, 0, 0, 255), font=font)
    _shot.update(
        elements=elements,
        image=img,
        scale=scale,
        rows=rows,
        cell=cell,
        cell_h=cell_h,
        raw=raw,
        zoom=None,
        sig=sig,
        unchanged=unchanged,
        at=time.monotonic(),
    )
    return _encode(img)


def _target(args: dict) -> tuple[float, float]:
    """Point visé, en pixels de la capture : sous-case du dernier zoom, case de la grille (« C4 ») ou x / y."""
    if not _shot:
        raise ToolError("Prenez d'abord une capture (pc_screenshot).")
    img = _shot["image"]
    if args.get("element") not in (None, ""):
        items = _shot.get("elements") or []
        try:
            e = items[int(args["element"]) - 1] if int(args["element"]) >= 1 else None
        except (TypeError, ValueError, IndexError):
            e = None
        if e is None:
            raise ToolError(f"'element' : numéro 1 à {len(items)} de la dernière capture." if items else
                            "Aucun élément numéroté sur la dernière capture : utilise 'cell'.")  # fmt: skip
        s = _shot["scale"]
        return (e["x"] + e["w"] / 2) * s, (e["y"] + e["h"] / 2) * s
    if args.get("zoom") not in (None, ""):
        z = _shot.get("zoom")
        try:
            n = int(args["zoom"]) - 1
        except (TypeError, ValueError):
            n = -1
        if not z or not 0 <= n < ZOOM_GRID**2:
            raise ToolError(f"'zoom' : numéro 1 à {ZOOM_GRID**2} de la dernière vue agrandie (pc_zoom d'abord).")
        x0, y0, size = z
        step = size / ZOOM_GRID
        return x0 + (n % ZOOM_GRID + 0.5) * step, y0 + (n // ZOOM_GRID + 0.5) * step
    cell = str(args.get("cell") or "").strip().upper()
    if cell:
        col, row = string.ascii_uppercase.find(cell[:1]), int(cell[1:]) - 1 if cell[1:].isdigit() else -1
        if not (0 <= col < COLS and 0 <= row < _shot["rows"]):
            raise ToolError(f"Case inconnue : {cell} (A1 à {_label(COLS - 1, _shot['rows'] - 1)}).")
        return (col + 0.5) * _shot["cell"], (row + 0.5) * _shot["cell_h"]
    try:
        x, y = float(args["x"]), float(args["y"])
    except (KeyError, TypeError, ValueError):
        raise ToolError("Indiquez 'cell' (ex. « C4 ») ou 'x' et 'y' en pixels de la capture.") from None
    if not (0 <= x < img.width and 0 <= y < img.height):
        raise ToolError(f"Point hors de l'écran (capture de {img.width}×{img.height}).")
    return x, y


def _zoom(cell: str) -> str:
    """Vue agrandie (×3) des 3×3 cases autour de `cell`, découpée en sous-cases numérotées 1…36."""
    from PIL import ImageDraw, ImageFont

    cx, cy = _target({"cell": cell})
    size = 3 * _shot["cell"]
    img = _shot["image"]
    x0 = min(max(0, cx - size / 2), img.width - size)
    y0 = min(max(0, cy - size / 2), img.height - size)
    s = _shot["scale"]  # découpe dans l'écran en pleine résolution : plus net que la capture réduite
    crop = _shot["raw"].crop((round(x0 / s), round(y0 / s), round((x0 + size) / s), round((y0 + size) / s)))
    view = crop.resize((ZOOM_SIZE, ZOOM_SIZE))
    draw = ImageDraw.Draw(view, "RGBA")
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:
        font = ImageFont.load_default()
    step = ZOOM_SIZE / ZOOM_GRID
    for i in range(ZOOM_GRID**2):
        x, y = (i % ZOOM_GRID) * step, (i // ZOOM_GRID) * step
        draw.rectangle((x, y, x + step, y + step), outline=(255, 0, 80, 120))
        draw.rectangle((x + 1, y + 1, x + 24, y + 17), fill=(0, 0, 0, 150))
        draw.text((x + 4, y + 1), str(i + 1), fill=(255, 255, 255, 235), font=font)
    _shot["zoom"] = (x0, y0, size)
    return _encode(view)


async def zoom(args: dict) -> ToolResult:
    cell = str(args.get("cell") or "").strip().upper()
    if not cell:
        raise ToolError("'cell' requis : case de la capture à agrandir, ex. « C4 ».")
    image = await asyncio.to_thread(_zoom, cell)
    return ToolResult(
        f"Vue agrandie autour de {cell} jointe, sous-cases 1 à {ZOOM_GRID**2} (de gauche à droite, puis de haut en "
        "bas). Pour cliquer : pc_click avec 'zoom' = numéro de la sous-case.",
        image,
    )


def _preview_point(args: dict) -> str:
    from PIL import ImageDraw

    x, y = _target(args)
    img = _shot["image"].copy()
    d = ImageDraw.Draw(img)
    for r, w in ((18, 4), (4, 4)):
        d.ellipse((x - r, y - r, x + r, y + r), outline=(255, 0, 0), width=w)
    return _encode(img)


def _preview_screen(args: dict) -> str | None:
    return _encode(_shot["image"]) if _shot else None


def _send(*inputs: _INPUT) -> None:
    arr = (_INPUT * len(inputs))(*inputs)
    _user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))


def _key(vk: int = 0, up: bool = False, char: int = 0) -> _INPUT:
    flags = (0x2 if up else 0) | (0x4 if char else 0)  # KEYUP, UNICODE
    return _INPUT(type=1, ki=_KEYBDINPUT(wVk=vk, wScan=char, dwFlags=flags))


def _mouse(flags: int, data: int = 0) -> _INPUT:
    return _INPUT(type=0, mi=_MOUSEINPUT(mouseData=ctypes.c_ulong(data).value, dwFlags=flags))


def _type(text: str) -> None:
    units = text.encode("utf-16-le")  # caractères hors BMP (emoji) : deux unités UTF-16
    for i in range(0, len(units), 2):
        code = int.from_bytes(units[i : i + 2], "little")
        _send(_key(char=code), _key(char=code, up=True))


async def _after() -> ToolResult:
    await asyncio.sleep(0.8)
    image = await asyncio.to_thread(_capture)
    return ToolResult(
        f"Action faite. Nouvelle capture jointe. {_context()}", image, elements_text(), bool(_shot.get("unchanged"))
    )


def still_fresh() -> str | None:
    """Capture de moins de FRESH secondes : le message à renvoyer au modèle au lieu d'en prendre une autre."""
    if time.monotonic() - _shot.get("at", -FRESH) < FRESH:
        return "La capture jointe juste avant est toujours à jour : analyse-la (décris ce que tu vois) puis agis. " + (
            _context()
        )
    return None


async def screenshot(args: dict) -> ToolResult:
    image = await asyncio.to_thread(_capture)
    return ToolResult(
        f"Capture jointe ({_shot['image'].width}×{_shot['image'].height}), grille A1 à "
        f"{_label(COLS - 1, _shot['rows'] - 1)} (colonnes A→{string.ascii_uppercase[COLS - 1]}, "
        f"lignes 1→bas). {_context()}",
        image,
        elements_text(),
    )


async def open_url(args: dict) -> ToolResult:
    _check_failsafe()
    url = str(args.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")) or len(url) > 2000 or any(c in url for c in '\r\n"<>'):
        raise ToolError("'url' : adresse http(s)://… attendue.")
    os.startfile(url)  # navigateur par défaut, avec les comptes déjà connectés de l'utilisateur
    await asyncio.sleep(2.0)  # la page se charge
    _last.update(kind="command")
    return await _after()


async def focus(args: dict) -> ToolResult:
    _check_failsafe()
    wanted = str(args.get("title") or "").strip().lower()
    if not wanted:
        raise ToolError("'title' requis : une partie du titre de la fenêtre.")
    hwnd = next((h for h, t in _windows() if wanted in t.lower()), None)
    if hwnd is None:
        raise ToolError(f"Aucune fenêtre ne contient « {wanted} ». {_context()}")
    _user32.ShowWindow(hwnd, 9)  # SW_RESTORE : si elle est réduite
    _send(_key(VK["alt"]), _key(VK["alt"], up=True))  # Windows n'autorise le premier plan qu'après une saisie
    _user32.SetForegroundWindow(hwnd)
    _last.update(kind="command")
    return await _after()


# ---------- souris d'abord : le clavier n'est permis que si la souris ne peut pas faire l'affaire ----------
_last = {"kind": None, "refused": 0}  # dernière action ; refus clavier consécutifs


def _mouse_first(kind: str) -> None:
    """Règle « la souris d'abord » (rules.py) appliquée à la dernière capture."""
    mouse_first(kind, _last, bool(_shot.get("elements")), bool(_shot.get("unchanged")))


def _show_ring(x: int, y: int) -> None:
    """Cercle rouge à l'écran, là où l'agent clique (fenêtre transparente aux clics, sans prise de focus)."""
    try:
        subprocess.Popen(
            [os.path.join(os.path.dirname(sys.executable), "pythonw.exe"), str(_RING), str(x), str(y), "700"],
            creationflags=0x08000000,
        )
    except OSError:
        pass  # sans cercle, le clic se fait quand même


async def _glide(x: int, y: int) -> None:
    """Le curseur glisse jusqu'à la cible (≈ 0,3 s) : l'utilisateur voit où l'agent va cliquer."""
    p = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    for i in range(1, 13):
        _user32.SetCursorPos(round(p.x + (x - p.x) * i / 12), round(p.y + (y - p.y) * i / 12))
        await asyncio.sleep(0.025)


async def click(args: dict) -> ToolResult:
    _check_failsafe()
    x, y = _target(args)
    button = args.get("button", "left")
    if button not in ("left", "right", "double"):
        raise ToolError("'button' : left, right ou double.")
    _dpi_aware()
    sx, sy = round(x / _shot["scale"]), round(y / _shot["scale"])
    _show_ring(sx, sy)
    await _glide(sx, sy)
    _check_failsafe()  # l'utilisateur a pu saisir la souris pendant le trajet
    down, up = (0x8, 0x10) if button == "right" else (0x2, 0x4)
    for _ in range(2 if button == "double" else 1):
        _send(_mouse(down), _mouse(up))
    _last.update(kind="click", refused=0)
    await asyncio.sleep(0.3)  # le cercle s'efface avant la capture
    return await _after()


async def type_text(args: dict) -> ToolResult:
    _check_failsafe()
    _mouse_first("type")
    text = str(args.get("text") or "")
    if not text or len(text) > 500:
        raise ToolError("'text' requis (500 caractères max).")
    _type(text)
    _last.update(kind="type")
    return await _after()


async def press(args: dict) -> ToolResult:
    _check_failsafe()
    _mouse_first("keyboard")
    names = [k.strip().lower() for k in str(args.get("keys") or "").split("+") if k.strip()]
    if not names or len(names) > 4 or any(n not in VK for n in names):
        raise ToolError("'keys' : touches séparées par +, ex. « ctrl+s », « enter », « alt+f4 ».")
    vks = [VK[n] for n in names]
    _send(*[_key(v) for v in vks], *[_key(v, up=True) for v in reversed(vks)])
    _last.update(kind="keyboard")
    return await _after()


async def scroll(args: dict) -> ToolResult:
    _check_failsafe()
    amount = args.get("amount", 5)
    if args.get("direction") not in ("up", "down") or not isinstance(amount, int) or not 1 <= amount <= 30:
        raise ToolError("'direction' : up ou down ; 'amount' : 1 à 30 crans.")
    _send(_mouse(0x800, 120 * amount * (1 if args["direction"] == "up" else -1)))
    _last.update(kind="scroll")
    return await _after()


# ---------- ouvrir une application : par la liste de Windows, jamais par le clavier ----------
_apps: dict = {"at": 0.0, "found": []}
APPS_FRESH = 300  # secondes : la liste des applications installées est relue au-delà
ALIASES = {  # noms courants (anglais, raccourcis) → nom affiché par Windows en français
    "notepad": "bloc-notes",
    "calc": "calculatrice",
    "calculator": "calculatrice",
    "explorer": "explorateur de fichiers",
    "file explorer": "explorateur de fichiers",
    "explorateur": "explorateur de fichiers",
    "settings": "paramètres",
    "terminal": "terminal",
    "cmd": "invite de commandes",
    "vscode": "visual studio code",
    "vs code": "visual studio code",
}


def _plain(text: str) -> str:
    """« Bloc-notes » et « bloc notes » : le même nom. Minuscules, sans accents, lettres et chiffres seulement."""
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if c.isalnum() and not unicodedata.combining(c))


def start_apps() -> list[tuple[str, str]]:
    """Applications du menu Démarrer : (nom affiché, identifiant), d'après Windows lui-même (Get-StartApps)."""
    if _apps["found"] and time.monotonic() - _apps["at"] < APPS_FRESH:
        return _apps["found"]
    command = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "Get-StartApps | ForEach-Object { $_.Name + [char]9 + $_.AppID }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, timeout=20, creationflags=0x08000000,  # CREATE_NO_WINDOW
        ).stdout.decode("utf-8", "replace")  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        name, _, app_id = line.lstrip("\ufeff").partition("\t")
        if name.strip() and app_id.strip():
            found.append((name.strip(), app_id.strip()))
    _apps.update(at=time.monotonic(), found=found)
    return found


def find_app(wanted: str, apps: list[tuple[str, str]]) -> tuple[str, str] | None:
    """L'application demandée : nom exact d'abord, puis nom qui commence par la demande, puis qui la contient ; à
    égalité, le nom le plus court (« Word » avant « WordPad », jamais « Désinstaller … »)."""
    key = _plain(ALIASES.get(wanted.strip().lower(), wanted))
    if len(key) < 2:
        return None
    named = sorted(((_plain(name), name, app_id) for name, app_id in apps), key=lambda x: len(x[0]))
    for match in (str.__eq__, str.startswith, str.__contains__):
        for plain, name, app_id in named:
            if plain and match(plain, key):
                return name, app_id
    return None


async def open_app(args: dict) -> ToolResult:
    _check_failsafe()
    name = str(args.get("name") or "").strip()
    if not name or len(name) > 60 or "\n" in name:
        raise ToolError("'name' requis : nom de l'application, ex. « bloc-notes » (60 caractères max).")
    apps = await asyncio.to_thread(start_apps)
    found = find_app(name, apps)
    if found is None:
        plain = {_plain(n): n for n, _ in apps}
        near = [plain[k] for k in difflib.get_close_matches(_plain(name), list(plain), n=5, cutoff=0.5)]
        hint = f" Noms proches : {', '.join(near)}." if near else ""
        raise ToolError(
            f"Aucune application installée ne s'appelle « {name} ».{hint} Rien n'a été tapé ni cliqué : donne son "
            "nom exact, ou clique son icône (barre des tâches, bureau) si elle est visible."
        )
    before = _user32.GetForegroundWindow()
    try:  # lancée par son identifiant, comme le fait le menu Démarrer : ni souris, ni clavier
        subprocess.Popen(  # noqa: ASYNC220 (rend la main aussitôt)
            ["explorer.exe", f"shell:AppsFolder\\{found[1]}"], creationflags=0x08000000
        )
    except OSError as e:
        raise ToolError(f"« {found[0]} » n'a pas pu être lancée : {e}") from None
    for _ in range(24):  # jusqu'à 6 s : la fenêtre de l'application passe au premier plan
        await asyncio.sleep(0.25)
        if _user32.GetForegroundWindow() != before:
            break
    await asyncio.sleep(0.6)  # elle finit de se dessiner
    _last.update(kind="command")
    result = await _after()
    result.text = f"« {found[0]} » lancée. {result.text}"
    return result


_REASON = {"reason": {"type": "string", "description": "Ce que tu vois sur la capture et pourquoi cette action"}}
_POINT = {
    "element": {"type": "integer", "description": "Numéro jaune de l'élément sur la capture (le plus fiable)"},
    "zoom": {"type": "integer", "description": "Sous-case de la dernière vue pc_zoom (plus précis)"},
    "cell": {"type": "string", "description": "Case de la grille, ex. « C4 »"},
    "x": {"type": "number"},
    "y": {"type": "number"},
}


def _action(name: str, description: str, props: dict, run, required: tuple = (), preview=_preview_screen) -> Tool:
    """Action sur le PC : validation, modèle local, et la raison exigée (le modèle regarde avant d'agir)."""
    schema = {"type": "object", "properties": {**_REASON, **props}, "required": ["reason", *required]}
    return Tool(name, description, schema, run, level="pc", confirm=True, local_only=True, preview=preview)


register(
    Tool(
        "pc_screenshot",
        "Capture l'écran du PC avec une grille de repérage (cases A1, B2…). À faire avant d'agir.",
        {"type": "object", "properties": {}},
        screenshot,
        level="pc",
        local_only=True,
        fresh=still_fresh,
    ),
    _action(
        "pc_open_url",
        "Ouvre une adresse web dans le navigateur de l'utilisateur, à l'écran (ex. https://mail.google.com).",
        {"url": {"type": "string"}},
        open_url,
        ("url",),
    ),
    _action(
        "pc_open",
        "Ouvre une application installée, par son nom (ex. « bloc-notes », « calculatrice »), sans toucher au "
        "clavier ni à la souris.",
        {"name": {"type": "string"}},
        open_app,
        ("name",),
    ),
    _action(
        "pc_focus",
        "Met au premier plan une fenêtre déjà ouverte, d'après une partie de son titre (ex. « Bloc-notes », « Edge »).",
        {"title": {"type": "string"}},
        focus,
        ("title",),
    ),
    _action(
        "pc_click",
        "Clique avec la souris. Le plus fiable : 'element' = numéro jaune d'un élément de la liste jointe à la "
        "capture. Sinon case de la grille ('cell', ex. « C4 »), sous-case d'une vue pc_zoom ('zoom') ou x/y.",
        {**_POINT, "button": {"type": "string", "enum": ["left", "right", "double"]}},
        click,
        preview=_preview_point,
    ),
    Tool(
        "pc_zoom",
        "Agrandit la zone autour d'une case pour viser précisément un petit élément avant de cliquer.",
        {"type": "object", "properties": {"cell": {"type": "string"}}, "required": ["cell"]},
        zoom,
        level="pc",
        local_only=True,
    ),
    _action(
        "pc_type",
        "Tape un texte au clavier dans la fenêtre au premier plan.",
        {"text": {"type": "string"}},
        type_text,
        ("text",),
    ),
    _action(
        "pc_key",
        "Appuie sur une touche ou un raccourci, ex. « enter », « ctrl+s », « alt+f4 ».",
        {"keys": {"type": "string"}},
        press,
        ("keys",),
    ),
    _action(
        "pc_scroll",
        "Fait défiler la fenêtre sous la souris.",
        {"direction": {"type": "string", "enum": ["up", "down"]}, "amount": {"type": "integer"}},
        scroll,
        ("direction",),
    ),
)
