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
import html
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
            "sizes": [ollama_library.public_size(m["name"], x) for x in m["sizes"]],
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


PAGE_SECTIONS = (
    ("bundles", "Bundles", "Plusieurs éléments installés ensemble, en un clic."),
    ("components", "Outils de Lezard IA", "Fonctions à ajouter à Lezard IA : voix, recherche web, contrôle du PC…"),
    ("skills", "Compétences du chat", "Des façons de répondre à activer dans le chat."),
    ("mcp", "Serveurs MCP", "Des outils donnés à l'IA : fichiers, mémoire, navigateur…"),
    ("agent_tools", "Outils pour agents", "Outils pour Claude Code et les autres agents de programmation."),
    ("apps", "Applications", "Applications branchées sur les modèles de Lezard IA."),
)


def _license_name(item: dict) -> str:
    lic = item.get("license") or (item.get("licenses") or [None])[0]
    return (lic.get("name") if isinstance(lic, dict) else lic) or ""


def static_page(catalog: dict) -> str:
    """Le catalogue en texte simple, sur une page sans script : la marketplace du site charge ses fiches par script,
    les moteurs de recherche n'y lisent presque rien. Cette page leur donne chaque nom, sa description et sa licence."""
    esc = html.escape
    parts, count = [], 0
    for key, title, lead in PAGE_SECTIONS:
        rows = []
        for it in catalog.get(key) or []:
            lic, repo = _license_name(it), it.get("github")
            meta = [f"Licence : {esc(lic)}"] if lic else []
            if repo:
                meta.append(f'<a href="https://github.com/{esc(repo)}" rel="noopener nofollow">Code source</a>')
            rows.append(
                f'      <h3 id="{esc(key)}-{esc(str(it["id"]))}">{esc(it["name"])}</h3>\n'
                f"      <p>{esc(it.get('description') or '')}</p>\n"
                + (f'      <p class="updated">{" · ".join(meta)}</p>\n' if meta else "")
            )
        count += len(rows)
        parts.append(f'      <h2 id="{esc(key)}">{esc(title)}</h2>\n      <p>{esc(lead)}</p>\n' + "".join(rows))
    models = (catalog.get("library") or {}).get("models") or []
    rows = []
    for m in models:
        lic = _license_name(m)
        sizes = ", ".join(s["tag"].split(":", 1)[-1] for s in m.get("sizes") or [] if s.get("tag"))
        meta = " · ".join(
            x for x in (f"Tailles : {esc(sizes)}" if sizes else "", f"Licence : {esc(lic)}" if lic else "") if x
        )
        rows.append(
            f'        <li id="modele-{esc(m["name"])}"><strong>{esc(m["name"])}</strong> : {esc(m.get("description") or "")}'
            + (f' <span class="updated">({meta})</span>' if meta else "")
            + "</li>\n"
        )
    count += len(rows)
    parts.append(
        '      <h2 id="modeles">Modèles d\'IA</h2>\n'
        "      <p>Les modèles de la bibliothèque d'Ollama, installables en un clic depuis Lezard IA (descriptions "
        "d'Ollama, en anglais).</p>\n      <ul>\n" + "".join(rows) + "      </ul>\n"
    )
    summary = (
        " · ".join(f'<a href="#{k}">{esc(t)}</a>' for k, t, _ in PAGE_SECTIONS)
        + ' · <a href="#modeles">Modèles d\'IA</a>'
    )
    template = (Path(__file__).resolve().parent / "marketplace_page.html").read_text(encoding="utf-8")
    return (
        template.replace("{{COUNT}}", str(count))
        .replace("{{DATE}}", esc(catalog.get("generated_at") or time.strftime("%Y-%m-%d")))
        .replace("{{SUMMARY}}", summary)
        .replace("{{BODY}}", "".join(parts))
    )


def write_page(catalog: dict) -> None:
    (OUT / "catalogue.html").write_text(static_page(catalog), encoding="utf-8", newline="\n")


async def main() -> int:
    from . import marketplace_extra

    marketplace_extra.apply_source()  # fiches ajoutées depuis la dernière version (catalogue/supplement.json)
    if "--page" in sys.argv:  # la page en texte seulement, d'après le catalogue déjà écrit : aucun accès à Internet
        write_page(json.loads((OUT / "catalog.json").read_text(encoding="utf-8")))
        print(f"page du catalogue -> {OUT / 'catalogue.html'}")
        return 0
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
    write_page(catalog)
    missing = sum(1 for m in models if not m["license"])
    print(f"{len(models)} modèles ({missing} sans licence lisible), {len(texts)} textes de licence -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
