"""Guides « brancher un outil » (16.4) : chaque exemple pointe sur une route qui existe, avec un modèle servi."""

import json
import re
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

STATIC = Path(__file__).resolve().parent.parent / "gateway" / "web" / "static"
SOURCE = (STATIC / "integrations.js").read_text(encoding="utf-8")
MASTER = {"Authorization": "Bearer master"}
EXPECTED = {"openwebui", "continue", "cursor", "python", "javascript", "langchain", "curl", "openclaw"}
EXPECTED |= {
    "ccrouter",
    "claudecode",
    "cline",
    "kilocode",
    "anythingllm",
    "jan",
    "librechat",
    "aider",
    "opencode",
    "goose",
    "chatbox",
    "qwencode",
    "zed",
    "cherrystudio",
}


def guides() -> dict[str, str]:
    """Bloc source de chaque guide, par identifiant."""
    body = SOURCE[SOURCE.index("export const GUIDES") : SOURCE.index("export function guideText")]
    parts = re.split(r'\n  \{\n(?:    //[^\n]*\n)?    id: "', body)[1:]  # commentaire facultatif avant l'id
    return {p.split('"', 1)[0]: p for p in parts}


def app_routes(app) -> set[tuple[str, str]]:
    out = set()
    stack = list(app.routes)
    while stack:
        r = stack.pop()
        stack += getattr(getattr(r, "original_router", None), "routes", []) or []
        for m in getattr(r, "methods", None) or ():
            out.add((m, getattr(r, "path", "")))
    return out


def make(up):
    return create_app(
        [ProviderConfig("ollama", "openai", "http://ollama:11434/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({}, "pro", master_key="master"),
        store=DocumentStore(":memory:"),
    )


def upstream(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}, {"id": "nomic-embed-text"}]})
    if path.endswith("/embeddings"):
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            },
        )
    return httpx.Response(
        200,
        json={
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 3, "total_tokens": 4, "completion_tokens": 1},
        },
    )


def test_every_tool_of_the_roadmap_has_a_guide_filled_with_url_and_key():
    found = guides()
    assert set(found) == EXPECTED
    for gid, block in found.items():
        code = block[block.index("code:") :]
        assert "${base}" in code and "${key}" in code, gid  # adresse et clé remplies
        assert re.search(r'routes: \[("/v1/[^"]+"(, )?)+\]', block), gid


def test_every_route_used_by_the_guides_exists():
    with TestClient(make(upstream)) as c:
        routes = app_routes(c.app)
    paths = set(re.findall(r'"(/v1/[a-z/_-]+)"', SOURCE)) | {
        "/v1" + p for p in re.findall(r"\$\{base\}(/[a-z/_-]+)", SOURCE)
    }
    assert {"/v1/models", "/v1/chat/completions", "/v1/embeddings"} <= paths
    method = {"/v1/models": "GET"}
    for p in paths:
        assert (method.get(p, "POST"), p) in routes, p


def test_models_named_in_the_guides_are_served():
    names = set(re.findall(r'model(?:"?\s*[:=]\s*"?)(auto(?:/[a-z]+)?)"?', SOURCE))
    assert {"auto", "auto/speed"} <= names
    with TestClient(make(upstream)) as c:
        assert c.get("/v1/models", headers=MASTER).status_code == 200
        for name in names:
            r = c.post(
                "/v1/chat/completions",
                headers=MASTER,
                json={"model": name, "messages": [{"role": "user", "content": "Bonjour"}]},
            )
            assert r.status_code == 200, (name, r.text)
        r = c.post("/v1/embeddings", headers=MASTER, json={"model": "auto", "input": "Bonjour"})
        assert r.status_code == 200, r.text  # exemple LangChain (OpenAIEmbeddings, model="auto")


def test_curl_example_body_is_valid():
    body = re.search(r"-d '(\{.*?\})'", SOURCE).group(1)
    with TestClient(make(upstream)) as c:
        r = c.post("/v1/chat/completions", headers=MASTER, json=json.loads(body))
        assert r.status_code == 200


def test_share_dialog_and_admin_section_use_the_guides():
    html = (STATIC.parent / "admin.html").read_text(encoding="utf-8")
    assert 'id="sd-guides"' in html and 'id="tools-section"' in html and 'id="tl-guides"' in html
    assert 'from "./integrations.js"' in (STATIC / "key-dialog.js").read_text(encoding="utf-8")
    assert 'from "./integrations.js"' in (STATIC / "tools-ui.js").read_text(encoding="utf-8")
