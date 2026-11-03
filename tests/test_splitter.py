"""Découpage des messages trop longs pour les IA locales (13.4) : map-reduce."""

import asyncio
import json
import re

import httpx
import pytest
from dataclasses import replace
from fastapi.testclient import TestClient

from gateway import splitter
from gateway.classifier import classify
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.registry import ModelRegistry
from gateway.store import DocumentStore

MASTER_KEY = "gw-master-0123456789abcdefghijklmnop"
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
SENTENCE = re.compile(r"La phrase (\d+) raconte un détail précis du chapitre \d+\.")


@pytest.fixture(autouse=True)
def probe_on(monkeypatch):
    monkeypatch.setenv("LOCAL_CONTEXT_PROBE", "true")
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)


def text_of(sentences: int) -> str:
    paras = []
    for p in range(0, sentences, 5):
        paras.append(
            " ".join(
                f"La phrase {k} raconte un détail précis du chapitre {p // 5 + 1}."
                for k in range(p, min(p + 5, sentences))
            )
        )
    return "Résume ce texte en trois points.\n\n" + "\n\n".join(paras)


def tiny(window=2048):
    return replace(ModelRegistry().get("ollama", "tinyllama", "local"), context_window=window)


def body_of(text):
    return {"model": "auto", "messages": [{"role": "user", "content": text}]}


def three_parts_text() -> str:
    """Message qui demande exactement 3 appels pour une fenêtre de 2 048 tokens (≈ 2,5 fois la place utile)."""
    room = splitter.make_plan(body_of(text_of(400)), classify(body_of(text_of(400))), [tiny()]).chunk_tokens
    n = 1
    while len(text_of(n)) < room * 4 * 2.5:
        n += 1
    text = text_of(n)
    assert len(splitter.make_plan(body_of(text), classify(body_of(text)), [tiny()]).chunks) == 3
    return text


# ---------- découpage aux limites naturelles ----------


def test_no_sentence_cut_in_two():
    text = text_of(300).replace("\n\n", " ")  # un seul paragraphe : coupe entre les phrases
    chunks = splitter.split_message(text, 400)
    assert len(chunks) > 3
    seen = []
    for c in chunks:
        assert c.count("La phrase") == len(SENTENCE.findall(c)), c[-80:]  # aucune phrase incomplète
        seen += [int(k) for k in SENTENCE.findall(c)]
    assert seen == list(range(300))  # tout, dans l'ordre, sans doublon


def test_code_split_between_functions():
    funcs = "\n\n".join(
        f"def fonction_{k}(x):\n" + "".join(f"    y{i} = x + {i}\n" for i in range(12)) + "    return y0\n"
        for k in range(30)
    )
    text = f"Relis ce code :\n\n```python\n{funcs}\n```\n\nQue fait-il ?"
    chunks = splitter.split_message(text, 300)
    blocks = [c for c in chunks if "def fonction_" in c]
    assert len(blocks) > 2
    for c in blocks:
        assert c.startswith("```python\ndef fonction_") or c.startswith("Relis ce code"), c[:40]
        for k in re.findall(r"def fonction_(\d+)", c):  # chaque fonction entière dans son morceau
            assert f"def fonction_{k}(x):\n" in c and c.count("return y0") >= 1
        assert c.count("```") % 2 == 0  # balises refermées
    all_defs = [k for c in blocks for k in re.findall(r"def fonction_(\d+)", c)]
    assert all_defs == [str(k) for k in range(30)]


def test_markdown_heading_stays_with_its_paragraph():
    text = "\n\n".join(f"## Section {k}\n\n" + "Un paragraphe assez long pour remplir. " * 12 for k in range(20))
    for c in splitter.split_message(text, 300):
        assert not c.rstrip().splitlines()[-1].startswith("##")  # jamais un titre seul en fin de morceau


def test_excerpt_keeps_start_and_end():
    text = "Traduis en anglais le texte suivant.\n\n" + "Bla bla. " * 2000 + "\n\nMerci, garde le ton familier."
    ex = splitter.excerpt(text, 100)
    assert "Traduis en anglais" in ex and "garde le ton familier" in ex and len(ex) < 600


# ---------- faux serveurs locaux ----------


class Servers:
    """Ollama (tinyllama, fenêtre 2 048) et, au besoin, LM Studio (phi-mini, fenêtre 2 048 chargée)."""

    def __init__(self, fail_reduce=False):
        self.calls: list[tuple[str, dict]] = []
        self.inflight = self.max_inflight = 0
        self.fail_reduce = fail_reduce

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "ollama" and path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "tinyllama"}]})
        if host == "ollama" and path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if host == "ollama" and path == "/api/show":
            return httpx.Response(200, json={"modelfile": "...", "model_info": {"llama.context_length": 2048}})
        if host == "lmstudio" and path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "phi-mini"}]})
        if host == "lmstudio" and path == "/api/v0/models":
            return httpx.Response(200, json={"data": [{"id": "phi-mini", "loaded_context_length": 2048}]})
        if path.endswith("/chat/completions"):
            body = json.loads(request.content)
            self.calls.append((host, body))
            system = body["messages"][0]["content"] if body["messages"][0]["role"] == "system" else ""
            user = body["messages"][-1]["content"]
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            await asyncio.sleep(0.02)
            self.inflight -= 1
            if "Tu reçois la partie" in system:
                answer = f"Note : phrases {', '.join(SENTENCE.findall(user)[:2])}…"
            elif "Notes prises sur chaque partie" in user:
                if self.fail_reduce:
                    return httpx.Response(500, json={"error": {"message": "panne"}})
                answer = "Synthèse finale."
            else:
                answer = "Réponse directe."
            usage = {
                "prompt_tokens": sum(len(str(m["content"])) for m in body["messages"]) // 4,
                "completion_tokens": 10,
            }
            usage["total_tokens"] = usage["prompt_tokens"] + 10
            if body.get("stream"):
                chunks = [
                    {"choices": [{"index": 0, "delta": {"content": answer}}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": usage},
                ]
                return httpx.Response(
                    200, text="".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": answer}}
                    ],
                    "usage": usage,
                },
            )
        return httpx.Response(404)

    def maps(self):
        return [(h, b) for h, b in self.calls if "Tu reçois la partie" in b["messages"][0]["content"]]

    def reduces(self):
        return [(h, b) for h, b in self.calls if "Notes prises sur chaque partie" in b["messages"][-1]["content"]]


def make(servers, lmstudio=False):
    cfgs = [ProviderConfig("ollama", "openai", "http://ollama:11434/v1", "local")]
    if lmstudio:
        cfgs.append(ProviderConfig("lmstudio", "openai", "http://lmstudio:1234/v1", "local"))
    return create_app(
        cfgs,
        httpx.AsyncClient(transport=httpx.MockTransport(servers)),
        plans=PlanResolver({}, "free", master_key=MASTER_KEY),
        store=DocumentStore(":memory:"),
    )


def ask(c, text, model="auto", headers=MASTER, **kw):
    return c.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": model, "messages": [{"role": "user", "content": text}], **kw},
    )


# ---------- critère d'acceptation ----------


def test_three_windows_three_maps_one_synthesis():
    text, s = three_parts_text(), Servers()
    with TestClient(make(s)) as c:
        r = ask(c, text)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["choices"][0]["message"]["content"] == "Synthèse finale."
        assert len(s.maps()) == 3 and len(s.reduces()) == 1 and len(s.calls) == 4
        sentences = []
        for _, b in s.maps():  # aucune phrase coupée en deux
            chunk = b["messages"][-1]["content"]
            assert chunk.count("La phrase") == len(SENTENCE.findall(chunk))
            sentences += [int(k) for k in SENTENCE.findall(chunk)]
            assert "Résume ce texte en trois points" in b["messages"][0]["content"]  # la consigne suit
            assert b["max_tokens"] <= 512
        assert sentences == list(range(len(SENTENCE.findall(text))))
        reduce = s.reduces()[0][1]["messages"][-1]["content"]
        assert reduce.count("### Partie") == 3 and "Résume ce texte" in reduce
        meta = out["x_gateway"]
        assert meta["split"]["parts"] == 3 and meta["split"]["window"] == 2048 and meta["split"]["reduced"]
        assert [a["role"] for a in meta["split"]["agents"]] == ["map", "map", "map", "reduce"]
        assert "vue d'ensemble" in meta["split"]["notice"]
        assert "découpé en 3 parties" in meta["routing"]["reason"]
        assert out["usage"]["total_tokens"] == sum(
            a["prompt_tokens"] + a["completion_tokens"] for a in meta["split"]["agents"]
        )
        # /v1/route l'annonce sans appeler de modèle
        r = c.post("/v1/route", headers=MASTER, json=body_of(text))
        assert r.json()["split"]["parts"] == 3 and len(s.calls) == 4


def test_split_off_and_explicit_model():
    text, s = three_parts_text(), Servers()
    with TestClient(make(s)) as c:
        r = ask(c, text, split="off")
        assert r.status_code == 503 and not s.calls
        r = ask(c, text, model="ollama/tinyllama", split="off")
        assert r.status_code == 400 and "fenêtre de 2048 tokens" in r.json()["error"]["message"]
        r = ask(c, text, model="ollama/tinyllama")  # modèle explicite : découpé, sur ce modèle
        assert r.status_code == 200 and r.json()["x_gateway"]["split"]["workers"] == ["ollama/tinyllama"]
        assert ask(c, text, split="toujours").status_code == 400
        assert ask(c, "Bonjour").json()["x_gateway"].get("split") is None  # message court : normal
        r = ask(c, text, headers={**MASTER, "X-Gateway-Split": "off"})
        assert r.status_code == 503


def test_parallel_on_several_local_servers():
    text, s = three_parts_text(), Servers()
    with TestClient(make(s, lmstudio=True)) as c:
        r = ask(c, text)
        assert r.status_code == 200, r.text
        split = r.json()["x_gateway"]["split"]
        assert split["parallel"] and set(split["workers"]) == {"ollama/tinyllama", "lmstudio/phi-mini"}
        assert {h for h, _ in s.maps()} == {"ollama", "lmstudio"} and s.max_inflight >= 2
    s1 = Servers()
    with TestClient(make(s1)) as c:  # un seul serveur : à la suite
        ask(c, text)
        assert s1.max_inflight == 1


def test_every_call_counts_in_quotas_and_provider_limits():
    text, s = three_parts_text(), Servers()
    with TestClient(make(s)) as c:
        k = c.post("/v1/keys", headers=MASTER, json={"name": "k", "limits": {"tpd": 1_000_000}}).json()
        r = ask(c, text, headers={"Authorization": f"Bearer {k['key']}"})
        total = r.json()["usage"]["total_tokens"]
        usage = c.get("/v1/keys", headers=MASTER, params={"with_usage": "true"}).json()["data"][0]["usage"]
        assert usage["tpd"]["used"] == total
        # 3 appels par minute au plus : la synthèse (4e appel) est refusée, les notes sont rendues telles quelles
        assert c.put("/v1/admin/providers/ollama/rules", headers=MASTER, json={"rpm": 3}).status_code == 200
        r = ask(c, text)
        assert r.status_code == 200, r.text
        split = r.json()["x_gateway"]["split"]
        assert not split["reduced"] and split["agents"][-1]["ok"] is False
        assert "Partie 1" in r.json()["choices"][0]["message"]["content"]
        assert len(s.calls) == 7  # 4 + 3 : aucun appel au-delà de la limite


def test_follow_up_question_after_long_message():
    """Cas signalé : long texte, réponse, puis « traduis-le » — le long texte n'est plus le dernier message.
    Avant : « Message trop long… même découpé » (le long texte comptait comme historique)."""
    text, s = three_parts_text(), Servers()
    convo = [
        {"role": "user", "content": text},
        {"role": "assistant", "content": "Voici un résumé. " * 40},
        {"role": "user", "content": "Et maintenant, liste les numéros de chapitre cités."},
    ]
    with TestClient(make(s)) as c:
        r = c.post("/v1/chat/completions", headers=MASTER, json={"model": "auto", "messages": convo})
        assert r.status_code == 200, r.text
        assert len(s.maps()) == 3 and len(s.reduces()) == 1
        for _, b in s.maps():  # la vraie question guide chaque partie
            assert "liste les numéros de chapitre" in b["messages"][0]["content"]
        reduce = s.reduces()[0][1]["messages"]
        assert reduce[-1]["content"].startswith("Et maintenant, liste les numéros")
        assert "lu en 3 parties" in reduce[0]["content"] and len(reduce[0]["content"]) < 2000  # rappel, pas le texte
        assert sum(len(str(m["content"])) for m in reduce) // 4 < 2048


def test_long_conversation_drops_oldest_messages():
    """Conversation dont chaque message est court, mais trop longue au total pour 2 048 tokens : pas de découpage
    utile, les plus anciens échanges ne sont pas transmis."""
    s = Servers()
    convo = []
    for k in range(30):
        convo += [
            {"role": "user", "content": f"Question {k} : " + "parle-moi encore du chapitre. " * 8},
            {"role": "assistant", "content": f"Réponse {k} : " + "voici un détail du chapitre. " * 8},
        ]
    convo.append({"role": "user", "content": "Dernière question : quel chapitre préfères-tu ?"})
    with TestClient(make(s)) as c:
        r = c.post("/v1/chat/completions", headers=MASTER, json={"model": "auto", "messages": convo})
        assert r.status_code == 200, r.text
        split = r.json()["x_gateway"]["split"]
        assert split["parts"] == 1 and split["trimmed_messages"] > 0 and "plus anciens" in split["notice"]
        assert len(s.calls) == 1 and not s.maps()  # un seul appel, sans lecture par parties
        sent = s.calls[0][1]["messages"]
        assert sent[-1]["content"].startswith("Dernière question") and "Question 0 :" not in str(sent)
        assert sum(len(str(m["content"])) for m in sent) // 4 < 2048


def test_too_long_even_when_split(monkeypatch):
    text = three_parts_text()
    monkeypatch.setenv("SPLIT_MAX_PARTS", "2")
    with TestClient(make(Servers())) as c:
        r = ask(c, text)
        assert r.status_code == 400 and "SPLIT_MAX_PARTS" in r.json()["error"]["message"]


def test_reduce_failure_returns_notes():
    s = Servers(fail_reduce=True)
    with TestClient(make(s)) as c:
        r = ask(c, three_parts_text())
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"].startswith("La synthèse finale a échoué")
        assert r.json()["x_gateway"]["split"]["reduced"] is False


def test_streaming_progress_then_synthesis():
    s = Servers()
    with TestClient(make(s)) as c:
        with c.stream(
            "POST",
            "/v1/chat/completions",
            headers=MASTER,
            json={"model": "auto", "stream": True, "messages": [{"role": "user", "content": three_parts_text()}]},
        ) as r:
            assert r.status_code == 200
            events = [json.loads(l[6:]) for l in r.iter_lines() if l.startswith("data: {")]
        progress = [e["x_gateway_progress"]["split"] for e in events if "x_gateway_progress" in e]
        assert progress == [{"done": 1, "parts": 3}, {"done": 2, "parts": 3}, {"done": 3, "parts": 3}]
        text = "".join((ch.get("delta") or {}).get("content") or "" for e in events for ch in e.get("choices", []))
        assert text == "Synthèse finale."
        final = events[-1]["x_gateway"]
        assert final["split"]["parts"] == 3 and len(final["split"]["agents"]) == 4
