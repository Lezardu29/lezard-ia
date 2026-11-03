"""Formats d'upload supplémentaires (17.2) : DOCX, XLSX, HTML, Markdown avec front-matter."""

import io
import zipfile

import pytest

from gateway import documents
from gateway.documents import DocumentError, chunk_document, context_message

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
S_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R_NS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def zipped(parts: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            z.writestr(name, text)
    return buf.getvalue()


def para(text: str, style: str | None = None) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def docx(body: str) -> bytes:
    return zipped({"word/document.xml": f"<w:document {W_NS}><w:body>{body}</w:body></w:document>"})


def test_docx_paragraphs_headings_and_tables():
    table = (
        "<w:tbl><w:tr><w:tc>" + para("Plan") + "</w:tc><w:tc>" + para("Prix") + "</w:tc></w:tr>"
        "<w:tr><w:tc>" + para("Pro") + "</w:tc><w:tc>" + para("3 €") + "</w:tc></w:tr></w:tbl>"
    )
    body = (
        para("Introduction générale.")
        + para("Tarifs", "Heading1")
        + table
        + "<w:p><w:r><w:t>Ligne</w:t><w:tab/><w:t>tabulée</w:t></w:r></w:p>"
        + para("Annexe", "Titre2")
        + para("Texte de l'annexe.")
    )
    kind, _, chunks = chunk_document("offre.docx", docx(body))
    assert kind == "docx" and len(chunks) == 1  # petites sections regroupées
    text = chunks[0].text
    assert "Introduction générale." in text and "Plan | Prix\n\nPro | 3 €" in text and "Ligne\ttabulée" in text
    assert text.index("Tarifs") < text.index("Annexe") < text.index("Texte de l'annexe.")

    long = para("Préambule. " * 30) + para("Sécurité", "Heading1") + para("Le mot de passe est TOURNESOL. " * 30)
    _, _, chunks = chunk_document("long.docx", docx(long), size=128, overlap=0)
    hit = [c for c in chunks if "TOURNESOL" in c.text][0]
    assert hit.meta == {"section": "Sécurité"}
    label = context_message([{**hit.to_dict(), "filename": "long.docx"}])
    assert "[long.docx · § Sécurité]" in label


def sheet(rows: str) -> str:
    return f"<worksheet {S_NS}><sheetData>{rows}</sheetData></worksheet>"


def xlsx(sheets: list[tuple[str, str]], shared: list[str]) -> bytes:
    book = "".join(f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, (n, _) in enumerate(sheets))
    rels = "".join(
        f'<Relationship Id="rId{i + 1}" Target="worksheets/sheet{i + 1}.xml" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
        for i in range(len(sheets))
    )
    parts = {
        "xl/workbook.xml": f"<workbook {S_NS} {R_NS}><sheets>{book}</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": (
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>'
        ),
        "xl/sharedStrings.xml": f"<sst {S_NS}>"
        + "".join(
            f"<si><t>{s}</t></si>" if i else f"<si><r><t>{s[:2]}</t></r><r><t>{s[2:]}</t></r><rPh><t>x</t></rPh></si>"
            for i, s in enumerate(shared)
        )
        + "</sst>",
    }
    for i, (_, rows) in enumerate(sheets):
        parts[f"xl/worksheets/sheet{i + 1}.xml"] = sheet(rows)
    return zipped(parts)


def test_xlsx_sheets_as_csv():
    ventes = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="D1" t="s"><v>2</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>3</v></c><c r="B2"><v>12.5</v></c><c r="D2" t="b"><v>1</v></c></row>'
        '<row r="3"><c r="A3" t="inlineStr"><is><t>Lyon, centre</t></is></c><c r="B3"><f>B2*2</f><v>25</v></c></row>'
    )
    notes = '<row r="1"><c r="A1" t="str"><v>Remarque libre</v></c></row>'
    data = xlsx([("Ventes", ventes), ("Notes", notes), ("Vide", "")], ["Ville", "Montant", "Payé", "Brest"])
    kind, _, chunks = chunk_document("compta.xlsx", data)
    assert kind == "xlsx" and [c.meta["sheet"] for c in chunks] == ["Ventes", "Notes"]
    assert chunks[0].text == 'Feuille « Ventes »\nVille,Montant,,Payé\nBrest,12.5,,VRAI\n"Lyon, centre",25,,\n'
    assert chunks[0].meta["rows"] == [1, 2] and chunks[0].meta["columns"] == ["Ville", "Montant", "", "Payé"]
    label = context_message([{**chunks[0].to_dict(), "filename": "compta.xlsx"}])
    assert "[compta.xlsx · feuille Ventes · lignes de données 1-2]" in label

    big = "".join(f'<row r="{i}"><c r="A{i}"><v>{i}</v></c></row>' for i in range(1, 400))
    _, _, chunks = chunk_document("gros.xlsx", xlsx([("S", big)], []), size=64, overlap=0)
    assert len(chunks) > 1 and all(c.text.startswith("Feuille « S »\n1\n") for c in chunks)  # en-tête répété


def test_xlsx_dates_and_formulas():
    styles = (
        f'<styleSheet {S_NS}><numFmts count="2"><numFmt numFmtId="164" formatCode="dd/mm/yyyy;@"/>'
        '<numFmt numFmtId="165" formatCode="#,##0.00\\ &quot;€&quot;"/></numFmts>'
        '<cellXfs count="5"><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/><xf numFmtId="165"/>'
        '<xf numFmtId="22"/></cellXfs></styleSheet>'
    )
    rows = (
        '<row r="1"><c r="A1" s="1"><v>46024</v></c><c r="B1" s="2"><v>45658</v></c><c r="C1" s="3"><v>46024</v></c>'
        '<c r="D1" s="4"><v>46024.75</v></c><c r="E1" s="1"><v>0.5</v></c><c r="F1"><f>SUM(A1:C1)</f></c>'
        '<c r="G1" s="0"><v>46024</v></c></row>'
    )
    buf = io.BytesIO(xlsx([("Dates", rows)], []))
    with zipfile.ZipFile(buf, "a") as z:
        z.writestr("xl/styles.xml", styles)
    data = buf.getvalue()
    text = chunk_document("d.xlsx", data)[2][0].text
    assert text == "Feuille « Dates »\n2026-01-02,2025-01-01,46024,2026-01-02 18:00:00,12:00:00,=SUM(A1:C1),46024\n"
    label = context_message([{**chunk_document("d.xlsx", data)[2][0].to_dict(), "filename": "d.xlsx"}])
    assert "[d.xlsx · feuille Dates]" in label  # une seule ligne : pas de « lignes de données 1-0 »


def test_xlsx_limits_and_errors(monkeypatch):
    with pytest.raises(DocumentError) as e:
        chunk_document("vide.xlsx", xlsx([("A", "")], []))
    assert e.value.status == 422
    monkeypatch.setattr(documents, "XLSX_MAX_CELLS", 5)
    rows = "".join(f'<row r="{i}"><c r="A{i}"><v>{i}</v></c></row>' for i in range(1, 10))
    with pytest.raises(DocumentError) as e:
        chunk_document("trop.xlsx", xlsx([("A", rows)], []))
    assert e.value.status == 413
    far = '<row r="1"><c r="A1"><v>1</v></c><c r="XFD1"><v>2</v></c></row>'  # colonne 16384 : ignorée, pas de trou
    monkeypatch.setattr(documents, "XLSX_MAX_CELLS", 100)
    _, _, chunks = chunk_document("loin.xlsx", xlsx([("A", far)], []))
    assert chunks[0].text == "Feuille « A »\n1\n"


@pytest.mark.parametrize(
    "name,data,status",
    [
        ("faux.docx", b"pas un zip", 422),
        ("casse.docx", b"PK\x03\x04" + b"\x00" * 40, 422),
        ("sans.docx", zipped({"autre.xml": "<a/>"}), 422),
        ("mal.docx", zipped({"word/document.xml": "<w:document"}), 422),
        ("dtd.docx", zipped({"word/document.xml": '<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>'}), 422),
        (
            "dtd16.docx",
            zipped({"word/document.xml": '﻿<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>'.encode("utf-16-le")}),
            422,
        ),
        ("ancien.doc", b"\xd0\xcf\x11\xe0" + b"\x00" * 100, 415),
        ("ancien.xls", b"\xd0\xcf\x11\xe0" + b"\x00" * 100, 415),
        ("archive.zip", b"PK\x03\x04\x00\x00", 415),
    ],
)
def test_office_errors(name, data, status):
    with pytest.raises(DocumentError) as e:
        chunk_document(name, data)
    assert e.value.status == status, e.value.message
    if name.startswith("dtd"):  # refusé pour la DTD elle-même, pas par hasard (UTF-16 compris)
        assert "DTD" in e.value.message


def test_zip_bomb_refused(monkeypatch):
    monkeypatch.setattr(documents, "OOXML_MAX_BYTES", 10_000)
    data = docx(para("a" * 50_000))  # se compresse en quelques centaines d'octets
    assert len(data) < 2_000
    with pytest.raises(DocumentError) as e:
        chunk_document("bombe.docx", data)
    assert e.value.status == 413


def test_html_visible_text_only():
    page = """<!doctype html><html><head><title>Guide &amp; FAQ</title><style>p{color:red}</style>
    <script>var secret = "NE_PAS_INDEXER";</script></head><body>
    <nav>Accueil</nav><h1>Installation</h1><p>Lancez <b>lancer.ps1</b>&nbsp;puis ouvrez&#160;le&nbsp;navigateur.</p>
    <noscript>Activez JavaScript</noscript>
    <h2>Tarifs</h2><table><tr><th>Plan</th><th>Prix</th></tr><tr><td>Pro</td><td>3 &euro;</td></tr></table>
    <ul><li>un</li><li>deux</li></ul><svg><text>dessin</text></svg></body></html>"""
    kind, _, chunks = chunk_document("aide.html", page.encode())
    text = "\n".join(c.text for c in chunks)
    assert kind == "html" and "NE_PAS_INDEXER" not in text and "color" not in text and "dessin" not in text
    assert "Activez" not in text and "<" not in text
    assert text.startswith("Guide & FAQ") and "Lancez lancer.ps1 puis ouvrez le navigateur." in text
    assert "Plan | Prix\nPro | 3 €" in text and "un\ndeux" in text
    assert chunk_document("x.htm", b"<p>court</p>")[2][0].text == "court"

    filler = "<p>" + "Contenu générique. " * 40 + "</p>"
    _, _, chunks = chunk_document("long.html", (filler + "<h2>Wifi</h2><p>Code TOURNESOL</p>").encode(), 64, 0)
    assert [c.meta for c in chunks if "TOURNESOL" in c.text] == [{"section": "Wifi"}]


def test_markdown_front_matter_and_sections():
    md = """---
title: "Guide d'installation"
tags: [gateway, ollama]
auteurs:
  - Alice
  - Bob
# commentaire
---
# Démarrage

Lancez la gateway.

```python
# pas un titre
print("ok")
```
"""
    kind, _, chunks = chunk_document("guide.md", md.encode())
    assert kind == "markdown"
    head = chunks[0]
    assert head.meta == {
        "front_matter": {"title": "Guide d'installation", "tags": ["gateway", "ollama"], "auteurs": ["Alice", "Bob"]}
    }
    assert head.text == "title : Guide d'installation\ntags : gateway, ollama\nauteurs : Alice, Bob"
    assert "---" not in "".join(c.text for c in chunks)
    assert chunks[1].meta == {"section": "Démarrage"} and "# pas un titre" in chunks[1].text
    assert context_message([{**head.to_dict(), "filename": "guide.md"}]).count("[guide.md · en-tête]") == 1

    filler = "Remplissage. " * 60
    _, _, chunks = chunk_document("n.markdown", f"{filler}\n\n## Wifi\n\nCode TOURNESOL".encode(), 64, 0)
    assert [c.meta for c in chunks if "TOURNESOL" in c.text] == [{"section": "Wifi"}]


def test_markdown_without_front_matter_untouched():
    for md in ("---\n\nSéparateur en tête, pas d'en-tête.\n", "Texte simple.\n\n---\n\nSuite.", "---\n---\nVide"):
        _, _, chunks = chunk_document("a.md", md.encode())
        assert "front_matter" not in chunks[0].meta
    _, _, chunks = chunk_document("a.md", b"---\n---\nVide")
    assert chunks[0].text == "---\n---\nVide"


def test_upload_endpoint_accepts_docx(tmp_path):
    from fastapi.testclient import TestClient

    from gateway.main import create_app
    from gateway.plans import PlanResolver
    from gateway.store import DocumentStore

    key = "ct-alice-0123456789abcdefghij"
    app = create_app([], plans=PlanResolver({key: "pro"}, "free"), store=DocumentStore(":memory:"))
    with TestClient(app) as c:
        files = {"file": ("note.docx", docx(para("Bonjour DOCX")), "application/octet-stream")}
        r = c.post("/v1/documents", headers={"Authorization": f"Bearer {key}"}, files=files)
        assert r.status_code == 201 and r.json()["kind"] == "docx", r.text
        chunks = c.get(f"/v1/documents/{r.json()['id']}/chunks", headers={"Authorization": f"Bearer {key}"}).json()
        assert chunks["data"][0]["text"] == "Bonjour DOCX"
