"""Modèles de Hugging Face dans la marketplace : recherche GGUF, versions utilisables par Ollama, installation."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import hf_models
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

MASTER, ACCOUNT = {"Authorization": "Bearer master"}, {"Authorization": "Bearer compte"}
GB = 10**9

REPO = {
    "id": "unsloth/Qwen3-8B-GGUF",
    "private": False,
    "gated": False,
    "downloads": 172094,
    "likes": 161,
    "pipeline_tag": "text-generation",
    "tags": ["gguf", "base_model:Qwen/Qwen3-8B", "base_model:quantized:Qwen/Qwen3-8B", "license:apache-2.0"],
    "cardData": {"license": "apache-2.0"},
    "gguf": {"total": 8_190_735_360, "context_length": 40960},
    "siblings": [
        {"rfilename": "README.md", "size": 20119},
        {"rfilename": "Qwen3-8B-Q4_K_M.gguf", "size": 5_027_784_512},
        {"rfilename": "Qwen3-8B-Q8_0.gguf", "size": 8_709_519_168},
        {"rfilename": "Qwen3-8B-UD-Q4_K_XL.gguf", "size": 5_135_722_304},
        {"rfilename": "Qwen3-8B-BF16.gguf", "size": 16_388_044_384},
        {"rfilename": "mmproj-F16.gguf", "size": 800_000_000},  # projecteur d'images : pas un modèle
        {"rfilename": "big/Qwen3-8B-Q2_K.gguf", "size": 3 * GB},  # sous-dossier : Ollama ne le prend pas par ce nom
        {"rfilename": "Qwen3-8B-F16-00001-of-00002.gguf", "size": 8 * GB},  # découpé en plusieurs fichiers
        {"rfilename": "Qwen3-8B-weird.gguf", "size": 4 * GB},  # quantification illisible
    ],
}


@pytest.fixture(autouse=True)
def empty_cache():
    hf_models._cache.clear()
    hf_models._sizes.clear()


def test_files_keep_one_ollama_usable_file_per_quantization():
    info = hf_models.parse_files(REPO)
    assert [f["quant"] for f in info["files"]] == ["Q4_K_M", "UD-Q4_K_XL", "Q8_0", "BF16"]  # du plus petit au plus gros
    assert info["files"][0]["tag"] == "hf.co/unsloth/Qwen3-8B-GGUF:Q4_K_M"
    assert info["params_b"] == 8.19 and info["vision"] is True
    assert info["license"]["commercial"] == "oui"


@pytest.mark.parametrize(
    "change", [{"gated": "auto"}, {"private": True}, {"tags": ["text-generation"]}, {"id": "../x"}]
)
def test_gated_private_or_non_gguf_repositories_are_refused(change):
    with pytest.raises(hf_models.HfError):
        hf_models.parse_files({**REPO, **change})
    assert hf_models.parse_search([{**REPO, **change}]) == []


@pytest.mark.parametrize(
    "name, commercial",
    [
        ("apache-2.0", "oui"),
        ("mit", "oui"),
        ("cc-by-nc-4.0", "non"),
        ("llama3.1", "conditions"),
        ("gemma", "conditions"),
        ("other", "conditions"),
        (None, "inconnu"),
        ("wtfpl-x", "inconnu"),
    ],
)
def test_licenses_say_what_they_allow(name, commercial):
    assert hf_models.license_of(name)["commercial"] == commercial


def test_search_rows_are_short_and_safe():
    rows = hf_models.parse_search([REPO, {"id": "a/b", "tags": ["gguf"], "private": True}, "pas un objet"])
    assert rows == [
        {
            "repo": "unsloth/Qwen3-8B-GGUF",
            "author": "unsloth",
            "downloads": 172094,
            "likes": 161,
            "task": "text-generation",
            "base_model": "Qwen/Qwen3-8B",
            "license": hf_models.license_of("apache-2.0"),
            "vision": False,
            "updated": None,
        }
    ]


class Upstream:
    def __init__(self):
        self.calls = []
        self.ollama_models = ["llama3.1:8b", "hf.co/unsloth/Qwen3-8B-GGUF:Q8_0"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        host, path = request.url.host, request.url.path
        if host == "huggingface.co":
            assert "authorization" not in request.headers  # jamais de clé envoyée à Hugging Face
            if path == "/api/models":
                return httpx.Response(200, json=[REPO])
            if path == "/api/models/unsloth/Qwen3-8B-GGUF":
                return httpx.Response(200, json=REPO)
            return httpx.Response(404)
        if host == "ollama":
            if path == "/api/pull":
                self.ollama_models.append(json.loads(request.content)["model"])
                return httpx.Response(200, content=b'{"status":"success"}')
            if path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": m} for m in self.ollama_models]})
        return httpx.Response(404)


def make(up):
    return create_app(
        [ProviderConfig("ollama", "openai", "http://ollama:11434/v1", "local")],
        httpx.AsyncClient(transport=httpx.MockTransport(up)),
        plans=PlanResolver({"compte": "free"}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )


def test_routes_search_list_versions_and_install(monkeypatch):
    up = Upstream()
    monkeypatch.setattr("gateway.api.admin.disk_info", lambda: {"free": 7 * GB, "path": "C:\\"})
    monkeypatch.setattr("gateway.api.admin.is_this_machine", lambda url: True)  # « ollama » : l'Ollama de ce PC
    with TestClient(make(up)) as c:
        assert c.get("/v1/admin/models/hf/search?q=qwen", headers=ACCOUNT).status_code == 403
        r = c.get("/v1/admin/models/hf/search?q=qwen3 8b", headers=MASTER)
        assert r.status_code == 200 and r.json()["models"][0]["repo"] == "unsloth/Qwen3-8B-GGUF"
        sent = [x for x in up.calls if x.url.host == "huggingface.co"][0]
        assert sent.url.params["search"] == "qwen3 8b" and sent.url.params["filter"] == "gguf"
        c.get("/v1/admin/models/hf/search?q=qwen3 8b", headers=MASTER)  # même recherche : réponse gardée
        assert len([x for x in up.calls if x.url.host == "huggingface.co"]) == 1
        assert c.get("/v1/admin/models/hf/search?q=<script>", headers=MASTER).status_code == 400
        assert c.get("/v1/admin/models/hf/files?repo=../../etc", headers=MASTER).status_code == 400
        assert c.get("/v1/admin/models/hf/files?repo=a/absent", headers=MASTER).status_code == 404

        info = c.get("/v1/admin/models/hf/files?repo=unsloth/Qwen3-8B-GGUF", headers=MASTER).json()
        rows = {f["quant"]: f for f in info["files"]}
        assert rows["Q8_0"]["installed"] is True and rows["Q4_K_M"]["installed"] is False
        assert rows["Q4_K_M"]["memory"] > rows["Q4_K_M"]["size"]  # fichier + cache de la conversation
        assert rows["Q4_K_M"]["fit"] in ("vram", "offload", "too_big") and info["ollama"] == "ollama"

        # l'espace disque exigé est la vraie taille du fichier, pas le minimum d'un modèle inconnu
        r = c.post("/v1/admin/ollama/pull", headers=MASTER, json={"model": rows["Q8_0"]["tag"]})
        assert r.status_code == 507 and "9.7 Go nécessaires" in r.json()["error"]["message"]
        with c.stream("POST", "/v1/admin/ollama/pull", headers=MASTER, json={"model": rows["Q4_K_M"]["tag"]}) as r:
            assert r.status_code == 200 and [json.loads(x) for x in r.iter_lines() if x][-1] == {"status": "done"}
        assert "hf.co/unsloth/Qwen3-8B-GGUF:Q4_K_M" in up.ollama_models


def test_the_marketplace_page_has_the_tab_and_never_injects_html():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "gateway" / "web" / "static" / "marketplace.js").read_text(
        encoding="utf-8"
    )
    assert '["hf", "Hugging Face"]' in js and "/v1/admin/models/hf/search?q=${encodeURIComponent(q)}" in js
    assert "innerHTML" not in js
