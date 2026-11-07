"""Upload et découpage (chunking) de documents (tâche 3.1).

Formats : PDF, TXT (et tout texte), Markdown (en-tête front-matter), HTML (texte visible), DOCX, XLSX, CSV/TSV,
code source. Chaque format a sa stratégie de découpage :
- texte / PDF : découpage récursif (paragraphes -> lignes -> phrases -> mots) avec chevauchement,
                sans traverser les pages (métadonnée `page`)
- Markdown / HTML / DOCX (17.2) : comme le texte, en suivant les titres (métadonnée `section`) ; front-matter du
                Markdown à part (métadonnée `front_matter`)
- CSV         : groupes de lignes, l'en-tête est répété dans chaque chunk (métadonnée `rows`)
- XLSX (17.2) : chaque feuille découpée comme un CSV (métadonnées `sheet`, `rows`)
- code        : coupe aux définitions de premier niveau (fonctions, classes…), puis regroupe
                les petits blocs (métadonnées `start_line` / `end_line`)
Tailles exprimées en tokens (estimation ~4 caractères / token, comme le classifieur).
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .classifier import CHARS_PER_TOKEN, estimate_tokens

DEFAULT_CHUNK_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 64
MIN_CHUNK_TOKENS, MAX_CHUNK_TOKENS = 64, 8192

CODE_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "bash",
    ".ps1": "powershell",
    ".sql": "sql",
    ".lua": "lua",
    ".r": "r",
    ".dart": "dart",
    ".vue": "vue",
    ".svelte": "svelte",
}
CSV_EXT = {".csv": ",", ".tsv": "\t"}
MARKDOWN_EXT = {".md", ".markdown", ".mdx"}
HTML_EXT = {".html", ".htm", ".xhtml"}
LEGACY_OFFICE = {".doc": "DOCX", ".xls": "XLSX", ".ppt": "PDF", ".odt": "DOCX", ".ods": "XLSX"}
TEXT_SEPARATORS = ["\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " "]
CODE_BOUNDARY = re.compile(
    r"^(async def |def |class |function |async function |export |const \w+ = (async )?\(|"
    r"func |fn |pub fn |pub struct |impl |struct |enum |interface |type |trait |module |"
    r"public |private |protected |internal |static |@\w+|#\[|CREATE |ALTER |INSERT )"
)
DECORATOR = re.compile(r"^(@\w+|#\[)")


class DocumentError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Chunk:
    index: int
    text: str
    tokens: int
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"index": self.index, "text": self.text, "tokens": self.tokens, "meta": self.meta}


def max_upload_bytes() -> int:
    return int(os.getenv("DOCUMENT_MAX_BYTES", str(20 * 1024 * 1024)))


# ---------- détection & décodage ----------


def detect_kind(filename: str, data: bytes) -> tuple[str, str | None]:
    """Retourne (kind, language) avec kind ∈ pdf | docx | xlsx | html | markdown | csv | code | text."""
    ext = os.path.splitext(filename.lower())[1]
    if ext == ".pdf" or data[:5] == b"%PDF-":
        return "pdf", None
    if ext in (".docx", ".xlsx"):
        if data[:4] != b"PK\x03\x04":
            raise DocumentError(422, f"'{filename}' n'est pas un fichier {ext[1:].upper()} valide.")
        return ext[1:], None
    if ext in LEGACY_OFFICE:
        raise DocumentError(415, f"Format {ext} non pris en charge : enregistrez-le en {LEGACY_OFFICE[ext]}.")
    if b"\x00" in data[:4096] and not data.startswith(UTF16_BOMS):  # UTF-16 (Bloc-notes « Unicode ») : texte
        raise DocumentError(
            415, f"'{filename}' semble binaire : formats acceptés PDF, DOCX, XLSX, texte, Markdown, HTML, CSV, code."
        )
    if ext in CSV_EXT:
        return "csv", None
    if ext in MARKDOWN_EXT:
        return "markdown", None
    if ext in HTML_EXT:
        return "html", None
    if ext in CODE_EXT:
        return "code", CODE_EXT[ext]
    return "text", None


UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def decode(data: bytes) -> str:
    if data.startswith(UTF16_BOMS):  # Windows : « Unicode » du Bloc-notes, exports de PowerShell
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


# ---------- découpage texte ----------


def _atoms(text: str, max_chars: int, seps: list[str]) -> list[str]:
    """Découpe récursivement en morceaux <= max_chars, en privilégiant les grands séparateurs."""
    if len(text) <= max_chars:
        return [text]
    if not seps:
        return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
    sep, rest = seps[0], seps[1:]
    parts = text.split(sep)
    if len(parts) == 1:
        return _atoms(text, max_chars, rest)
    out: list[str] = []
    for i, p in enumerate(parts):
        piece = p + (sep if i < len(parts) - 1 else "")
        if piece:
            out += _atoms(piece, max_chars, rest) if len(piece) > max_chars else [piece]
    return out


def _merge(atoms: list[str], max_chars: int, overlap_chars: int) -> list[str]:
    """Regroupe les atomes en chunks <= max_chars ; reprend la fin du chunk précédent (overlap)."""
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for a in atoms:
        if cur and cur_len + len(a) > max_chars:
            chunks.append("".join(cur))
            tail: list[str] = []
            tail_len = 0
            for t in reversed(cur):
                if tail_len + len(t) > overlap_chars:
                    break
                tail.insert(0, t)
                tail_len += len(t)
            if tail_len + len(a) > max_chars:
                tail, tail_len = [], 0
            cur, cur_len = tail, tail_len
        cur.append(a)
        cur_len += len(a)
    if cur:
        chunks.append("".join(cur))
    return [c.strip() for c in chunks if c.strip()]


def split_text(text: str, size: int, overlap: int) -> list[str]:
    max_chars, ov = size * CHARS_PER_TOKEN, overlap * CHARS_PER_TOKEN
    return _merge(_atoms(text, max_chars, TEXT_SEPARATORS), max_chars, ov)


# ---------- découpage par format ----------


def _chunks_text(segments: list[tuple[str, dict]], size: int, overlap: int) -> list[tuple[str, dict]]:
    out = []
    for text, meta in segments:
        out += [(t, dict(meta)) for t in split_text(text, size, overlap)]
    return out


def _chunks_csv(text: str, delimiter: str, size: int) -> list[tuple[str, dict]]:
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        pass
    return _chunks_rows(list(csv.reader(io.StringIO(text), delimiter=delimiter)), delimiter, size)


def _chunks_rows(rows: list[list[str]], delimiter: str, size: int) -> list[tuple[str, dict]]:
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []

    def render(r: list[str]) -> str:
        buf = io.StringIO()
        csv.writer(buf, delimiter=delimiter, lineterminator="\n").writerow(r)
        return buf.getvalue()

    header, data = render(rows[0]), rows[1:]
    max_chars = size * CHARS_PER_TOKEN
    out, cur, start = [], [], 1
    for i, r in enumerate(data, start=1):
        line = render(r)
        if cur and len(header) + sum(map(len, cur)) + len(line) > max_chars:
            out.append((header + "".join(cur), {"rows": [start, i - 1], "columns": rows[0]}))
            cur, start = [], i
        cur.append(line)
    if cur or not data:
        out.append((header + "".join(cur), {"rows": [start, len(data)], "columns": rows[0]}))
    return out


def _chunks_code(text: str, size: int, language: str | None) -> list[tuple[str, dict]]:
    lines = text.splitlines()
    starts = [0]
    for i, line in enumerate(lines):
        if i and CODE_BOUNDARY.match(line) and not DECORATOR.match(lines[i - 1]):
            starts.append(i)
    blocks = [(s, e) for s, e in zip(starts, starts[1:] + [len(lines)]) if e > s]
    max_chars = size * CHARS_PER_TOKEN

    def chars(s: int, e: int) -> int:
        return sum(len(l) + 1 for l in lines[s:e])

    # regroupe les blocs consécutifs ; découpe en fenêtres de lignes les blocs trop gros
    ranges: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = None
    for s, e in blocks:
        if chars(s, e) > max_chars:
            if cur:
                ranges.append(cur)
                cur = None
            ws = s
            while ws < e:
                we = ws
                while we < e and (we == ws or chars(ws, we + 1) <= max_chars):
                    we += 1
                ranges.append((ws, we))
                ws = we
        elif cur and chars(cur[0], e) <= max_chars:
            cur = (cur[0], e)
        else:
            if cur:
                ranges.append(cur)
            cur = (s, e)
    if cur:
        ranges.append(cur)
    return [
        ("\n".join(lines[s:e]).strip("\n"), {"start_line": s + 1, "end_line": e, "language": language})
        for s, e in ranges
        if "\n".join(lines[s:e]).strip()
    ]


def _pdf_pages(data: bytes) -> list[tuple[str, dict]]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "", {"page": i + 1}) for i, p in enumerate(reader.pages)]
    except (PdfReadError, ValueError, KeyError) as e:
        raise DocumentError(422, f"PDF illisible : {e}")
    pages = [(t, m) for t, m in pages if t.strip()]
    if not pages:
        raise DocumentError(422, "PDF sans texte extractible (document scanné ? l'OCR n'est pas supporté).")
    return pages


# ---------- formats bureautiques et web (17.2) : zipfile + xml.etree / html.parser, sans dépendance ----------

OOXML_MAX_BYTES = 200 * 1024 * 1024  # décompressé, toutes parties lues confondues (archive piégée « zip bomb »)
XLSX_MAX_COLUMNS = 512
XLSX_MAX_CELLS = 2_000_000
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
HEADING_STYLE = re.compile(r"^(heading|titre|title)\s*(\d*)$", re.I)
MD_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


class _Package:
    """Archive OOXML (DOCX, XLSX) lue avec un plafond de taille décompressée ; XML sans DTD ni entités."""

    def __init__(self, data: bytes, label: str):
        import zipfile

        self.label = label
        self.budget = OOXML_MAX_BYTES
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            self.names = set(self.zip.namelist())
        except (zipfile.BadZipFile, ValueError, OSError) as e:
            raise DocumentError(422, f"{label} illisible : {e}")

    def xml(self, name: str, required: bool = True):
        import xml.etree.ElementTree as ET

        if name not in self.names:
            if required:
                raise DocumentError(422, f"{self.label} illisible : partie {name} absente.")
            return None
        info = self.zip.getinfo(name)
        if info.file_size > self.budget:
            raise DocumentError(413, f"{self.label} trop volumineux une fois décompressé.")
        try:
            with self.zip.open(info) as f:
                raw = f.read(self.budget + 1)
        except Exception as e:  # zlib, CRC, chiffrement… : archive corrompue
            raise DocumentError(422, f"{self.label} illisible : {e}")
        if len(raw) > self.budget:
            raise DocumentError(413, f"{self.label} trop volumineux une fois décompressé.")
        self.budget -= len(raw)
        head = raw.replace(b"\x00", b"")  # XML en UTF-16 : la déclaration ne doit pas passer entre les octets nuls
        if b"<!DOCTYPE" in head or b"<!ENTITY" in head:  # jamais dans un OOXML légitime (expansion d'entités)
            raise DocumentError(422, f"{self.label} refusé : déclaration DTD inattendue.")
        try:
            return ET.fromstring(raw)
        except ET.ParseError as e:
            raise DocumentError(422, f"{self.label} illisible : {e}")


def _group(segments: list[tuple[str, dict]], size: int) -> list[tuple[str, dict]]:
    """Regroupe les petites sections consécutives (titres de DOCX, HTML, Markdown) jusqu'à la taille d'un chunk :
    pas de chunks minuscules ; le groupe garde le titre de sa première section."""
    max_chars = size * CHARS_PER_TOKEN
    out: list[tuple[str, dict]] = []
    for text, meta in segments:
        text = text.strip()
        if not text:
            continue
        if out and len(out[-1][0]) + len(text) + 2 <= max_chars and "front_matter" not in out[-1][1]:
            out[-1] = (out[-1][0] + "\n\n" + text, out[-1][1])
        else:
            out.append((text, dict(meta)))
    return out


def _docx_text(node) -> str:
    parts = []
    for e in node.iter():
        if e.tag == W + "t":
            parts.append(e.text or "")
        elif e.tag == W + "tab":
            parts.append("\t")
        elif e.tag in (W + "br", W + "cr"):
            parts.append("\n")
    return "".join(parts)


def _docx_sections(data: bytes) -> list[tuple[str, dict]]:
    """Paragraphes et tableaux (cellules séparées par « | ») dans l'ordre ; les styles de titre ouvrent une section."""
    root = _Package(data, "DOCX").xml("word/document.xml")
    body = root.find(W + "body")
    if body is None:
        raise DocumentError(422, "DOCX illisible : corps du document absent.")
    sections: list[tuple[list[str], dict]] = [([], {})]
    for block in body:
        if block.tag == W + "p":
            text = _docx_text(block).strip()
            style = block.find(f"{W}pPr/{W}pStyle")
            name = style.get(W + "val", "") if style is not None else ""
            if text and HEADING_STYLE.match(name):
                sections.append(([text], {"section": text[:200]}))
            elif text:
                sections[-1][0].append(text)
        elif block.tag == W + "tbl":
            for row in block.iter(W + "tr"):
                cells = [" ".join(_docx_text(c).split()) for c in row.iter(W + "tc")]
                if any(cells):
                    sections[-1][0].append(" | ".join(cells))
    return [("\n\n".join(lines), meta) for lines, meta in sections]


def _column(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return n - 1


def _xlsx_sheets(data: bytes) -> list[tuple[str, list[list[str]]]]:
    """Feuilles (nom, lignes) d'un classeur : valeurs telles qu'enregistrées (une formule donne son dernier résultat
    calculé, ou « =formule » s'il n'y en a pas) ; cellules au format date en dates ISO."""
    pkg = _Package(data, "XLSX")
    book = pkg.xml("xl/workbook.xml")
    pr = book.find(S + "workbookPr")
    date1904 = pr is not None and pr.get("date1904", "").lower() in ("1", "true")
    date_styles = _date_styles(pkg)
    rels = pkg.xml("xl/_rels/workbook.xml.rels", required=False)
    targets = {}
    for r in rels if rels is not None else []:
        if r.tag == PKG_REL + "Relationship":
            t = r.get("Target", "").lstrip("/")
            targets[r.get("Id")] = t if t.startswith("xl/") else "xl/" + t
    strings = []
    shared = pkg.xml("xl/sharedStrings.xml", required=False)
    for si in shared if shared is not None else []:
        t = si.find(S + "t")  # texte simple, ou suite de <r> mis en forme ; le phonétique (<rPh>) est ignoré
        strings.append(
            t.text or "" if t is not None else "".join(r.findtext(S + "t") or "" for r in si.findall(S + "r"))
        )
    sheets, cells = [], 0
    for i, sh in enumerate(book.iter(S + "sheet")):
        name = sh.get("name") or f"Feuille {i + 1}"
        part = targets.get(sh.get(REL + "id"), f"xl/worksheets/sheet{i + 1}.xml")
        root = pkg.xml(part, required=False)
        if root is None:
            continue
        rows: list[list[str]] = []
        for row in root.iter(S + "row"):
            values: dict[int, str] = {}
            for pos, c in enumerate(row.iter(S + "c")):
                col = _column(c.get("r", "")) if c.get("r") else pos
                if not 0 <= col < XLSX_MAX_COLUMNS:
                    continue
                cells += 1
                if cells > XLSX_MAX_CELLS:
                    raise DocumentError(413, f"Classeur trop grand (plus de {XLSX_MAX_CELLS} cellules).")
                kind, v = c.get("t", "n"), c.find(S + "v")
                if kind == "inlineStr":
                    text = "".join(t.text or "" for t in c.iter(S + "t"))
                elif v is None or v.text is None:
                    f = c.find(S + "f")
                    if f is None or not f.text:
                        continue
                    text = "=" + f.text
                elif kind == "s":
                    k = int(v.text) if v.text.strip().isdigit() else -1
                    text = strings[k] if 0 <= k < len(strings) else ""
                elif kind == "b":
                    text = "VRAI" if v.text.strip() == "1" else "FAUX"
                elif kind == "n" and c.get("s", "").isdigit() and int(c.get("s")) in date_styles:
                    text = _excel_date(v.text, date1904)
                else:
                    text = v.text
                if text.strip():
                    values[col] = text
            if values:
                rows.append([values.get(j, "") for j in range(max(values) + 1)])
        if rows:
            width = max(map(len, rows))
            sheets.append((name, [r + [""] * (width - len(r)) for r in rows]))
    return sheets


DATE_FORMAT_IDS = set(range(14, 23)) | {45, 46, 47}  # formats de date / heure intégrés à Excel


def _date_styles(pkg: _Package) -> set[int]:
    """Indices des styles de cellule (cellXfs) au format date ou heure, intégré ou personnalisé (« jj/mm/aaaa »…)."""
    root = pkg.xml("xl/styles.xml", required=False)
    if root is None:
        return set()
    custom = {}
    for f in root.iter(S + "numFmt"):
        code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", f.get("formatCode", "")).lower()  # textes, couleurs, échappés
        if f.get("numFmtId", "").isdigit():
            custom[int(f.get("numFmtId"))] = bool(re.search(r"[dmyhs]", code)) and "0" not in code
    xfs = root.find(S + "cellXfs")
    out = set()
    for i, xf in enumerate(xfs if xfs is not None else []):
        n = int(xf.get("numFmtId", "0")) if xf.get("numFmtId", "0").isdigit() else 0
        if n in DATE_FORMAT_IDS or custom.get(n):
            out.add(i)
    return out


def _excel_date(value: str, date1904: bool) -> str:
    from datetime import datetime, timedelta

    try:
        x = float(value)
    except ValueError:
        return value
    if not 0 <= x < 2_958_466:  # au-delà du 31/12/9999
        return value
    d = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    d += timedelta(seconds=round(x * 86400))
    if x == int(x):
        return d.date().isoformat()
    return d.strftime("%H:%M:%S") if x < 1 else d.isoformat(sep=" ", timespec="seconds")


def _xlsx_chunks(data: bytes, size: int) -> list[tuple[str, dict]]:
    out = []
    for name, rows in _xlsx_sheets(data):
        out += [(f"Feuille « {name} »\n{t}", {**m, "sheet": name}) for t, m in _chunks_rows(rows, ",", size)]
    if not out:
        raise DocumentError(422, "Classeur sans cellule remplie.")
    return out


class _HtmlText(HTMLParser):
    """Texte visible d'une page : sans scripts, styles ni balises ; titres h1–h3 en sections, cellules en « | »."""

    SKIP = {"script", "style", "noscript", "template", "svg", "head", "iframe", "object"}
    BLOCK = {
        "p",
        "div",
        "br",
        "li",
        "ul",
        "ol",
        "tr",
        "table",
        "section",
        "article",
        "header",
        "footer",
        "nav",
        "aside",
        "main",
        "blockquote",
        "pre",
        "hr",
        "dd",
        "dt",
        "dl",
        "figure",
        "figcaption",
        "form",
        "h4",
        "h5",
        "h6",
        "address",
        "details",
        "summary",
    }
    HEADINGS = {"h1", "h2", "h3"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections: list[tuple[list[str], dict]] = [([], {})]
        self.skip = 0
        self.title: list[str] | None = None
        self.page_title = ""
        self.heading: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.title = []
        elif tag in self.SKIP:
            self.skip += 1
        elif tag in self.HEADINGS:
            self.heading = []
        elif tag in self.BLOCK:
            self._text("\n")
        elif tag in ("td", "th"):
            self._text(" | ")

    def handle_endtag(self, tag):
        if tag == "title" and self.title is not None:
            self.page_title = " ".join("".join(self.title).split())
            self.title = None
        elif tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.HEADINGS and self.heading is not None:
            text = " ".join("".join(self.heading).split())
            self.heading = None
            if text:
                self.sections.append(([text, "\n"], {"section": text[:200]}))
        elif tag in self.BLOCK:
            self._text("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr"):
            self._text("\n")

    def handle_data(self, data):
        if self.title is not None:
            self.title.append(data)
        elif self.heading is not None and not self.skip:
            self.heading.append(data)
        else:
            self._text(data)

    def _text(self, data):
        if not self.skip:
            self.sections[-1][0].append(data)


def _html_sections(text: str) -> list[tuple[str, dict]]:
    p = _HtmlText()
    p.feed(text)
    p.close()
    out = []
    for parts, meta in p.sections:
        lines = (" ".join(line.split()) for line in "".join(parts).split("\n"))
        lines = [line.strip(" |") for line in lines]
        out.append(("\n".join(line for line in lines if line), meta))
    if p.page_title and not out[0][0]:
        out[0] = (p.page_title, {"section": p.page_title[:200]})
    elif p.page_title:
        out[0] = (p.page_title + "\n" + out[0][0], {"section": p.page_title[:200]})
    return out


def _front_matter(text: str) -> tuple[dict, str]:
    """En-tête YAML simple (--- … ---) d'un Markdown : clés « nom: valeur » et listes « - élément »."""
    m = re.match(r"\A---[ \t]*\n(.*?\n)?---[ \t]*(\n|\Z)", text, re.S)
    if not m:
        return {}, text
    fields: dict = {}
    key = None
    for line in (m.group(1) or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s*(.*)$|^-\s+(.*)$", line)
        kv = re.match(r"^([A-Za-z0-9_][\w .-]*?)\s*:\s*(.*)$", line)
        if item and key:
            value = (item.group(1) if item.group(1) is not None else item.group(2)).strip().strip("'\"")
            fields[key] = (fields[key] if isinstance(fields[key], list) else []) + [value]
        elif kv:
            key, value = kv.group(1).strip(), kv.group(2).strip()
            if value.startswith("[") and value.endswith("]"):
                fields[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
            else:
                fields[key] = value.strip("'\"")
    if len(fields) > 50 or not fields:  # pas un en-tête (un --- de séparation en tête de fichier) : texte intact
        return {}, text
    return fields, text[m.end() :]


def _markdown_sections(text: str) -> list[tuple[str, dict]]:
    fields, body = _front_matter(text)
    sections: list[tuple[list[str], dict]] = []
    if fields:
        shown = "\n".join(f"{k} : {', '.join(v) if isinstance(v, list) else v}" for k, v in fields.items())
        sections.append(([shown], {"front_matter": fields}))
    sections.append(([], {}))
    fenced = False
    for line in body.split("\n"):
        if FENCE.match(line):
            fenced = not fenced
        h = None if fenced else MD_HEADING.match(line)
        if h:
            sections.append(([line], {"section": h.group(2)[:200]}))
        else:
            sections[-1][0].append(line)
    return [("\n".join(lines), meta) for lines, meta in sections]


def chunk_document(
    filename: str, data: bytes, size: int = DEFAULT_CHUNK_TOKENS, overlap: int | None = None
) -> tuple[str, str | None, list[Chunk]]:
    if overlap is None:  # défaut proportionné à la taille demandée
        overlap = min(DEFAULT_OVERLAP_TOKENS, size // 8)
    if not data:
        raise DocumentError(400, "Fichier vide.")
    if len(data) > max_upload_bytes():
        raise DocumentError(413, f"Fichier trop volumineux (max {max_upload_bytes()} octets).")
    if not MIN_CHUNK_TOKENS <= size <= MAX_CHUNK_TOKENS:
        raise DocumentError(400, f"chunk_size doit être entre {MIN_CHUNK_TOKENS} et {MAX_CHUNK_TOKENS}.")
    if not 0 <= overlap < size // 2:
        raise DocumentError(400, "chunk_overlap doit être >= 0 et < chunk_size / 2.")

    kind, language = detect_kind(filename, data)
    if kind == "pdf":
        raw = _chunks_text(_pdf_pages(data), size, overlap)
    elif kind == "docx":
        raw = _chunks_text(_group(_docx_sections(data), size), size, overlap)
    elif kind == "xlsx":
        raw = _xlsx_chunks(data, size)
    else:
        text = decode(data).replace("\r\n", "\n")
        if kind == "csv":
            raw = _chunks_csv(text, CSV_EXT[os.path.splitext(filename.lower())[1]], size)
        elif kind == "code":
            raw = _chunks_code(text, size, language)
        elif kind == "markdown":
            raw = _chunks_text(_group(_markdown_sections(text), size), size, overlap)
        elif kind == "html":
            raw = _chunks_text(_group(_html_sections(text), size), size, overlap)
        else:
            raw = _chunks_text([(text, {})], size, overlap)
    chunks = [Chunk(i, t, estimate_tokens(t), m) for i, (t, m) in enumerate(raw)]
    if not chunks:
        raise DocumentError(422, "Aucun contenu textuel exploitable.")
    return kind, language, chunks


# ---------- recherche (BM25) et injection dans le contexte ----------

_WORD = re.compile(r"\w{2,}")


def _terms(text: str) -> list[str]:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return _WORD.findall(t)


# mots vides de la QUESTION (sans accents, comme _terms) : « où se trouve la … » ne doit pas faire remonter tous les
# passages contenant « la » ou « se » (17.1 : ils écraseraient, dans la fusion, un passage trouvé par le sens)
STOPWORDS = frozenset(
    """
au aux avec ce ces cet cette dans de des du elle en est et eux il ils je la le les leur lui ma mais me mes moi mon
ne nos notre nous on ou par pas pour qu que quel quelle quels qui sa se ses son sur ta te tes toi ton tu un une vos
votre vous comment quand combien est-ce ya cela ca sont etre avoir fait faut peut quoi
a an and are as at be by do does for from how in is it of on or that the this to was what when where which who why
will with you your
""".split()
)


def bm25_rank(query: str, chunks: list[dict], k1: float = 1.5, b: float = 0.75) -> list[tuple[float, dict]]:
    """Classe des chunks ({text, ...}) par pertinence lexicale avec la requête."""
    terms = set(_terms(query))
    q = terms - STOPWORDS or terms  # question faite seulement de mots vides : on les garde
    docs = [_terms(c["text"]) for c in chunks]
    if not q or not docs:
        return [(0.0, c) for c in chunks]
    n = len(docs)
    avg = sum(map(len, docs)) / n or 1
    df = {t: sum(1 for d in docs if t in d) for t in q}
    scored = []
    for c, d in zip(chunks, docs):
        score = 0.0
        for t in q:
            tf = d.count(t)
            if tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(d) / avg))
        scored.append((score, c))
    return sorted(scored, key=lambda x: -x[0])


def _label(c: dict) -> str:
    m = c.get("meta") or {}
    if "page" in m:
        loc = f"p. {m['page']}"
    elif "start_line" in m:
        loc = f"lignes {m['start_line']}-{m['end_line']}"
    elif "sheet" in m and m["rows"][1] < m["rows"][0]:
        loc = f"feuille {m['sheet']}"
    elif "sheet" in m:
        loc = f"feuille {m['sheet']} · lignes de données {m['rows'][0]}-{m['rows'][1]}"
    elif "rows" in m:
        loc = f"lignes de données {m['rows'][0]}-{m['rows'][1]}"
    elif "section" in m:
        loc = f"§ {m['section'][:80]}"
    elif "front_matter" in m:
        loc = "en-tête"
    else:
        loc = f"extrait {c['index'] + 1}"
    return f"[{c['filename']} · {loc}]"


def fits(chunks: list[dict], budget: int) -> bool:
    return sum(c["tokens"] for c in chunks) <= budget


def hybrid_order(query: str, chunks: list[dict], semantic: list[int] | None = None) -> list[dict]:
    """Classement BM25, fusionné (RRF, 17.1) avec l'ordre par le sens quand il est connu (positions dans
    `chunks`). Les passages sans aucun signal gardent l'ordre BM25, en fin de liste."""
    from .vectors import rrf

    ranked = bm25_rank(query, chunks)
    if not semantic:
        return [c for _, c in ranked]
    pos = {id(c): i for i, c in enumerate(chunks)}
    lexical = [pos[id(c)] for s, c in ranked if s > 0]
    order = rrf(lexical, semantic)
    seen = set(order)
    rest = [pos[id(c)] for _, c in ranked if pos[id(c)] not in seen]
    return [chunks[i] for i in order + rest]


def select_context(
    chunks: list[dict], query: str, budget: int, semantic: list[int] | None = None
) -> tuple[list[dict], bool]:
    """Tout le contenu s'il tient dans le budget, sinon les chunks les plus pertinents (BM25, ou hybride si
    `semantic` est fourni). Retourne (chunks dans l'ordre du document, tronqué?)."""
    if fits(chunks, budget):
        return chunks, False
    picked, used = [], 0
    for c in hybrid_order(query, chunks, semantic):
        if used + c["tokens"] > budget:
            continue
        picked.append(c)
        used += c["tokens"]
    picked.sort(key=lambda c: (c["doc_order"], c["index"]))
    return picked, True


def context_message(chunks: list[dict]) -> str:
    body = "\n\n".join(f"{_label(c)}\n{c['text']}" for c in chunks)
    return (
        "Documents fournis par l'utilisateur (extraits). Appuie-toi dessus et cite la source "
        "entre crochets quand tu les utilises.\n\n" + body
    )
