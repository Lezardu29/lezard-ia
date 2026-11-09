"""Marketplace (page /marketplace) : catalogue, licences, installation.

- Visiteurs (sans clé) : /v1/marketplace (catalogue sans état de ce PC), licences des modèles et des compétences.
  Une licence pas encore lue coûte deux requêtes au registre d'Ollama : PUBLIC_FETCHES par minute au plus pour les
  visiteurs (les lectures en cache sont libres).
- Marketplace de lezard-ia.fr (SITE_ORIGINS, appel direct du navigateur du visiteur, CORS) : /v1/marketplace/installed
  dit ce qui est déjà installé sur ce PC, avec la clé maître, et ce que la marketplace locale en sait (matériel,
  modèles conseillés, bundles jugés) ; lecture seule (rien n'est modifié, aucun secret), donc sans la double
  authentification, qui reste exigée pour installer.
- Clé maître : /v1/admin/marketplace (état de ce PC), installation en un clic des composants, des outils pour agents
  et des applications (recettes figées de installers.py, une installation à la fois, journal dans
  data/logs/marketplace.log), et des serveurs MCP. Les modèles s'installent par /v1/admin/ollama/pull, les
  compétences par les réglages du chat. Les bundles y sont jugés pour ce PC (matériel, programmes trouvés) ; la page
  installe leurs éléments l'un après l'autre par ces mêmes routes.
"""

from __future__ import annotations

import asyncio
import logging
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .. import (
    app_connect,
    benchmark,
    chat_tools,
    detection,
    installers,
    marketplace,
    mcp,
    mcp_tools,
    model_catalog,
    ollama_library,
    services,
    skills,
    tools,
    updates,
)
from ..keys import VirtualKeyError
from ..plans import PlanError
from ..validation import json_object

log = logging.getLogger("gateway")
# plantage d'un installateur Windows (0xC0000005 : module System.dll de vieux installateurs NSIS, au hasard sous
# Windows 11), tel quel ou rapporté par winget : l'étape est relancée
INSTALLER_CRASH = {3221225477, -1073741819, 2316632070}
INSTALLER_RETRIES = 2
PACK_MODULES = {"voix": ("faster_whisper", "kokoro_onnx"), "multi": ("crewai",)}
PUBLIC_FETCHES = 20  # licences pas encore lues, par minute, pour l'ensemble des visiteurs
SITE_ORIGINS = ("https://lezard-ia.fr", "https://www.lezard-ia.fr")  # marketplace publique du site
INSTALLED_SETTING = "marketplace_installed"  # outils et applications installés depuis la page
STEP_TIMEOUT = 30 * 60
LOG_LINES = 40


def after_install(key: str, after: str | None) -> str | None:
    """Ce que la page dit après l'installation : la commande du chat d'abord (« /aider … »), puis le reste."""
    eid = key.split(":", 1)[1]
    if eid not in marketplace.CHAT:
        return after
    use = marketplace.chat_usage(eid)["use"]
    return f"Dans le chat de Lezard IA : {use}." + (f" {after}" if after else "")


def log_path() -> Path:
    return Path(os.getenv("MARKETPLACE_LOG", os.path.join("data", "logs", "marketplace.log")))


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    admin = shared.admin
    job: dict = {"id": None, "state": "idle", "code": None, "proc": None, "restart": False, "after": None}
    fetched: list[float] = []  # lectures du registre demandées par des visiteurs (dernière minute)

    def fail(e: marketplace.MarketplaceError):
        raise VirtualKeyError(e.status, e.message)

    def installed(cid: str) -> bool:
        if cid in PACK_MODULES:
            if updates.installer_mode():
                return (updates.ROOT / "packs" / cid / "pack.json").is_file()
            return all(importlib.util.find_spec(m) is not None for m in PACK_MODULES[cid])
        if cid == "web":
            return tools.web_enabled()
        if cid == "pc":
            return tools.pc_enabled()
        if cid == "ollama":
            return installers.program("ollama") is not None or (updates.ROOT / "packs" / "ollama.installed").is_file()
        return False

    def manual(c: dict) -> str:
        """Sans l'installation Windows (lancement depuis le code) : la commande à lancer soi-même."""
        if c["id"] == "voix":
            return "pip install -r requirements-local.txt, puis redémarrez Lezard IA."
        if c["id"] == "multi":
            return "pip install -r requirements-multi.txt, puis redémarrez Lezard IA."
        if c["id"] in ("web", "pc"):
            return f"Ajoutez TOOLS_{c['id'].upper()}=true dans .env, puis redémarrez Lezard IA."
        return "Installez Ollama depuis https://ollama.com/download."

    def busy() -> bool:
        return job["state"] == "running"

    def done_installs() -> dict:
        return holder["store"].get_setting(INSTALLED_SETTING) or {}

    async def present() -> set[str]:
        """Outils et applications sur ce PC : ce qu'on y lit (detection.py) ; à défaut, ce que la page a noté."""
        found, done = await asyncio.to_thread(detection.scan), done_installs()
        keys = [k for k in installers.RECIPES if k.startswith(("tool:", "app:"))]
        return {k for k in keys if (found[k] if found.get(k) is not None else k in done)}

    def one_click(key: str, here: set[str]) -> dict:
        """Ce que la page affiche pour un élément installable en un clic (vide : pas de recette)."""
        r = installers.RECIPES.get(key)
        if not r:
            return {}
        return {
            "one_click": True,
            "missing": installers.missing(key),
            "steps": installers.display(key),
            "after": after_install(key, r.get("after")),
            "size": r.get("size"),
            "where": r.get("where"),
            "deps": installers.dependencies(key),
            "installed": key in here,
            "uninstall": [installers.label(a) for a in installers.uninstall_steps(key) or ()],
            "uninstall_hint": installers.uninstall_hint(key),
            "connect": key.split(":", 1)[1] in app_connect.APPS and key.startswith("app:"),
        }

    def npx() -> str | None:
        """npx utilisable : Node.js trop ancien (moins de 24) compte comme absent, winget le met alors à jour."""
        found = installers.program("npx")  # comme partout ailleurs (npx.cmd sous Windows)
        return None if found and installers.too_old("npx") else found

    def public() -> dict:
        """Ce que la marketplace propose, sans rien de ce PC : la même chose pour tout le monde."""
        snap = marketplace.stars_snapshot()
        star = lambda e: marketplace.with_stars(e, snap)  # noqa: E731
        return {
            "components": [star({k: v for k, v in c.items() if k != "action"}) for c in marketplace.COMPONENTS],
            "mcp": [{**star(s), "tools": mcp_tools.tools_of(s["id"])} for s in marketplace.MCP_SERVERS],
            "apps": [star(a) for a in marketplace.APPS],
            "agent_tools": [star(t) for t in marketplace.AGENT_TOOLS],
            "bundles": marketplace.public_bundles(),
            "skills": [star(s) for s in skills.catalog()],
            "stars_date": snap.get("date"),
            "skills_catalog_url": skills.CATALOG_URL,
            "web_search": marketplace.web_search_comparison(),
            "disclaimer": marketplace.DISCLAIMER,
        }

    @router.get("/v1/marketplace")
    async def public_catalog():
        """Visite sans clé : le catalogue et la bibliothèque d'Ollama (tailles, capacités), sans état de ce PC."""
        try:
            lib = await ollama_library.cached(holder["store"], holder["gw"].client)
            models = [
                {
                    "name": m["name"],
                    "description": m["description"],
                    "capabilities": m["capabilities"],
                    "sizes": [ollama_library.public_size(m["name"], x) for x in m["sizes"]],
                }
                for m in lib["models"]
            ]
            library = {"models": models, "fetched_at": lib["fetched_at"], "stale": lib["stale"]}
        except (httpx.HTTPError, ValueError):
            library = None
        return {**public(), "library": library}

    # ----- marketplace de lezard-ia.fr : ce qui est déjà installé ici (appel d'une autre origine, CORS) -----
    def site_headers(request: Request) -> dict:
        origin = request.headers.get("origin")
        return (
            {"Access-Control-Allow-Origin": origin, "Vary": "Origin"} if origin in SITE_ORIGINS else {"Vary": "Origin"}
        )

    @router.options("/v1/marketplace/installed")
    async def installed_preflight(request: Request):
        headers = site_headers(request)
        if "Access-Control-Allow-Origin" in headers:
            headers.update(
                {
                    "Access-Control-Allow-Methods": "GET",
                    "Access-Control-Allow-Headers": "authorization",
                    "Access-Control-Max-Age": "600",
                }
            )
            if request.headers.get("access-control-request-private-network") == "true":
                headers["Access-Control-Allow-Private-Network"] = "true"  # site public -> ce PC (localhost)
        return Response(status_code=204, headers=headers)

    @router.get("/v1/marketplace/installed")
    async def installed_items(request: Request):
        """Déjà installé sur ce PC : modèles, compétences actives, composants, serveurs MCP, outils et applications."""
        headers = site_headers(request)
        try:
            ctx = shared.plan_ctx({}, request)
        except PlanError as e:
            return JSONResponse({"error": {"message": e.message}}, status_code=e.status, headers=headers)
        if not ctx.is_admin:
            return JSONResponse({"error": {"message": "Clé maître requise."}}, status_code=403, headers=headers)
        gw = holder["gw"]
        status = await gw.detect(max_age=60)
        models = sorted(
            {
                m
                for p in gw.providers.values()
                if p.cfg.kind == "openai"
                and (p.name.startswith("ollama") or ":11434" in p.cfg.base_url)
                and not p.cfg.owner
                and status.get(p.name, {}).get("online")
                for m in status[p.name].get("models", [])
            }
        )
        chat = holder["store"].get_pref(ctx.identity, "key:" + ctx.key["id"] if ctx.key else "account", "chat")
        here = await present()
        body = {
            "models": models,
            "skills": list(((chat or [{}])[0] or {}).get("skills") or []),
            "components": [c["id"] for c in marketplace.COMPONENTS if await asyncio.to_thread(installed, c["id"])],
            "mcp": sorted(marketplace.installed_mcp()),
            "tools": sorted(k.split(":", 1)[1] for k in here if k.startswith("tool:")),
            "apps": sorted(k.split(":", 1)[1] for k in here if k.startswith("app:")),
            "pc": await this_pc(),
        }
        return JSONResponse(body, headers=headers)

    async def this_pc() -> dict | None:
        """Ce que la marketplace locale sait de ce PC, pour celle du site : conseils selon le matériel, adéquation
        de chaque taille de modèle, bundles jugés, serveurs MCP connectés. Lu par le navigateur du propriétaire
        (clé maître), jamais par le serveur de lezard-ia.fr. None si le relevé échoue : le site s'en passe."""
        if not hasattr(shared, "recommendations"):
            return None
        try:
            reco = await shared.recommendations()
            try:
                lib = await shared.library()
                fits = {r["tag"]: [r["fit"], r["memory"], r["speed_label"]] for r in lib["models"]}
            except VirtualKeyError:  # ollama.com injoignable : les conseils restent, sans le détail de chaque taille
                fits = {}
            return {
                "recommendations": reco,
                "fits": fits,
                "bundles": await bundles(),
                "mcp_connected": sorted(s["name"] for s in mcp.overview() if s["ok"]),
            }
        except Exception:  # noqa: BLE001 - matériel ou Ollama illisible : la liste de ce qui est installé suffit
            log.warning("Marketplace du site : relevé de ce PC impossible", exc_info=True)
            return None

    @router.get("/v1/marketplace/models/{name}/license")
    async def public_model_license(name: str, tag: str = "latest"):
        """Licence d'un modèle pour un visiteur : la lecture en cache, ou une nouvelle lecture dans la limite."""
        cache = holder["store"].get_setting(marketplace.LICENSE_SETTING) or {}
        hit = cache.get(f"{name}:{tag}")
        if not (hit and time.time() - hit.get("fetched_at", 0) < marketplace.LICENSE_TTL):
            now = time.time()
            fetched[:] = [t for t in fetched if now - t < 60]
            if len(fetched) >= PUBLIC_FETCHES:
                raise VirtualKeyError(429, "Trop de licences demandées en même temps : réessayez dans une minute.")
            fetched.append(now)
        try:
            return await marketplace.model_license(holder["gw"].client, holder["store"], name, tag)
        except marketplace.MarketplaceError as e:
            fail(e)

    @router.get("/v1/marketplace/skills/{skill_id}")
    async def public_skill(skill_id: str):
        """Compétence pour un visiteur : son texte, sa licence et la notice d'origine (fichiers fournis)."""
        s = skills.load().get(skill_id)
        if not s:
            raise VirtualKeyError(404, f"Compétence '{skill_id}' introuvable.")
        folder = skills.PACKS_DIR
        return {
            **s.to_dict(),
            "content": s.content,
            "license_text": (folder / "licenses" / s.license_file).read_text(encoding="utf-8"),
            "notice": (folder / "NOTICE.md").read_text(encoding="utf-8"),
        }

    async def bundles() -> list[dict]:
        """Bundles jugés pour ce PC : mémoire et carte graphique (relevé partagé, gardé quelques secondes), meilleur
        modèle par usage (avec le test de performance s'il a été lancé), programmes trouvés (Claude Code…)."""
        gw, store = holder["gw"], holder["store"]
        try:
            hw = await gw.hardware.snapshot(list(gw.providers.values()), gw.client)
        except Exception:  # relevé impossible : jugés sans le matériel plutôt que pas de marketplace
            hw = {}
        reco = model_catalog.recommend(
            hw, bench=store.get_setting(benchmark.SETTING), measured=store.get_setting(benchmark.MODEL_SETTING)
        )
        return await asyncio.to_thread(marketplace.bundles_for, reco, hw, installers.program)

    @router.get("/v1/admin/marketplace")
    async def catalog(request: Request):
        """Le catalogue public, plus l'état de ce PC : installé, installable, serveurs MCP connectés."""
        admin(request)
        mode = updates.installer_mode()
        snap = marketplace.stars_snapshot()
        comps = []
        for c in marketplace.COMPONENTS:
            lacking = [] if mode else installers.missing(f"component:{c['id']}")
            comps.append(
                {
                    **marketplace.with_stars({k: v for k, v in c.items() if k != "action"}, snap),
                    "installed": await asyncio.to_thread(installed, c["id"]),
                    "installable": not lacking and not (c["id"] == "pc" and sys.platform != "win32"),
                    "missing": lacking,
                    "deps": [] if mode else installers.dependencies(f"component:{c['id']}"),
                    "where": "Dossier de Lezard IA, sur ce PC"
                    if mode
                    else installers.RECIPES.get(f"component:{c['id']}", {}).get("where"),
                    "manual": None if mode else manual(c),
                }
            )
        status = {s["name"]: s for s in mcp.overview()}
        node_deps = (
            [{"id": "node", **{k: v for k, v in installers.DEPENDENCIES["node"].items() if k != "provides"}}]
            if installers.dependency_steps(["npx"])
            else []
        )
        declared = marketplace.installed_mcp()
        here = await present()
        servers = [
            {
                **marketplace.with_stars(s, snap),
                "tools": mcp_tools.tools_of(s["id"]),
                "installed": s["id"] in declared,
                "deps": [] if npx() else node_deps,
                "connected": bool(status.get(s["id"], {}).get("ok")),
                "error": status.get(s["id"], {}).get("error"),
            }
            for s in marketplace.MCP_SERVERS
        ]
        return {
            **public(),
            "bundles": await bundles(),
            "components": comps,
            "mcp": servers,
            "agent_tools": [
                {**marketplace.with_stars(t, snap), **one_click(f"tool:{t['id']}", here)}
                for t in marketplace.AGENT_TOOLS
            ],
            "apps": [
                {**marketplace.with_stars(a, snap), **one_click(f"app:{a['id']}", here)} for a in marketplace.APPS
            ],
            "mcp_services": [{"id": k, "name": v["label"], "url": v["url"]} for k, v in services.SERVICES.items()],
            "node": bool(npx()),
            "installer_mode": mode,
            "job": job["id"] if busy() else None,
        }

    @router.get("/v1/admin/marketplace/models/{name}/license")
    async def model_license(name: str, request: Request, tag: str = "latest"):
        admin(request)
        try:
            return await marketplace.model_license(holder["gw"].client, holder["store"], name, tag)
        except marketplace.MarketplaceError as e:
            fail(e)

    @router.get("/v1/skills/{skill_id}/license")
    async def skill_license(skill_id: str, request: Request):
        """Texte de la licence d'une compétence (fichier fourni avec la gateway) et la notice d'origine."""
        shared.plan_ctx({}, request)
        s = skills.load().get(skill_id)
        if not s:
            raise VirtualKeyError(404, f"Compétence '{skill_id}' introuvable.")
        folder = skills.PACKS_DIR
        return {
            "id": s.id,
            "license": s.license,
            "source": s.source,
            "url": s.url,
            "text": (folder / "licenses" / s.license_file).read_text(encoding="utf-8"),
            "notice": (folder / "NOTICE.md").read_text(encoding="utf-8"),
        }

    # ----- installation en un clic : une à la fois, étapes l'une après l'autre, journal lisible dans la page -----
    def tail() -> list[str]:
        try:
            lines = log_path().read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return [line[:300] for line in lines[-LOG_LINES:]]

    async def run(key: str, steps: list, cwd: Path, record: bool | str, then=None) -> None:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "CI": "1", "NO_COLOR": "1", "npm_config_fund": "false", "npm_config_audit": "false"}
        env.pop("ELECTRON_RUN_AS_NODE", None)  # héritée d'un terminal de VS Code : fausse les applis Electron
        installer_env = installers.installer_env(env)  # comme un double-clic (CI=1 fait planter celui de Chatbox CE)
        code = 0
        if record is True:  # réinstallation : ce qui ferait échouer une étape (déjà déclaré) est retiré juste avant
            pre = await asyncio.to_thread(lambda: [installers.replaced_first(t) for t in steps])
            steps = [step for t, first in zip(steps, pre) for step in ((first, t) if first else (t,))]
        with path.open("w", encoding="utf-8") as out:
            for template in steps:
                out.write("> " + installers.label(template) + "\n")  # sans le chemin de ce PC
                out.flush()
                if record == "forget" and await asyncio.to_thread(installers.already_undone, template):
                    out.write("Déjà retiré.\n")
                    continue
                if record == "forget" and await asyncio.to_thread(installers.stop_running, template):
                    out.write("Ses programmes encore lancés sont arrêtés d'abord (fichiers libérés).\n")
                try:
                    await asyncio.to_thread(installers.refresh_path)  # une dépendance vient peut-être d'arriver
                    argv = installers.resolve(template)
                except marketplace.MarketplaceError as e:
                    out.write(e.message + "\n")
                    code = -1
                    break
                for attempt in range(1 + INSTALLER_RETRIES):
                    try:
                        proc = await asyncio.to_thread(
                            subprocess.Popen,
                            argv,
                            cwd=cwd,
                            env=installer_env if installers.windows_installer(template) else env,
                            stdin=subprocess.DEVNULL,
                            stdout=out,
                            stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    except OSError as e:
                        out.write(f"Lancement impossible : {e}\n")
                        code = -1
                        break
                    job["proc"] = proc
                    try:
                        code = await asyncio.to_thread(proc.wait, STEP_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        out.write("Trop long : installation arrêtée.\n")
                        code = -2
                    if code not in INSTALLER_CRASH or not installers.windows_installer(template):
                        break
                    if attempt < INSTALLER_RETRIES:
                        out.write(
                            "L'installateur s'est fermé brusquement (plantage connu de certains installateurs) : "
                            "nouvel essai.\n"
                        )
                        out.flush()
                if code:
                    break
                if record == "forget":
                    await asyncio.to_thread(installers.tidy_after, template)
            if code == 0 and then is not None:
                try:
                    note = await then()
                    if note:
                        out.write(note + "\n")
                except marketplace.MarketplaceError as e:
                    out.write(e.message + "\n")
                    code = -3
                except Exception:  # jamais une tâche laissée « en cours »
                    log.exception("Marketplace : étape finale de %s", key)
                    out.write("Étape finale impossible (voir le journal de Lezard IA).\n")
            out.write("Terminé.\n" if code == 0 else f"Échec (code {code}).\n")
        if code == 0 and record:
            done = done_installs()
            if record == "forget":  # désinstallé
                done.pop(key, None)
            else:
                done[key] = {"at": int(time.time())}
            holder["store"].set_setting(INSTALLED_SETTING, done)
        detection.forget()  # même après un échec : une partie des étapes a peut-être abouti
        if code == 0 and record is True:  # outil ou application installé : redémarrer seulement si c'est utile
            try:
                job["restart"] = await asyncio.to_thread(needs_restart, key)
            except Exception:  # simple conseil : jamais une installation réussie laissée « en cours »
                log.exception("Marketplace : vérification après installation impossible")
                job["restart"] = False
        job.update(state="done" if code == 0 else "error", code=code, proc=None)

    def needs_restart(key: str) -> bool:
        """Après l'installation d'un outil : sa commande du chat marche-t-elle déjà ? Le PATH de Windows est relu
        (le programme vient d'arriver) ; s'il reste introuvable par Lezard IA, seul un redémarrage le fera voir.
        Sans commande du chat (bibliothèque, extension du navigateur, outil de Claude Code) : rien à redémarrer ici."""
        installers.refresh_path()
        eid = key.split(":", 1)[1]
        if eid not in marketplace.CHAT:
            return False
        return not any(a["id"] == eid for a in chat_tools.available(holder["store"]))

    def start(
        key: str, steps: list, cwd: Path, restart: bool, after: str | None, record: bool | str, then=None
    ) -> dict:
        if busy():
            raise VirtualKeyError(409, "Une installation est déjà en cours : attendez qu'elle se termine.")
        job.update(id=key, state="running", code=None, restart=restart, after=after)
        job["task"] = asyncio.create_task(run(key, steps, cwd, record, then))
        return {"state": "running", "id": key, "restart": restart}

    @router.get("/v1/admin/marketplace/job")
    async def job_status(request: Request):
        admin(request)
        if job["id"] is None:
            return {"state": "idle"}
        return {
            "id": job["id"],
            "state": job["state"],
            "code": job["code"],
            "restart": job["restart"],
            "after": job["after"],
            "log": await asyncio.to_thread(tail),
        }

    @router.post("/v1/admin/marketplace/components/{cid}/install", status_code=202)
    async def install_component(cid: str, request: Request):
        admin(request)
        try:
            c = marketplace.component(cid)
        except marketplace.MarketplaceError as e:
            fail(e)
        request.state.audit = {"target": f"marketplace:{cid}"}  # action sensible : journal d'audit (20.2)
        if cid == "pc" and sys.platform != "win32":
            raise VirtualKeyError(409, "Contrôle du PC : Windows seulement.")
        key = f"component:{cid}"
        if updates.installer_mode():  # installation Windows : le lanceur (packs figés et vérifiés)
            root = updates.ROOT
            steps = [[str(root / "python" / "python.exe"), str(root / "lezard.py"), *c["action"]]]
            return start(key, steps, root, c["restart"], None, False)
        r = installers.recipe(key)
        if "env" in r:  # recherche web, contrôle du PC : un réglage de .env, actif tout de suite
            if busy():
                raise VirtualKeyError(409, "Une installation est déjà en cours : attendez qu'elle se termine.")
            await asyncio.to_thread(installers.set_env, updates.ROOT / ".env", *r["env"])
            job.update(id=key, state="done", code=0, restart=False, after=None, proc=None)
            return {"state": "done", "id": key, "restart": False}
        try:
            steps = installers.plan(key)
        except marketplace.MarketplaceError as e:
            fail(e)
        return start(key, steps, updates.ROOT, bool(r.get("restart")), r.get("after"), False)

    @router.post("/v1/admin/marketplace/{kind}/{item}/install", status_code=202)
    async def install_item(kind: str, item: str, request: Request):
        """Outil pour agents ou application : sa recette figée, lancée sur ce PC."""
        admin(request)
        if kind not in ("tools", "apps"):
            raise VirtualKeyError(404, "Rien à installer ici.")
        key = f"{kind[:-1]}:{item}"
        request.state.audit = {"target": f"marketplace:{key}"}
        try:
            r = installers.recipe(key)
            if r.get("windows") and sys.platform != "win32":
                raise marketplace.MarketplaceError(409, "Windows seulement.")
            steps = installers.plan(key)
        except marketplace.MarketplaceError as e:
            fail(e)
        # redémarrage proposé à la fin seulement si la commande du chat de l'outil ne marche pas encore (needs_restart)
        ctx = admin(request)
        then = None
        if item in (app_connect.APPS if kind == "apps" else app_connect.TOOLS):  # branché d'office (app_connect.py)

            async def then() -> str | None:
                note = await asyncio.to_thread(
                    app_connect.connect,
                    item,
                    holder["store"],
                    ctx.identity,
                    ctx.base_plan or ctx.plan,
                    holder["remote"].port,
                    True,  # juste installée : ouverte par son installateur, elle peut être refermée
                )
                if note and not note.startswith("Branchement automatique impossible"):
                    use = marketplace.chat_usage(item)["use"] if item in marketplace.CHAT else None
                    job["after"] = (f"Dans le chat de Lezard IA : {use}. " if use else "") + note
                elif note:
                    job["after"] = f"{note} {job['after'] or ''}".strip()
                return note

        return start(key, steps, Path.home(), False, after_install(key, r.get("after")), True, then)

    @router.post("/v1/admin/marketplace/apps/{item}/connect")
    async def connect_app(item: str, request: Request):
        """Rebranche une application déjà installée sur Lezard IA (nouvelle clé, l'ancienne est révoquée)."""
        ctx = admin(request)
        if item not in app_connect.APPS:
            raise VirtualKeyError(404, "Cette application se branche avec son guide.")
        request.state.audit = {"target": f"marketplace:app:{item}:connect"}
        note = await asyncio.to_thread(
            app_connect.connect, item, holder["store"], ctx.identity, ctx.base_plan or ctx.plan, holder["remote"].port
        )
        return {"connected": not note.startswith("Branchement automatique impossible"), "message": note}

    @router.post("/v1/admin/marketplace/{kind}/{item}/uninstall", status_code=202)
    async def uninstall_item(kind: str, item: str, request: Request):
        """Outil ou application : les étapes inverses de sa recette, s'il en a."""
        admin(request)
        if kind not in ("tools", "apps"):
            raise VirtualKeyError(404, "Rien à désinstaller ici.")
        key = f"{kind[:-1]}:{item}"
        request.state.audit = {"target": f"marketplace:{key}:uninstall"}
        try:
            steps = installers.uninstall_steps(key)
        except marketplace.MarketplaceError as e:
            fail(e)
        if not steps:
            hint = installers.uninstall_hint(key) or "Désinstallez-le avec l'outil qui l'a installé."
            raise VirtualKeyError(409, f"Pas de désinstallation depuis la page. {hint}")
        if lacking := [s[0] for s in steps if installers.program(s[0]) is None]:
            raise VirtualKeyError(409, f"Introuvable sur ce PC : {', '.join(dict.fromkeys(lacking))}.")
        return start(key, steps, Path.home(), False, None, "forget")

    # ----- serveurs MCP : déclarés dans data/mcp.json, puis relancés -----
    async def reload() -> list[dict]:
        await mcp.stop()
        await mcp.load(lambda: services.server_configs(holder["store"], holder["cipher"]))
        return mcp.overview()

    @router.post("/v1/admin/marketplace/mcp/{sid}")
    async def add_mcp(sid: str, request: Request):
        admin(request)
        body = await json_object(request)
        request.state.audit = {"target": f"mcp:{sid}"}
        command = npx()
        if not command:
            node = installers.dependency_steps(["npx"])
            if not node:
                raise VirtualKeyError(
                    409,
                    "Node.js 24 ou plus est requis pour les serveurs MCP : installez-le ou mettez-le à jour "
                    "(https://nodejs.org), puis réessayez.",
                )
            try:
                await asyncio.to_thread(marketplace.check_mcp, sid, body.get("folder"), body.get("secret"))
            except marketplace.MarketplaceError as e:
                fail(e)

            async def declare() -> str:  # Node.js installé : le serveur est déclaré puis lancé
                found = npx()
                if not found:
                    raise marketplace.MarketplaceError(  # ancien Node.js plus haut dans le PATH (nvm…) : à retirer
                        409,
                        "Node.js 24 ou plus introuvable après l'installation : mettez-le à jour (https://nodejs.org).",
                    )
                await asyncio.to_thread(marketplace.add_mcp, sid, found, body.get("folder"), body.get("secret"))
                state = next((s for s in await reload() if s["name"] == sid), None)
                return (
                    "Serveur MCP connecté." if state and state["ok"] else "Serveur MCP déclaré (pas encore connecté)."
                )

            return JSONResponse(start(f"mcp:{sid}", node, Path.home(), False, None, False, declare), status_code=202)
        try:
            cfg = await asyncio.to_thread(marketplace.add_mcp, sid, command, body.get("folder"), body.get("secret"))
        except marketplace.MarketplaceError as e:
            fail(e)
        state = next((s for s in await reload() if s["name"] == sid), None)
        return {
            "id": sid,
            "declared": cfg,
            "connected": bool(state and state["ok"]),
            "error": state and state.get("error"),
        }

    @router.delete("/v1/admin/marketplace/mcp/{sid}")
    async def delete_mcp(sid: str, request: Request):
        admin(request)
        request.state.audit = {"target": f"mcp:{sid}"}
        try:
            removed = await asyncio.to_thread(marketplace.remove_mcp, sid)
        except marketplace.MarketplaceError as e:
            fail(e)
        await reload()
        return {"id": sid, "removed": removed}

    return router
