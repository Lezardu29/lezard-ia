"""Correctifs de l'analyse du dépôt : réflexion des modèles qui raisonnent, documents UTF-16, interface du chat."""

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import orchestrator as orch
from gateway.config import ProviderConfig
from gateway.documents import DocumentError, chunk_document
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.providers.base import strip_reasoning
from gateway.store import DocumentStore

ROOT = Path(__file__).resolve().parent.parent
MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
ACCOUNT_KEY = "ct-compte-0123456789abcdefghij"
ACCOUNT = {"Authorization": f"Bearer {ACCOUNT_KEY}"}


# ---------- réflexion « <think>… » (DeepSeek-R1, Qwen3 servis par LM Studio, llama.cpp, vLLM) ----------


def test_strip_reasoning():
    assert strip_reasoning("<think>\nje réfléchis {x}\n</think>\n\nRecette de crêpes") == "Recette de crêpes"
    assert strip_reasoning("  <THINK>a</THINK>b") == "b"
    assert strip_reasoning("<think>coupée par max_tokens") == ""  # réponse coupée : rien d'utilisable
    assert strip_reasoning("Réponse <think>citée</think> au milieu") == "Réponse <think>citée</think> au milieu"
    assert strip_reasoning(None) == ""


class Reasoner:
    """Faux serveur local dont le modèle raisonne à voix haute avant de répondre."""

    def __init__(self, answer: str):
        self.answer = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-r1:8b"}]})
        content = f"<think>\nL'utilisateur parle de {{crêpes}}. Je dois être bref.\n</think>\n\n{self.answer}"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )


def client(answer: str) -> TestClient:
    app = create_app(
        [ProviderConfig("lmstudio", "openai", "http://lmstudio/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(Reasoner(answer))),
        plans=PlanResolver({ACCOUNT_KEY: "pro"}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )
    return TestClient(app)


def test_conversation_title_without_the_model_reasoning():
    with client("Recette de crêpes") as c:
        msgs = [{"role": "user", "content": "Comment faire des crêpes ?"}, {"role": "assistant", "content": "Ainsi."}]
        conv = c.post("/v1/conversations", headers=ACCOUNT, json={"messages": msgs}).json()
        title = c.post(f"/v1/conversations/{conv['id']}/title", headers=ACCOUNT).json()["title"]
        assert title == "Recette de crêpes"  # avant : « <think> L'utilisateur parle de {crêpes}… »


def test_compact_summary_without_the_model_reasoning():
    with client("Résumé : la personne veut faire des crêpes.") as c:
        msgs = [{"role": "user", "content": "crêpes " * 50}, {"role": "assistant", "content": "farine " * 50}]
        r = c.post("/v1/compact", headers=ACCOUNT, json={"messages": msgs})
        assert r.status_code == 200, r.text
        summary = r.json()["summary"]
        assert "<think>" not in summary and summary.endswith("Résumé : la personne veut faire des crêpes.")


def test_multi_agent_plan_read_despite_the_planner_reasoning():
    plan = json.dumps({"parts": [{"role": "cuisinier", "question": "Quelle pâte ?"}]})
    resp = {"choices": [{"message": {"content": f"<think>Je pense à {{la pâte}} et {{la cuisson}}</think>{plan}"}}]}
    # avant : l'expression « {…} » allait de la première accolade de la réflexion à la dernière du plan : illisible
    assert orch.parse_plan(orch._text(resp), 4) == (["Quelle pâte ?"], ["cuisinier"])


# ---------- documents texte enregistrés en UTF-16 (Bloc-notes « Unicode », PowerShell) ----------


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be"])
def test_utf16_text_document_is_read_not_rejected_as_binary(encoding):
    data = "Café élevé\r\nDeuxième ligne".encode(encoding)
    if encoding == "utf-16-be":
        data = b"\xfe\xff" + data
    kind, _, chunks = chunk_document("notes.txt", data)
    assert kind == "text" and chunks[0].text == "Café élevé\nDeuxième ligne"


def test_binary_file_still_rejected():
    with pytest.raises(DocumentError) as e:
        chunk_document("a.bin", b"\x00\x01\x02")
    assert e.value.status == 415


# ---------- interface du chat ----------


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js absent")
def test_interface_modules_in_node():
    """Flux SSE, Markdown, export, téléchargement Ollama : tests/interface.test.mjs, sur le vrai code de la page."""
    r = subprocess.run(
        ["node", "--test", str(ROOT / "tests" / "interface.test.mjs")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=ROOT,
    )
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]


def test_chat_keys_and_buttons_do_not_cut_or_double_an_answer():
    chat = (ROOT / "gateway" / "web" / "static" / "chat.js").read_text(encoding="utf-8")
    # Entrée pendant une réponse : ne l'arrête plus (le bouton d'envoi et Échap le font)
    assert "if (!state.controller) submit(e);" in chat
    # « Réessayer » sous une erreur, suggestion de l'accueil : jamais une seconde réponse en parallèle
    assert chat.count("if (state.controller) return;                          // une autre réponse") == 1
    assert "if (!s || state.controller) return;" in chat
    assert 'import { sse } from "./sse.js";' in chat


def test_conversation_search_ignores_late_answers():
    conv = (ROOT / "gateway" / "web" / "static" / "conversations-ui.js").read_text(encoding="utf-8")
    assert "if (seq !== ui.seq || !r.ok) return;" in conv


# ---------- Windows sans base des fuseaux horaires (tzdata absent du Python de l'installateur) ----------


def test_date_question_answers_without_time_zone_database(monkeypatch):
    import zoneinfo

    from gateway.api import chat

    def missing(key):
        raise zoneinfo.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(chat, "ZoneInfo", missing)
    with client("Nous sommes lundi.") as c:
        r = c.post(
            "/v1/chat/completions",
            headers=ACCOUNT,
            json={"model": "auto", "messages": [{"role": "user", "content": "Quel jour serons-nous demain ?"}]},
        )
        assert r.status_code == 200, r.text  # avant : 500 sur tous les PC Windows


def test_windows_install_ships_the_time_zone_database():
    lock = (ROOT / "installer" / "requirements.lock").read_text(encoding="utf-8")
    assert "\ntzdata==" in lock and 'tzdata>=2024.1; sys_platform == "win32"' in (ROOT / "requirements.txt").read_text(
        encoding="utf-8"
    )


# ---------- base qui ne grossit plus sans fin : mesures, alertes déclenchées, notifications lues ----------


def test_history_tables_are_purged_and_unread_notifications_kept(monkeypatch):
    import time

    from gateway.main import history_days
    from gateway.metrics import COLUMNS

    store = DocumentStore(":memory:")
    now = time.time()
    old, recent = now - 800 * 86400, now - 86400
    store.insert_metrics([{**dict.fromkeys(COLUMNS), "ts": ts, "owner": "o", "status": "ok"} for ts in (old, recent)])
    store._db.execute("INSERT INTO alert_fired VALUES ('r', 'p', 50, ?)", (old,))
    for _ in range(2):
        store.add_notification("o", "info", "t", "m")
    store._db.execute("UPDATE notifications SET ts=? ", (old,))
    store._db.execute("UPDATE notifications SET read=1 WHERE id=(SELECT MIN(id) FROM notifications)")
    done = store.purge_history(now - 730 * 86400, now - 180 * 86400)
    assert done == {"metrics": 1, "alerts": 1, "notifications": 1}
    left = store._db.execute("SELECT read FROM notifications").fetchall()
    assert left == [(0,)]  # non lue : gardée, même ancienne
    assert store._db.execute("SELECT COUNT(*) FROM request_metrics").fetchone()[0] == 1

    monkeypatch.setenv("GATEWAY_METRICS_DAYS", "0")
    assert history_days("GATEWAY_METRICS_DAYS", 730) is None  # 0 : sans limite
    monkeypatch.setenv("GATEWAY_METRICS_DAYS", "n'importe quoi")
    assert history_days("GATEWAY_METRICS_DAYS", 730) == 730
