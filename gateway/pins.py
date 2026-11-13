"""Versions figées de la marketplace : lesquelles ont une version plus récente publiée depuis au moins 14 jours.

    python -m gateway.pins          tableau des versions figées, et celles à monter (avec empreinte et taille)
    python -m gateway.pins --liens  chaque téléchargement figé répond-il encore, empreinte enregistrée comprise
                                    (--complet : installateurs téléchargés et empreinte recalculée, ~1,5 Go)

Relevées dans le catalogue (serveurs MCP) et les recettes d'installation : paquets npm (pkg@1.2.3), paquets Python
(pkg==1.2.3, plafond pkg<=1.2.3), installateurs officiels de GitHub (empreinte SHA-256 et taille figées) et versions
GitHub nommées (image d'Open WebUI, LibreChat). Rien n'est modifié : la commande dit quoi changer. Comme Ollama, une
version n'est retenue que 14 jours après sa publication, sans préversion. GITHUB_TOKEN (facultatif) relève la limite
de l'API de GitHub (voir marketplace_export).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
import time
from dataclasses import dataclass, field

import httpx

from . import installers, marketplace
from .marketplace_export import github_headers

QUARANTINE_DAYS = 14
_NPM = re.compile(r"^(@?[a-z0-9][\w.-]*(?:/[\w.-]+)?)@(\d+\.\d+\.\d+)$")
_PY = re.compile(r"^([A-Za-z0-9][\w.-]*)(?:\[[\w,-]+\])?(==|<=)(\d+(?:\.\d+)*)$")  # extras : pkg[all]==1.2
_RELEASE = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/releases/download/([^/]+)/([^/]+)$")
_STABLE = re.compile(r"^v?\d+(\.\d+)*$")


@dataclass
class Pin:
    source: str  # npm | pypi | github
    name: str  # paquet, ou dépôt owner/repo
    version: str  # version figée (étiquette GitHub telle quelle)
    used_by: list[str] = field(default_factory=list)
    asset: str | None = None  # fichier d'une version GitHub (installateur figé)


def collect() -> list[Pin]:
    """Toutes les versions figées, une fois chacune, avec les éléments qui les utilisent."""
    found: dict[tuple, Pin] = {}

    def add(source, name, version, who, asset=None):
        pin = found.setdefault((source, name, version, asset), Pin(source, name, version, asset=asset))
        if who not in pin.used_by:
            pin.used_by.append(who)

    for s in marketplace.MCP_SERVERS:
        add("npm", s["package"], s["version"], f"mcp:{s['id']}")
    for key, r in installers.RECIPES.items():
        for argv in r.get("steps", ()):
            for a in argv:
                if m := _NPM.match(a):
                    add("npm", m.group(1), m.group(2), key)
                elif (m := _PY.match(a)) and argv[0] in (installers.PYTHON, "uv", installers.USER_PYTHON[0]):
                    add("pypi", m.group(1), m.group(3), key)
                elif m := _RELEASE.match(a):
                    add("github", m.group(1), m.group(2), key, asset=m.group(3))
    add("github", "LibreChat-AI/LibreChat", installers.LIBRECHAT_TAG, "app:librechat")
    return list(found.values())


def _key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.lstrip("v").split("."))


def newest(versions: dict[str, str], cutoff: str) -> tuple[str, str] | None:
    """(version, date) la plus haute parmi les versions stables publiées au plus tard à `cutoff` (AAAA-MM-JJ)."""
    ok = [(v, d) for v, d in versions.items() if _STABLE.match(v) and d[:10] <= cutoff]
    return max(ok, key=lambda x: _key(x[0])) if ok else None


async def _npm(client, name):
    data = (await client.get(f"https://registry.npmjs.org/{name}", timeout=30)).raise_for_status().json()
    deprecated = {v for v, x in data.get("versions", {}).items() if x.get("deprecated")}
    return {v: d for v, d in data.get("time", {}).items() if v not in ("created", "modified") and v not in deprecated}


async def _pypi(client, name):
    data = (await client.get(f"https://pypi.org/pypi/{name}/json", timeout=30)).raise_for_status().json()
    return {v: f[0]["upload_time"] for v, f in data.get("releases", {}).items() if f and not f[0].get("yanked")}


async def _github(client, repo):
    r = await client.get(
        f"https://api.github.com/repos/{repo}/releases?per_page=50", headers=github_headers(), timeout=30
    )
    rows = [x for x in r.raise_for_status().json() if not x.get("draft") and not x.get("prerelease")]
    return {x["tag_name"]: x["published_at"] or "" for x in rows}, {x["tag_name"]: x.get("assets") or [] for x in rows}


def _same_asset(asset: str, old: str, new: str, assets: list[dict]) -> dict | None:
    """Le fichier correspondant dans la nouvelle version : même nom, version remplacée."""
    want = {asset, asset.replace(old.lstrip("v"), new.lstrip("v"))}
    return next((a for a in assets if a.get("name") in want), None)


async def check(client: httpx.AsyncClient, today: str | None = None) -> list[dict]:
    """Chaque version figée : la plus récente retenable, et si elle est à monter."""
    today = today or time.strftime("%Y-%m-%d")
    cutoff = time.strftime(
        "%Y-%m-%d", time.gmtime(time.mktime(time.strptime(today, "%Y-%m-%d")) - QUARANTINE_DAYS * 86400)
    )
    gate, out = asyncio.Semaphore(4), []

    async def one(pin: Pin) -> None:
        row = {"pin": pin, "latest": None, "date": None, "update": False, "asset": None, "error": None}
        try:
            async with gate:
                if pin.source == "npm":
                    versions = await _npm(client, pin.name)
                elif pin.source == "pypi":
                    versions = await _pypi(client, pin.name)
                else:
                    versions, assets = await _github(client, pin.name)
            best = newest(versions, cutoff)
            if best:
                row["latest"], row["date"] = best[0], best[1][:10]
                gone = pin.source != "github" and pin.version not in versions  # dépréciée ou retirée : à quitter
                row["update"] = (
                    best[0] != pin.version
                    if gone or not _STABLE.match(pin.version)
                    else _key(best[0]) > _key(pin.version)
                )
                if row["update"] and pin.asset:
                    a = _same_asset(pin.asset, pin.version, best[0], assets.get(best[0], []))
                    row["asset"] = a and {
                        "url": a["browser_download_url"],
                        "sha256": (a.get("digest") or "").removeprefix("sha256:") or None,
                        "size": a.get("size"),
                    }
        except (httpx.HTTPError, ValueError, KeyError) as e:
            row["error"] = type(e).__name__
        out.append(row)

    await asyncio.gather(*(one(p) for p in collect()))
    return sorted(out, key=lambda r: (not r["update"], r["pin"].source, r["pin"].name))


# ---------- téléchargements figés : répondent-ils encore, et l'empreinte enregistrée est-elle la bonne ? ----------
def downloads() -> list[dict]:
    """Chaque téléchargement figé du catalogue : {kind, name, ...} (sans doublon)."""
    out, seen = [], set()

    def add(**d):
        key = tuple(sorted(d.items()))
        if key not in seen:
            seen.add(key)
            out.append(d)

    for p in collect():
        if p.source == "npm":
            add(kind="npm", name=p.name, version=p.version)
        elif p.source == "pypi":
            add(kind="pypi", name=p.name, version=p.version)
        elif not p.asset:
            add(kind="tag", name=p.name, version=p.version)
    for key, r in installers.RECIPES.items():
        for argv in r.get("steps", ()):
            if argv[:2] == (installers.PYTHON, installers.SETUP):
                add(kind="asset", name=key, url=argv[2], sha256=argv[3], size=int(argv[4]))
            elif argv[0] == "code" and argv[1] == "--install-extension":
                add(kind="vscode", name=argv[2])
            elif argv[0] == "winget":
                add(kind="winget", name=argv[argv.index("--id") + 1])
            elif argv[0] == installers.PYTHON and "webbrowser" in argv:
                add(kind="page", name=key, url=argv[-1])
    from . import librechat_setup as lc  # sa base MongoDB : archive officielle (empreinte publiée à côté, .sha256)

    add(kind="asset", name="app:librechat", url=lc.MONGO_URL, sha256=lc.MONGO_SHA256, size=lc.MONGO_SIZE)
    return out


async def _ghcr_exists(client, image: str) -> bool:
    repo, tag = image.removeprefix("ghcr.io/").rsplit(":", 1)
    token = (await client.get(f"https://ghcr.io/token?scope=repository:{repo}:pull", timeout=30)).json()["token"]
    accept = ", ".join(
        "application/vnd." + t
        for t in ("oci.image.index.v1+json", "docker.distribution.manifest.list.v2+json", "oci.image.manifest.v1+json")
    )
    r = await client.head(
        f"https://ghcr.io/v2/{repo}/manifests/{tag}", headers={"Authorization": f"Bearer {token}", "Accept": accept}
    )
    return r.status_code == 200


async def _asset(client, d: dict, full: bool) -> str | None:
    """None : bon ; sinon ce qui ne va pas. Empreinte comparée à celle publiée par GitHub (ou recalculée : full)."""
    m = _RELEASE.match(d["url"])
    r = await client.head(d["url"], timeout=60)
    if r.status_code != 200:
        return f"lien mort ({r.status_code})"
    if int(r.headers.get("content-length") or -1) != d["size"]:
        return f"taille {r.headers.get('content-length')} au lieu de {d['size']}"
    if full:
        digest = hashlib.sha256()
        async with client.stream("GET", d["url"], timeout=600) as s:
            async for chunk in s.aiter_bytes(1 << 20):
                digest.update(chunk)
        return None if digest.hexdigest() == d["sha256"] else f"empreinte {digest.hexdigest()} (fichier recalculé)"
    if not m:  # hors GitHub (MongoDB) : empreinte publiée par l'éditeur dans <fichier>.sha256
        r = await client.get(d["url"] + ".sha256", timeout=30)
        published = r.text.split()[0].lower() if r.status_code == 200 and r.text.strip() else None
        if not published:
            return f"empreinte publiée illisible ({r.status_code})"
        return None if published == d["sha256"] else f"empreinte publiée {published} différente"
    rel = await client.get(
        f"https://api.github.com/repos/{m.group(1)}/releases/tags/{m.group(2)}", headers=github_headers(), timeout=30
    )
    if rel.status_code != 200:
        return f"version GitHub illisible ({rel.status_code}) : empreinte non comparée"
    published = next((a.get("digest") for a in rel.json().get("assets", []) if a.get("name") == m.group(3)), None)
    if not published:
        return "GitHub ne publie pas d'empreinte pour ce fichier : relancez avec --complet"
    return None if published == f"sha256:{d['sha256']}" else f"empreinte publiée {published} différente"


async def check_downloads(client: httpx.AsyncClient, full: bool = False) -> list[dict]:
    """Chaque téléchargement figé, avec `problem` (None : il répond et correspond)."""
    gate, out = asyncio.Semaphore(4), []

    async def one(d: dict) -> None:
        problem = None
        try:
            async with gate:
                k = d["kind"]
                if k == "npm":
                    r = await client.get(f"https://registry.npmjs.org/{d['name']}/{d['version']}", timeout=30)
                    problem = None if r.status_code == 200 else f"version absente de npm ({r.status_code})"
                    if not problem and r.json().get("deprecated"):
                        problem = f"version dépréciée : {r.json()['deprecated'][:120]}"
                elif k == "pypi":
                    r = await client.get(f"https://pypi.org/pypi/{d['name']}/{d['version']}/json", timeout=30)
                    problem = None if r.status_code == 200 else f"version absente de PyPI ({r.status_code})"
                    if not problem and any(f.get("yanked") for f in r.json().get("urls", [])):
                        problem = "version retirée (yanked) de PyPI"
                elif k == "tag":
                    r = await client.head(f"https://github.com/{d['name']}/releases/tag/{d['version']}", timeout=30)
                    problem = None if r.status_code == 200 else f"étiquette GitHub introuvable ({r.status_code})"
                elif k == "asset":
                    problem = await _asset(client, d, full)
                elif k == "image":
                    problem = None if await _ghcr_exists(client, d["name"]) else "image introuvable"
                elif k == "vscode":
                    r = await client.post(
                        "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery",
                        json={"filters": [{"criteria": [{"filterType": 7, "value": d["name"]}]}], "flags": 0},
                        headers={"Accept": "application/json;api-version=3.0-preview.1"},
                        timeout=30,
                    )
                    problem = None if r.json()["results"][0]["extensions"] else "extension absente de VS Code"
                elif k == "winget":
                    if not installers.program("winget"):
                        problem = "non vérifié (winget absent de ce système)"
                    else:
                        p = await asyncio.create_subprocess_exec(
                            installers.program("winget"), "show", "--id", d["name"], "--exact",
                            "--accept-source-agreements", "--disable-interactivity",
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        )  # fmt: skip
                        problem = None if await p.wait() == 0 else "paquet absent de winget"
                elif k == "page":
                    r = await client.get(d["url"], timeout=30)
                    problem = None if r.status_code == 200 else f"page introuvable ({r.status_code})"
        except (httpx.HTTPError, ValueError, KeyError, IndexError, OSError) as e:
            problem = f"injoignable ({type(e).__name__})"
        out.append({**d, "problem": problem})

    await asyncio.gather(*(one(d) for d in downloads()))
    return sorted(out, key=lambda r: (r["problem"] is None, r["kind"], r["name"]))


async def links(full: bool) -> int:
    async with httpx.AsyncClient(headers={"User-Agent": "LezardIA-pins"}, follow_redirects=True) as client:
        rows = await check_downloads(client, full)
    for r in rows:
        what = r.get("version") or r.get("url") or ""
        mark = "OK " if r["problem"] is None else "!! "
        print(f"{mark} {r['kind']:6} {r['name']:42} {what[:60]:60} {r['problem'] or ''}")
    bad = [r for r in rows if r["problem"] and not r["problem"].startswith("non vérifié")]
    print(f"\n{len(rows)} téléchargements figés, {len(bad)} en défaut.")
    return 1 if bad else 0


async def main() -> int:
    from . import marketplace_extra

    marketplace_extra.apply_source()  # les versions figées du supplément (catalogue/supplement.json) sont suivies aussi
    if "--liens" in sys.argv:
        return await links("--complet" in sys.argv)
    async with httpx.AsyncClient(headers={"User-Agent": "LezardIA-pins"}, follow_redirects=True) as client:
        rows = await check(client)
    for r in rows:
        p = r["pin"]
        state = (
            f"illisible ({r['error']})"
            if r["error"]
            else f"→ {r['latest']} (publiée le {r['date']})"
            if r["update"]
            else "à jour"
        )
        print(f"{p.source:6} {p.name:42} {p.version:12} {state:38} {', '.join(p.used_by)}")
        if r["update"] and p.asset:
            a = r["asset"]
            print(
                f"       {a['url']}\n       sha256 {a['sha256']}  taille {a['size']}"
                if a
                else "       fichier introuvable"
            )
    todo = sum(r["update"] for r in rows)
    print(f"\n{len(rows)} versions figées, {todo} à monter (publiées depuis au moins {QUARANTINE_DAYS} jours).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
