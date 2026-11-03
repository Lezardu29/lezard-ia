import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.documents import DocumentError, bm25_rank, chunk_document, select_context, split_text
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore


def make_pdf(pages: list[str]) -> bytes:
    """PDF minimal valide (Helvetica), une chaîne de texte par page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode('latin-1')}\nendstream")
        content_id = len(objs)
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        )
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{off:010d} 00000 n \n" for off in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


# ---------- découpage ----------


def test_text_chunks_respect_size_and_overlap():
    para = ("Le routage intelligent choisit le meilleur modèle. " * 30).strip()
    text = "\n\n".join(f"Section {i}. {para}" for i in range(6))
    chunks = split_text(text, 128, 16)
    assert len(chunks) > 3 and all(len(c) <= 128 * 4 for c in chunks)
    # chevauchement : le début d'un chunk reprend la fin du précédent
    assert any(chunks[i + 1][:40] in chunks[i] for i in range(len(chunks) - 1))


def test_pdf_chunks_keep_page_numbers():
    kind, _, chunks = chunk_document("rapport.pdf", make_pdf(["Budget annuel 2026", "Annexe technique"]))
    assert kind == "pdf"
    assert [(c.meta["page"], c.text) for c in chunks] == [(1, "Budget annuel 2026"), (2, "Annexe technique")]


def test_pdf_without_text_is_rejected():
    with pytest.raises(DocumentError) as e:
        chunk_document("scan.pdf", make_pdf([""]))
    assert e.value.status == 422


def test_csv_repeats_header_and_tracks_rows():
    rows = "\n".join(f"{i};produit {i};{i * 1.5}" for i in range(1, 301))
    kind, _, chunks = chunk_document("ventes.csv", f"id;nom;prix\n{rows}".encode(), size=128)
    assert kind == "csv" and len(chunks) > 1
    assert all(c.text.startswith("id;nom;prix\n") for c in chunks)
    assert chunks[0].meta["rows"][0] == 1 and chunks[-1].meta["rows"][1] == 300
    assert chunks[0].meta["columns"] == ["id", "nom", "prix"]
    # aucune ligne perdue ni dupliquée
    data_lines = [l for c in chunks for l in c.text.splitlines()[1:]]
    assert len(data_lines) == 300 and len(set(data_lines)) == 300


def test_code_splits_on_definitions():
    funcs = "\n\n".join(
        f"def f{i}(x):\n" + "\n".join(f"    y{j} = x * {j}" for j in range(40)) + "\n    return y0" for i in range(4)
    )
    src = f"import os\n\n@cache\n{funcs}\n\nclass A:\n    pass\n"
    kind, lang, chunks = chunk_document("module.py", src.encode(), size=256)
    assert (kind, lang) == ("code", "python") and len(chunks) >= 3
    for c in chunks[1:]:
        assert c.text.lstrip().startswith(("def ", "class ", "@cache"))
    assert "@cache\ndef f0" in "\n".join(c.text for c in chunks)  # décorateur collé à sa fonction
    assert chunks[0].meta["start_line"] == 1 and chunks[-1].meta["end_line"] == src.count("\n")


def test_huge_function_split_by_lines():
    src = "def big():\n" + "\n".join(f"    v{i} = {i}" for i in range(2000))
    _, _, chunks = chunk_document("big.py", src.encode(), size=128)
    assert len(chunks) > 5 and all(len(c.text) <= 128 * 4 for c in chunks)
    assert chunks[1].meta["start_line"] == chunks[0].meta["end_line"] + 1


def test_validation_errors():
    for args, status in [
        (("a.txt", b""), 400),
        (("a.bin", b"\x00\x01\x02"), 415),
        (("a.txt", b"x", 10), 400),
        (("a.txt", b"x", 256, 200), 400),
    ]:
        with pytest.raises(DocumentError) as e:
            chunk_document(*args)
        assert e.value.status == status


def test_cp1252_decoding():
    _, _, chunks = chunk_document("notes.txt", "Café élevé".encode("cp1252"))
    assert chunks[0].text == "Café élevé"


def test_bm25_and_context_selection():
    chunks = [
        {"index": i, "text": t, "tokens": 100, "doc_order": 0, "filename": "f", "meta": {}}
        for i, t in enumerate(
            [
                "les chats dorment",
                "la facturation est mensuelle",
                "le routage choisit un modèle",
                "météo ensoleillée",
            ]
        )
    ]
    assert bm25_rank("Comment marche la facturation ?", chunks)[0][1]["index"] == 1
    assert bm25_rank("modele routage", chunks)[0][1]["index"] == 2  # accents ignorés
    picked, truncated = select_context(chunks, "routage facturation", 200)
    assert truncated and [c["index"] for c in picked] == [1, 2]  # ordre du document
    assert select_context(chunks, "x", 1000) == (chunks, False)


# ---------- API ----------


class Upstream:
    def __init__(self):
        self.bodies = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
        self.bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )


@pytest.fixture
def api():
    up = Upstream()
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "free"),
        store=DocumentStore(":memory:"),
    )
    with TestClient(app) as c:
        yield c, up


ALICE = {"Authorization": "Bearer alice"}
BOB = {"Authorization": "Bearer bob"}


def test_upload_multipart_and_json(api):
    c, _ = api
    r = c.post(
        "/v1/documents",
        headers=ALICE,
        files={"file": ("rapport.pdf", make_pdf(["Page un", "Page deux"]), "application/pdf")},
    )
    assert r.status_code == 201 and r.json()["kind"] == "pdf" and r.json()["n_chunks"] == 2
    r = c.post("/v1/documents", headers=ALICE, json={"filename": "app.py", "content": "def f():\n    pass\n"})
    assert r.json()["kind"] == "code" and r.json()["language"] == "python"
    b64 = base64.b64encode(b"a,b\n1,2\n").decode()
    r = c.post(
        "/v1/documents",
        headers=ALICE,
        json={"filename": "t.csv", "content_base64": b64, "chunk_size": 128, "chunk_overlap": 0},
    )
    assert r.json()["kind"] == "csv" and r.json()["chunk_size"] == 128
    assert len(c.get("/v1/documents", headers=ALICE).json()["data"]) == 3
    assert c.post("/v1/documents", headers=ALICE, json={"filename": "x.txt"}).status_code == 400
    assert (
        c.post("/v1/documents", headers=ALICE, json={"filename": "x.txt", "content_base64": "%%%"}).status_code == 400
    )


def test_documents_are_isolated_per_owner(api):
    c, _ = api
    doc = c.post("/v1/documents", headers=ALICE, json={"filename": "secret.txt", "content": "secret"}).json()
    assert c.get(f"/v1/documents/{doc['id']}", headers=BOB).status_code == 404
    assert c.get("/v1/documents", headers=BOB).json()["data"] == []
    assert c.delete(f"/v1/documents/{doc['id']}", headers=BOB).status_code == 404
    assert (
        c.post(
            "/v1/chat/completions",
            headers=BOB,
            json={
                "model": "ollama/llama3.1:8b",
                "documents": [doc["id"]],
                "messages": [{"role": "user", "content": "?"}],
            },
        ).status_code
        == 404
    )
    assert c.delete(f"/v1/documents/{doc['id']}", headers=ALICE).json()["deleted"]
    assert c.get(f"/v1/documents/{doc['id']}/chunks", headers=ALICE).status_code == 404


def test_chunks_and_search_endpoints(api):
    c, _ = api
    text = "\n\n".join(
        ["Le plan Pro coûte 3 euros par mois."]
        + ["Texte de remplissage sans intérêt. " * 40] * 4
        + ["Le quota gratuit offre 50000 tokens par mois."]
    )
    doc = c.post(
        "/v1/documents",
        headers=ALICE,
        json={"filename": "faq.md", "content": text, "chunk_size": 128, "chunk_overlap": 0},
    ).json()
    chunks = c.get(f"/v1/documents/{doc['id']}/chunks", headers=ALICE).json()["data"]
    assert len(chunks) == doc["n_chunks"] > 3
    hits = c.post(
        f"/v1/documents/{doc['id']}/search",
        headers=ALICE,
        json={"query": "combien de tokens dans le quota gratuit ?", "top_k": 2},
    ).json()["data"]
    assert "quota gratuit" in hits[0]["text"] and hits[0]["score"] > 0


def test_chat_injects_documents(api):
    c, up = api
    small = c.post(
        "/v1/documents", headers=ALICE, json={"filename": "a.txt", "content": "Paris est la capitale."}
    ).json()
    r = c.post(
        "/v1/chat/completions",
        headers=ALICE,
        json={
            "model": "ollama/llama3.1:8b",
            "documents": [small["id"]],
            "messages": [{"role": "system", "content": "Sois bref."}, {"role": "user", "content": "Capitale ?"}],
        },
    ).json()
    sent = up.bodies[-1]["messages"]
    assert [m["role"] for m in sent] == ["system", "system", "user"] and "documents" not in up.bodies[-1]
    assert "[a.txt · extrait 1]\nParis est la capitale." in sent[1]["content"]
    assert r["x_gateway"]["documents"]["truncated"] is False and r["x_gateway"]["documents"]["chunks_used"] == 1

    filler = "\n\n".join(f"Paragraphe {i} : contenu générique sans rapport. " * 12 for i in range(30))
    big = c.post(
        "/v1/documents",
        headers=ALICE,
        json={
            "filename": "big.md",
            "content": filler + "\n\nLe mot de passe wifi est TOURNESOL.",
            "chunk_size": 128,
            "chunk_overlap": 0,
        },
    ).json()
    r = c.post(
        "/v1/chat/completions",
        headers=ALICE,
        json={
            "model": "ollama/llama3.1:8b",
            "documents": [big["id"]],
            "documents_budget": 300,
            "messages": [{"role": "user", "content": "Quel est le mot de passe wifi ?"}],
        },
    ).json()
    d = r["x_gateway"]["documents"]
    assert d["truncated"] and d["tokens"] <= 300 and d["chunks_used"] < d["chunks_total"]
    assert "TOURNESOL" in up.bodies[-1]["messages"][0]["content"]
