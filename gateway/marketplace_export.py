"""Copie publique de la marketplace pour le site lezard-ia.fr (Cloudflare Pages) : consultable même PC éteint.

    python -m gateway.marketplace_export            écrit site/marketplace/catalog.json et licenses.json

GITHUB_TOKEN (facultatif) : un jeton GitHub sans permission, pour relire les étoiles au-delà de 60 dépôts par heure.

Le catalogue est celui des visiteurs (/v1/marketplace), plus la licence de chaque modèle d'Ollama (taille « latest »,
lue dans le registre) et le texte des compétences. Les textes de licence, souvent partagés entre modèles, sont
rangés une seule fois dans licenses.json (chargé à la demande). À relancer avant un push pour rafraîchir le site.
Installer reste l'affaire du Lezard IA de chacun : la page du site y renvoie (adresse et clé saisies par le visiteur).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import httpx

from . import installers, marketplace, mcp_tools, ollama_library, skills

OUT = Path(__file__).resolve().parents[1] / "site" / "marketplace"
PARALLEL = 8
GITHUB_PARALLEL = 4


class _Memory:
    """Le magasin de réglages attendu par model_license, en mémoire : rien n'est écrit sur ce PC."""

    def __init__(self) -> None:
        self.data: dict = {}

    def get_setting(self, name: str) -> dict | None:
        return self.data.get(name)

    def set_setting(self, name: str, value: dict) -> None:
        self.data[name] = value


def github_headers() -> dict[str, str]:
    """Jeton GitHub facultatif (GITHUB_TOKEN, sans aucune permission) : 5 000 lectures par heure au lieu de 60.
    Envoyé à api.github.com seulement."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def refresh_stars(client: httpx.AsyncClient) -> dict:
    """Étoiles de chaque dépôt GitHub du catalogue, relevées maintenant ; un dépôt illisible garde son relevé."""
    entries = [*marketplace.COMPONENTS, *marketplace.MCP_SERVERS, *marketplace.APPS, *marketplace.AGENT_TOOLS]
    entries += skills.catalog()
    repos = sorted({r for r in map(marketplace.github_repo, entries) if r})
    old = marketplace.stars_snapshot().get("repos", {})
    found, read = dict(old), set()
    headers, gate = github_headers(), asyncio.Semaphore(GITHUB_PARALLEL)  # en rafale, GitHub refuse des lectures

    async def one(repo: str) -> None:
        try:
            async with gate:
                r = await client.get(f"https://api.github.com/repos/{repo}", headers=headers, timeout=20)
            r.raise_for_status()
            found[repo] = int(r.json()["stargazers_count"])
            read.add(repo)
        except (httpx.HTTPError, KeyError, ValueError) as e:
            limited = isinstance(e, httpx.HTTPStatusError) and e.response.headers.get("x-ratelimit-remaining") == "0"
            hint = " (limite de l'API atteinte : GITHUB_TOKEN la relève)" if limited and not headers else ""
            print(f"  {repo} : étoiles non relues, ancien relevé gardé{hint}")

    await asyncio.gather(*(one(r) for r in repos))
    snap = {"date": time.strftime("%Y-%m-%d"), "repos": {r: found[r] for r in repos if r in found}}
    marketplace.STARS_FILE.write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(read)} dépôts GitHub sur {len(repos)} relus" + ("" if headers else " (sans GITHUB_TOKEN)"))
    return snap


def _with_steps(entries, kind: str) -> list[dict]:
    out = []
    for e in entries:
        r = installers.RECIPES.get(f"{kind}:{e['id']}") or {}
        out.append(
            {
                **marketplace.with_stars(e),
                "steps": installers.display(f"{kind}:{e['id']}") if r.get("steps") else [],
                "after": r.get("after"),
                "size": r.get("size"),
                "where": r.get("where"),
                "deps": installers.possible_dependencies(f"{kind}:{e['id']}") if r else [],
            }
        )
    return out


def static_catalog() -> dict:
    folder = skills.PACKS_DIR
    notice = (folder / "NOTICE.md").read_text(encoding="utf-8")
    return {
        "components": [
            marketplace.with_stars({k: v for k, v in c.items() if k != "action"}) for c in marketplace.COMPONENTS
        ],
        "mcp": [
            {
                **marketplace.with_stars(s),
                "deps": installers.possible_dependencies(names=["npx"]),
                "tools": mcp_tools.tools_of(s["id"]),
            }
            for s in marketplace.MCP_SERVERS
        ],
        "stars_date": marketplace.stars_snapshot().get("date"),
        "apps": _with_steps(marketplace.APPS, "app"),
        "agent_tools": _with_steps(marketplace.AGENT_TOOLS, "tool"),
        "bundles": marketplace.public_bundles(),
        "skills": [
            {
                **marketplace.with_stars(s),
                "content": skills.load()[s["id"]].content,
                "license_text": (folder / "licenses" / skills.load()[s["id"]].license_file).read_text(encoding="utf-8"),
            }
            for s in skills.catalog()
        ],
        "skills_notice": notice,
        "skills_catalog_url": skills.CATALOG_URL,
        "web_search": marketplace.web_search_comparison(),
        "disclaimer": marketplace.DISCLAIMER,
    }


async def library_with_licenses(client: httpx.AsyncClient) -> tuple[list[dict], dict[str, str]]:
    rows = await ollama_library.fetch(client)
    store, gate, texts = _Memory(), asyncio.Semaphore(PARALLEL), {}

    async def one(m: dict) -> dict:
        async with gate:
            try:
                lic = await marketplace.model_license(client, store, m["name"], "latest")
            except marketplace.MarketplaceError:
                lic = None
        out = {
            "name": m["name"],
            "description": m["description"],
            "capabilities": m["capabilities"],
            "sizes": [
                {
                    "tag": f"{m['name']}:{x['label']}",
                    "params_b": x["params_b"],
                    "download": ollama_library.tag_size_gb(x["params_b"]) * 1e9,
                }
                for x in m["sizes"]
            ],
            "license": None,
        }
        if lic:
            ref = None
            if lic.get("text"):
                ref = hashlib.sha256(lic["text"].encode()).hexdigest()[:16]
                texts[ref] = lic["text"]
            out["license"] = {k: lic.get(k) for k in ("name", "commercial", "summary", "copyright", "source")}
            out["license"]["text_ref"] = ref
        return out

    return await asyncio.gather(*(one(m) for m in rows)), texts


async def main() -> int:
    async with httpx.AsyncClient(headers={"User-Agent": "LezardIA-marketplace-export"}) as client:
        await refresh_stars(client)
        models, texts = await library_with_licenses(client)
    catalog = {
        **static_catalog(),
        "library": {"models": models, "fetched_at": time.time()},
        "generated_at": time.strftime("%Y-%m-%d"),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (OUT / "licenses.json").write_text(json.dumps(texts, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    missing = sum(1 for m in models if not m["license"])
    print(f"{len(models)} modèles ({missing} sans licence lisible), {len(texts)} textes de licence -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
