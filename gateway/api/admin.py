"""Administration (clé maître) : fournisseurs, secrets, restrictions, modèles Ollama, recherche réseau."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import StreamingResponse

from ..bridge import validate_name
from ..config import ProviderConfig, hosted_instance, preset, presets
from ..crypto import Cipher, CryptoError, mask
from ..edition import PRICE, account_url, edition, license_info, load_license
from .. import auto_tunnel, benchmark, cloudflared, mcp, ollama_library, services, sources, updates
from .. import model_catalog
from ..license import LicenseError
from ..free_tiers import (
    CHECKED as FREE_CHECKED,
    TIERS,
    apply_rules as apply_free_rules,
    remove_rules as remove_free_rules,
)
from ..hardware import disk_info, is_this_machine
from ..local_context import server_root
from ..model_catalog import disk_needed, find, recommend
from ..keys import (
    PREFIX as KEY_PREFIX,
    WEB_2FA,
    VirtualKeyError,
    display_prefix,
    generate_secret,
    hash_secret,
    validate_key,
)
from ..local_discovery import OLLAMA_DOWNLOAD, find_servers, ollama_executable, start_ollama, this_host
from ..network_scan import ScanError, default_targets, expand, scan, suggest_name
from ..provider_rules import validate_rules
from ..providers.base import Provider, ProviderError
from ..remote import TunnelError, lan_addresses
from ..audit import safe_url
from ..validation import json_object
from ..core import Gateway, _err

log = logging.getLogger("gateway")

WEB_KEY_NAME = "Accès web (compte lezard-ia.fr)"
WEB_ACCOUNT_SITE = "https://lezard-ia.fr/web.html"


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    admin = shared.admin

    def _provider_or_404(name: str) -> Provider:
        p = holder["gw"].providers.get(name)
        if not p:
            raise VirtualKeyError(404, f"Provider '{name}' inconnu.")
        return p

    def not_gateway_key(secret: str) -> None:
        """Refuse un identifiant de CETTE passerelle collé (ou rempli par le navigateur) à la place de la clé du
        fournisseur : il serait envoyé à un tiers, et le fournisseur le refuserait de toute façon."""
        if secret in holder["gw"].plans.keys or secret.startswith(KEY_PREFIX):
            raise VirtualKeyError(
                400,
                "Ceci est une clé de cette passerelle (sans doute remplie automatiquement par "
                "le navigateur), pas la clé API du fournisseur. Collez la clé obtenue dans "
                "la console du fournisseur.",
            )

    @router.get("/v1/admin/metrics/status")
    async def metrics_status(request: Request):
        admin(request)
        return {"recorder": holder["gw"].metrics.stats(), "fx": holder["fx"].status()}

    @router.post("/v1/admin/fx/refresh")
    async def fx_refresh(request: Request):
        admin(request)
        try:
            await holder["fx"].refresh()
        except (httpx.HTTPError, ValueError) as e:
            raise ProviderError(502, f"Actualisation du taux impossible : {e}", "fx")
        return holder["fx"].status()

    @router.delete("/v1/admin/security/blocks/{ip}")
    async def unblock_ip(ip: str, request: Request):
        """Lève le blocage anti force brute d'une IP (12.3)."""
        admin(request)
        if not holder["auth"].unblock(ip):
            raise VirtualKeyError(404, f"L'adresse {ip} n'est pas bloquée.")
        return {"ip": ip, "unblocked": True}

    @router.get("/v1/admin/credentials")
    async def list_credentials(request: Request):
        admin(request)
        return {
            "object": "list",
            "encryption_key": holder["cipher"].fingerprint,
            "data": [c for c in holder["store"].list_credentials() if not c["provider"].startswith(services.PREFIX)],
        }

    async def reload_mcp() -> None:
        await mcp.stop()
        await mcp.load(lambda: services.server_configs(holder["store"], holder["cipher"]))

    @router.get("/v1/admin/services")
    async def list_services(request: Request):
        admin(request)
        return {"object": "list", **services.overview(holder["store"])}

    @router.post("/v1/admin/services/keys")
    async def create_service_key(request: Request):
        """Une clé de service : étiquette, service, accès (tous les dépôts ou dépôts précis), puis outils rechargés."""
        admin(request)
        body = await json_object(request)
        secret = body.get("secret")
        if not isinstance(secret, str):
            raise VirtualKeyError(400, "'secret' est requis.")
        not_gateway_key(secret.strip())
        try:
            entry = services.create_key(
                holder["store"],
                holder["cipher"],
                str(body.get("service") or ""),
                str(body.get("label") or ""),
                secret,
                body.get("scope"),
            )
        except services.ServiceError as e:
            raise VirtualKeyError(400, str(e))
        await reload_mcp()
        return {"data": entry, "mcp": mcp.overview()}

    @router.delete("/v1/admin/services/keys/{kid}")
    async def delete_service_key(kid: str, request: Request):
        admin(request)
        if not services.delete_key(holder["store"], kid):
            raise VirtualKeyError(404, f"Clé '{kid}' introuvable.")
        await reload_mcp()
        return {"object": "list", "data": services.overview(holder["store"])["keys"], "mcp": mcp.overview()}

    @router.get("/v1/admin/nextcloud")
    async def get_nextcloud(request: Request):
        admin(request)
        return {"data": sources.nextcloud_view(holder["store"])}

    @router.put("/v1/admin/nextcloud")
    async def put_nextcloud(request: Request):
        """Adresse, utilisateur et mot de passe d'application Nextcloud (outils /docs), chiffrés sur ce PC."""
        admin(request)
        body = await json_object(request)
        password = str(body.get("password") or "").strip()
        if password:
            not_gateway_key(password)
        try:
            tools = sources.save_nextcloud(
                holder["store"], holder["cipher"], str(body.get("url") or ""), str(body.get("user") or ""), password
            )
        except ValueError as e:
            raise VirtualKeyError(400, str(e))
        return {"data": sources.nextcloud_view(holder["store"]), "tools": tools}

    @router.delete("/v1/admin/nextcloud")
    async def delete_nextcloud(request: Request):
        admin(request)
        sources.clear_nextcloud(holder["store"], holder["cipher"])
        return {"data": sources.nextcloud_view(holder["store"])}

    @router.get("/v1/admin/mcp")
    async def list_mcp(request: Request):
        admin(request)
        return {"object": "list", "data": mcp.overview()}

    @router.post("/v1/admin/mcp/reload")
    async def reload_mcp_route(request: Request):
        """Relance les serveurs MCP (après une modification de data/mcp.json)."""
        admin(request)
        await reload_mcp()
        return {"object": "list", "data": mcp.overview()}

    @router.put("/v1/admin/credentials/{provider}")
    async def set_credential(provider: str, request: Request):
        admin(request)
        p = _provider_or_404(provider)
        body = await json_object(request)
        secret = body.get("api_key")
        if not isinstance(secret, str) or len(secret.strip()) < 8:
            raise VirtualKeyError(400, "'api_key' est requis.")
        secret = secret.strip()
        not_gateway_key(secret)
        cipher: Cipher = holder["cipher"]
        holder["store"].set_credential(provider, cipher.encrypt(secret, provider), mask(secret), cipher.fingerprint)
        p.cfg.stored_key, p.cfg.enabled = secret, True
        status = (await holder["gw"].detect(force=True))[provider]
        return {
            "provider": provider,
            "hint": mask(secret),
            "enabled": True,
            "online": status["online"],
            "error": status["error"],
        }

    @router.delete("/v1/admin/credentials/{provider}")
    async def delete_credential(provider: str, request: Request):
        admin(request)
        p = _provider_or_404(provider)
        if not holder["store"].delete_credential(provider):
            raise VirtualKeyError(404, f"Aucun secret enregistré pour '{provider}'.")
        p.cfg.stored_key = None
        if p.hosting == "cloud" and not p.cfg.resolved_key():
            p.cfg.enabled = False
        await holder["gw"].detect(force=True)
        return {"provider": provider, "deleted": True, "enabled": p.cfg.enabled}

    # ----- page Modèles : fournisseurs et modèles locaux -----
    def is_ollama(p: Provider) -> bool:
        return p.cfg.kind == "openai" and (p.name.startswith("ollama") or ":11434" in p.cfg.base_url)

    def ollama_root(p: Provider) -> str:
        url = p.cfg.base_url.rstrip("/")
        return url[:-3] if url.endswith("/v1") else url

    def key_source(p: Provider) -> str | None:
        if p.cfg.stored_key:
            return "dashboard"
        if p.cfg.api_key:
            return "config"
        if p.cfg.api_key_env and os.getenv(p.cfg.api_key_env):
            return "env"
        return None

    @router.get("/v1/admin/providers")
    async def admin_providers(request: Request):
        admin(request)
        gw: Gateway = holder["gw"]
        status = await gw.detect(force=True)  # page Modèles : état réel
        hints = {c["provider"]: c["hint"] for c in holder["store"].list_credentials()}
        custom = {c["name"] for c in holder["store"].list_custom_providers()}
        out = []
        for name, p in gw.providers.items():
            if p.cfg.owner:  # ponts : gérés par leur compte
                continue
            s = status.get(name, {})
            out.append(
                {
                    "name": name,
                    "kind": p.cfg.kind,
                    "hosting": p.hosting,
                    "base_url": p.cfg.base_url,
                    "enabled": p.cfg.enabled,
                    "online": s.get("online", False),
                    "models": s.get("models", []),
                    "error": s.get("error"),
                    "key": {"source": key_source(p), "hint": hints.get(name), "env": p.cfg.api_key_env},
                    "custom": name in custom,
                    "ollama": is_ollama(p),
                    "free_tier": name in gw.registry.free_enabled,
                    "rules": gw.guard.rules.get(name, {}),
                    "usage": gw.guard.status(name),
                }
            )
        return {"object": "list", "data": out}

    @router.put("/v1/admin/providers/{name}/rules")
    async def admin_provider_rules(name: str, request: Request):
        """Restrictions d'une IA en entrée : modèles, plans, débit, budget, tokens par réponse."""
        admin(request)
        gw: Gateway = holder["gw"]
        if name not in gw.providers or gw.providers[name].cfg.owner:
            raise VirtualKeyError(404, f"Fournisseur '{name}' inconnu.")
        rules = validate_rules(await json_object(request))
        gw.guard.set(name, rules)
        return {"name": name, "rules": rules, "usage": gw.guard.status(name)}

    @router.get("/v1/admin/providers/presets")
    async def admin_provider_presets(request: Request):
        """Fournisseurs proposés dans la fenêtre « Ajouter un modèle » (nom, URL et type préremplis)."""
        admin(request)
        taken = set(holder["gw"].providers)
        return {
            "object": "list",
            "data": [
                {**asdict(p), "added": p.name in taken, "env_key": bool(p.key_env and os.getenv(p.key_env))}
                for p in presets()
            ],
        }

    @router.post("/v1/admin/providers", status_code=201)
    async def admin_add_provider(request: Request):
        """Ajoute une IA : nom, URL, clé API (chiffrée) et restrictions facultatives, en une fois. Rien n'est
        enregistré si un champ est invalide."""
        admin(request)
        body = await json_object(request)
        gw: Gateway = holder["gw"]
        name = validate_name(body.get("name"), set(gw.providers) | {"auto"})
        kind, hosting = body.get("kind", "openai"), body.get("hosting", "cloud")
        if kind not in ("openai", "anthropic"):
            raise VirtualKeyError(400, "'kind' : openai (compatible OpenAI) ou anthropic.")
        if hosting not in ("local", "cloud"):
            raise VirtualKeyError(400, "'hosting' : local ou cloud.")
        base_url = str(body.get("base_url") or "").strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")) or " " in base_url:
            raise VirtualKeyError(400, "'base_url' doit être une URL http(s), ex. https://api.deepseek.com/v1.")
        secret = (body.get("api_key") or "").strip()
        if secret and len(secret) < 8:
            raise VirtualKeyError(400, "Clé API trop courte.")
        if secret:
            not_gateway_key(secret)
        pre = preset(name)
        key_env = pre.key_env if pre else None  # ex. OPENAI_API_KEY déjà dans .env
        if hosting == "cloud" and not secret and not (key_env and os.getenv(key_env)):
            raise VirtualKeyError(400, "Clé API requise pour une IA cloud.")
        rules = validate_rules(body.get("rules"))
        holder["store"].add_custom_provider(name, kind, base_url, hosting)
        p = gw.add_provider(ProviderConfig(name, kind, base_url, hosting, key_env))
        if secret:
            cipher: Cipher = holder["cipher"]
            holder["store"].set_credential(name, cipher.encrypt(secret, name), mask(secret), cipher.fingerprint)
            p.cfg.stored_key = secret
        if rules:
            gw.guard.set(name, rules)
        s = (await gw.detect(force=True))[name]
        request.state.audit = {
            "target": name,
            "detail": {"kind": kind, "hosting": hosting, "base_url": safe_url(base_url)},
        }
        return {"name": name, "online": s["online"], "models": s["models"], "rules": rules, "error": s["error"]}

    # ----- offres gratuites officielles (13.8) -----
    def free_tier_item(tier, status: dict) -> dict:
        gw: Gateway = holder["gw"]
        p = gw.providers.get(tier.provider)
        s = status.get(tier.provider, {})
        free = [m for m in s.get("models", []) if gw.registry.is_free(tier.provider, m)] if p else []
        return {
            **tier.to_dict(),
            "enabled": tier.provider in gw.registry.free_enabled,
            "configured": p is not None,
            "has_key": bool(p and p.cfg.resolved_key()),
            "online": s.get("online", False),
            "free_models": free[:50],
            "provider_rules": gw.guard.rules.get(tier.provider, {}),
            "usage": gw.guard.status(tier.provider) if p else None,
        }

    @router.get("/v1/admin/free-tiers")
    async def admin_free_tiers(request: Request):
        """Offres gratuites officielles : état, quotas, usage des conversations pour l'entraînement."""
        admin(request)
        status = await holder["gw"].detect()
        return {"object": "list", "checked": FREE_CHECKED, "data": [free_tier_item(t, status) for t in TIERS.values()]}

    @router.put("/v1/admin/free-tiers/{provider}")
    async def admin_set_free_tier(provider: str, request: Request):
        """{"enabled": true|false} : rend les modèles de l'offre gratuits et préremplit (ou retire) ses quotas."""
        admin(request)
        tier = TIERS.get(provider)
        if not tier:
            raise VirtualKeyError(404, f"Pas d'offre gratuite connue pour '{provider}'.")
        enabled = (await json_object(request)).get("enabled")
        if not isinstance(enabled, bool):
            raise VirtualKeyError(400, "'enabled' (true ou false) est requis.")
        gw: Gateway = holder["gw"]
        if enabled and provider not in gw.providers:  # absent de providers.json : ajouté, sans clé
            holder["store"].add_custom_provider(provider, "openai", tier.base_url, "cloud")
            gw.add_provider(
                ProviderConfig(
                    provider, "openai", tier.base_url, "cloud", tier.key_env, enabled=bool(os.getenv(tier.key_env))
                )
            )  # clé à saisir sinon
        current = gw.guard.rules.get(provider, {})
        gw.guard.set(
            provider, validate_rules(apply_free_rules(current, tier) if enabled else remove_free_rules(current, tier))
        )
        (gw.registry.free_enabled.add if enabled else gw.registry.free_enabled.discard)(provider)
        holder["store"].set_setting("free_tiers", {"providers": sorted(gw.registry.free_enabled)})
        return free_tier_item(tier, await gw.detect(force=enabled))

    def web_host() -> str | None:
        remote = holder["remote"]
        host = (urlparse(remote.status.public_url or "").hostname or "") if remote.active else ""
        return host.removesuffix(".lezard-ia.fr") if host.endswith(".lezard-ia.fr") and edition() == "pro" else None

    def paused() -> dict:
        """Rien n'est mis en pause : clés et fournisseurs cloud restent actifs dans toute la version."""
        return {"keys": 0, "cloud": []}

    @router.get("/v1/admin/edition")
    async def admin_edition(request: Request):
        """Accès par le site web (29.4) : licence de cette installation, compte et adresse web."""
        admin(request)
        return {
            "edition": edition(),
            "price": PRICE,
            "instance": holder["instance"],  # identifiant de cette installation, auquel la licence est liée
            "device_name": socket.gethostname()[:60],  # nom proposé au compte client pour ce PC
            "account_url": account_url(),  # « Activer avec mon compte » (serveur central, 30.5)
            "web_host": web_host(),  # « Utiliser l'IA sur le web » (31.6) : <nom> de <nom>.lezard-ia.fr
            "paused": paused(),  # licence finie sans perte (29.5) : ce qui attend son retour
            **license_info(),
        }

    async def start_auto_tunnel() -> None:
        """Accès à distance automatique : le tunnel gardé, sinon un nouveau demandé à lezard-ia.fr, puis démarré."""
        store, cipher, client = holder["store"], holder["cipher"], holder["gw"].client
        await cloudflared.ensure(client)
        port = holder["remote"].port
        saved = store.get_setting(auto_tunnel.SETTING) or {}
        if saved.get("auto") and await auto_tunnel.forget_if_deleted(client, store, cipher, holder["instance"], port):
            saved = {}  # supprimé par le ménage de lezard-ia.fr : une nouvelle adresse est demandée
        tunnel = None
        if saved.get("auto") and saved.get("token"):
            try:
                tunnel = {"hostname": saved["hostname"], "token": cipher.decrypt(saved["token"], auto_tunnel.CONTEXT)}
            except CryptoError:
                tunnel = None
        if tunnel is None:
            tunnel = await auto_tunnel.create(client, holder["instance"], socket.gethostname(), port)
            await asyncio.to_thread(auto_tunnel.save, store, cipher, tunnel)
        await shared.start_remote("cloudflare", None, False, {"hostname": tunnel["hostname"], "token": tunnel["token"]})
        holder["managed_tunnel"] = tunnel["hostname"]
        log.warning("Accès à distance automatique : https://%s", tunnel["hostname"])

    @router.post("/v1/admin/web-account")
    async def admin_web_account(request: Request):
        """« Utiliser l'IA sur le web » avec un compte lezard-ia.fr : une clé web neuve, aléatoire (la précédente est
        révoquée), et l'adresse publique de ce PC. La page les remet au site dans le fragment #lier=…, que le
        navigateur n'envoie à aucun serveur ; le site les chiffre dans le coffre du compte lié. Sans accès à distance,
        il est créé tout seul (auto_tunnel) : tunnel et adresse fixe <nom>.lezard-ia.fr, cloudflared téléchargé."""
        ctx = admin(request)
        totp = holder.get("totp")
        if not totp or not await asyncio.to_thread(totp.enabled):  # la clé web n'existe qu'avec la 2FA de ce PC
            return _err(
                409,
                "Activez d'abord la double authentification de ce PC : depuis Internet, la clé web demandera "
                "toujours un code de votre application.",
                "totp_setup",
            )
        remote = holder["remote"]
        if not remote.active:
            if os.getenv("REMOTE_ACCESS", "").strip():
                return _err(
                    409,
                    "Votre accès à distance (REMOTE_ACCESS) est arrêté : relancez-le (tableau de bord, « Accès à "
                    "distance ») puis recommencez.",
                )
            try:
                await start_auto_tunnel()
            except auto_tunnel.AutoTunnelError as e:
                return _err(e.status, e.message, "auto_tunnel")
            except cloudflared.CloudflaredError as e:
                return _err(502, f"Accès à distance : {e}", "auto_tunnel")
            except TunnelError as e:
                return _err(e.status, f"Accès à distance : {e.message}", "auto_tunnel")
        url = remote.status.public_url if remote.active else None
        if not url:
            return _err(409, "Accès à distance encore en démarrage : réessayez dans quelques secondes.")
        store = holder["store"]
        for k in store.list_keys(ctx.identity):
            if (k.get("metadata") or {}).get(WEB_2FA) and k["status"] != "revoked":
                store.set_key_status(ctx.identity, k["id"], "revoked", "remplacée par une nouvelle clé web")
        data = validate_key(
            {
                "name": WEB_KEY_NAME,
                "description": "Créée par « Créer mon compte web » ; gardée, chiffrée, dans le compte lezard-ia.fr. "
                "Depuis Internet, un code de double authentification est exigé.",
                "metadata": {WEB_2FA: True},
            },
            ctx.base_plan or ctx.plan,
        )
        secret = generate_secret()
        key = store.add_key(ctx.identity, {**data, "key_prefix": display_prefix(secret)}, hash_secret(secret))
        store.log_key_event(ctx.identity, key["id"], "created")
        request.state.audit = {"target": key["id"], "detail": {"name": WEB_KEY_NAME}}  # 20.2
        host = urlparse(url).hostname or ""
        return {
            "name": socket.gethostname()[:40] or "Mon PC",
            "url": f"{urlparse(url).scheme}://{urlparse(url).netloc}",
            "key": secret,
            "stable": bool(remote.status.to_dict().get("stable_url")) or host.endswith(".lezard-ia.fr"),
            "site": WEB_ACCOUNT_SITE,
        }

    @router.get("/v1/admin/welcome")
    async def admin_welcome(request: Request):
        """Premier lancement guidé (32.4) : affiché au tableau de bord tant qu'il n'a pas été terminé."""
        admin(request)
        return {"done": bool((holder["store"].get_setting("welcome") or {}).get("done"))}

    @router.post("/v1/admin/welcome")
    async def admin_welcome_done(request: Request):
        admin(request)
        holder["store"].set_setting("welcome", {"done": True, "at": time.time()})
        return {"done": True}

    @router.post("/v1/admin/license")
    async def admin_license(request: Request):
        """Active la licence collée dans « Accès par le site » (30.4) : vérifiée avant d'être enregistrée."""
        admin(request)
        token = (await json_object(request)).get("license")
        if not isinstance(token, str):
            return _err(400, "'license' : le code de licence (texte qui commence par LZ1.).")
        try:
            load_license(token, holder["instance"])
        except LicenseError as e:
            try:  # code refusé : la licence déjà enregistrée reste en vigueur
                load_license((holder["store"].get_setting("license") or {}).get("token"), holder["instance"])
            except LicenseError:
                pass
            return _err(400, str(e), "license")
        holder["store"].set_setting("license", {"token": token.strip()})
        request.state.audit = {"detail": {"licence": license_info()["license"]["status"]}}
        if holder.get("managed_remote"):  # accès à distance géré (31.1), sans attendre le prochain passage
            asyncio.create_task(holder["managed_remote"]())
        return await admin_edition(request)

    @router.delete("/v1/admin/license")
    async def admin_license_remove(request: Request):
        admin(request)
        holder["store"].set_setting("license", {})
        load_license(None, holder["instance"])
        if holder.get("managed_remote"):
            asyncio.create_task(holder["managed_remote"]())
        return await admin_edition(request)

    @router.post("/v1/admin/network/scan")
    async def admin_network_scan(request: Request):
        """Serveurs de modèles des autres machines du réseau local (12.5) ; l'ajout passe ensuite par
        POST /v1/admin/providers (hébergement « local », coût 0 €)."""
        admin(request)
        if hosted_instance():
            raise ScanError(
                "Recherche indisponible sur une instance hébergée : son réseau n'est pas le vôtre. "
                "Utilisez un pont vers vos modèles locaux.",
                409,
            )
        body = await json_object(request, empty={})
        own = await asyncio.to_thread(lan_addresses, "0.0.0.0")
        targets = body.get("hosts") or default_targets(own)
        if not targets:
            raise ScanError(
                "Aucun réseau local détecté : indiquez les adresses des machines (GATEWAY_NETWORK_HOSTS "
                "ou champ « Adresses »)."
            )
        addresses = await asyncio.to_thread(expand, targets, own)
        gw: Gateway = holder["gw"]
        found = await scan(addresses, gw.client)
        known = {p.cfg.base_url.rstrip("/"): name for name, p in gw.providers.items()}
        taken = set(gw.providers)
        for f in found:
            f["provider"] = known.get(f["base_url"])  # déjà ajouté sous ce nom
            if not f["provider"]:
                f["suggested_name"] = suggest_name(f["server"], f["host"], taken)
                taken.add(f["suggested_name"])
        return {"targets": targets, "scanned": len(addresses), "found": found}

    @app.exception_handler(ScanError)
    async def scan_error(_: Request, e: ScanError):
        return _err(e.status, e.message, "network")

    @router.delete("/v1/admin/providers/{name}")
    async def admin_delete_provider(name: str, request: Request):
        admin(request)
        if not holder["store"].delete_custom_provider(name):
            raise VirtualKeyError(
                404 if name not in holder["gw"].providers else 409,
                f"'{name}' n'a pas été ajouté depuis cette page : désactivez-le plutôt dans la "
                "configuration (providers.json ou variables d'environnement).",
            )
        holder["store"].delete_credential(name)
        holder["gw"].providers.pop(name, None)
        holder["gw"].discovered.pop(name, None)
        holder["gw"].guard.forget(name)
        return {"name": name, "deleted": True}

    class _Hardware:  # relevé partagé avec le choix du modèle d'embeddings
        async def snapshot(self, providers, client):
            return await holder["gw"].hardware.snapshot(providers, client)

    hardware = _Hardware()

    @router.get("/v1/admin/hardware")
    async def admin_hardware(request: Request):
        """Matériel du PC hôte (13.1) : GPU, CPU, RAM, disque des modèles, modèles chargés. Clé maître seulement
        (14.3) : ces informations décrivent la machine du propriétaire."""
        admin(request)
        gw: Gateway = holder["gw"]
        return await hardware.snapshot(list(gw.providers.values()), gw.client)

    async def this_pc_ollama(gw: Gateway) -> dict:
        """Ollama de ce PC : fournisseur ajouté (target), ou serveur trouvé mais pas encore ajouté, ou installé mais
        arrêté ; `root` : adresse de son API native (test de performance)."""
        status = await gw.detect(max_age=10)  # la page vient de changer les modèles : relevé récent, pas le cache
        target = next(
            (
                p
                for p in gw.providers.values()
                if is_ollama(p)
                and not p.cfg.owner
                and is_this_machine(p.cfg.base_url)
                and status.get(p.name, {}).get("online")
            ),
            None,
        )
        out = {"target": target, "installed": status[target.name].get("models", []) if target else []}
        out.update(running=False, installable=False, root=server_root(target.cfg.base_url) if target else None)
        if not target and not hosted_instance():  # Ollama de ce PC pas (encore) ajouté à la gateway
            found = next((s for s in await find_servers(gw.client, gw.instance_id) if s["server"] == "ollama"), None)
            out.update(running=bool(found), installed=(found or {}).get("models", []))
            out["root"] = server_root(found["base_url"]) if found else None
            out["installable"] = not found and bool(await asyncio.to_thread(ollama_executable))
        return out

    @router.get("/v1/admin/models/recommendations")
    async def admin_model_recommendations(request: Request):
        """Catalogue de modèles locaux classé selon le matériel (13.2) : tient en VRAM, en VRAM + RAM, trop gros ;
        avec le test de performance, vitesse estimée de chaque modèle."""
        admin(request)
        return await recommendations()

    async def recommendations() -> dict:
        """Aussi lu par la marketplace de lezard-ia.fr (api/marketplace.py, ce qui est installé sur ce PC)."""
        gw: Gateway = holder["gw"]
        hw = await hardware.snapshot(list(gw.providers.values()), gw.client)
        o = await this_pc_ollama(gw)
        out = recommend(
            hw,
            o["installed"],
            bench=holder["store"].get_setting(benchmark.SETTING),
            measured=holder["store"].get_setting(benchmark.MODEL_SETTING),
        )
        out["ollama"] = o["target"].name if o["target"] else None  # installation en un clic : Ollama de cette machine
        out["ollama_running"] = bool(o["target"]) or o["running"]  # en marche, ajouté ou non
        out["ollama_installed"] = bool(o["target"]) or o["running"] or o["installable"]
        out["ollama_installer"] = updates.installer_mode()  # Ollama installable depuis Lezard IA (installateur)
        return out

    # ----- test de performance (choix des IA locales après l'installation) : tâche de fond, suivie par la page -----
    bench_job: dict = {"state": "idle", "step": None, "error": None, "task": None}

    @router.get("/v1/admin/benchmark")
    async def admin_benchmark_status(request: Request):
        admin(request)
        public = {k: v for k, v in bench_job.items() if k != "task"}
        return {**public, "result": holder["store"].get_setting(benchmark.SETTING)}

    @router.post("/v1/admin/benchmark", status_code=202)
    async def admin_benchmark_start(request: Request):
        """Lance le test (1 à 3 min, dont 1,4 Go de modèles de test au premier passage) ; la page suit l'état."""
        admin(request)
        if bench_job["state"] == "running":
            raise VirtualKeyError(409, "Un test de performance est déjà en cours.")
        gw: Gateway = holder["gw"]
        o = await this_pc_ollama(gw)
        if not o["root"]:
            hint = "démarrez-le" if o["installable"] else "installez-le depuis la page Modèles"
            raise VirtualKeyError(409, f"Ollama ne tourne pas sur ce PC : {hint}, puis relancez le test.")

        async def job() -> None:
            try:
                result = await benchmark.run(gw.client, o["root"], step=lambda text: bench_job.update(step=text))
                holder["store"].set_setting(benchmark.SETTING, result)
                bench_job.update(state="done", step=None)
            except benchmark.BenchmarkError as e:
                bench_job.update(state="error", step=None, error=str(e))
            except Exception as e:  # jamais une tâche de fond perdue sans trace
                log.exception("test de performance")
                bench_job.update(state="error", step=None, error=f"Test interrompu ({type(e).__name__}).")

        bench_job.update(state="running", step="Préparation…", error=None, task=asyncio.create_task(job()))
        return {"state": "running"}

    @router.get("/v1/admin/models/library")
    async def admin_models_library(request: Request):
        """Tous les modèles de la bibliothèque Ollama, avec chaque taille : mémoire, téléchargement, vitesse (mesurée
        ou estimée) et s'ils sont installés. Rien n'est installé ici : c'est la page qui le propose, au choix."""
        admin(request)
        return await library()

    async def library() -> dict:
        """Aussi lue par la marketplace de lezard-ia.fr (adéquation de chaque taille à ce PC)."""
        gw: Gateway = holder["gw"]
        store = holder["store"]
        try:
            lib = await ollama_library.cached(store, gw.client)
        except (httpx.HTTPError, ValueError):
            raise VirtualKeyError(502, "Bibliothèque Ollama injoignable (ollama.com) : réessayez plus tard.") from None
        hw = await hardware.snapshot(list(gw.providers.values()), gw.client)
        o = await this_pc_ollama(gw)
        bench = store.get_setting(benchmark.SETTING)
        measured = store.get_setting(benchmark.MODEL_SETTING) or {}
        b = model_catalog.budget(hw)
        have = set(o["installed"])
        rows = []
        for m in lib["models"]:
            for size in m["sizes"]:
                tag = f"{m['name']}:{size['label']}"
                entry = model_catalog.CatalogModel(
                    tag,
                    m["name"],
                    size["params_b"],
                    ollama_library.tag_size_gb(size["params_b"]),
                    ollama_library.uses(m["capabilities"]),
                )
                need = model_catalog.memory_needed(entry)
                rows.append(
                    {
                        "tag": tag,
                        "name": m["name"],
                        "description": m["description"],
                        "capabilities": m["capabilities"],
                        "params_b": size["params_b"],
                        "download": entry.download_gb * model_catalog.GB,
                        "memory": round(need),
                        "fit": model_catalog.fit(need, b),
                        "speed": model_catalog.speed_of(entry, bench, measured, b),
                        "speed_source": model_catalog.speed_source(entry, bench, measured),
                        "speed_label": benchmark.label(model_catalog.speed_of(entry, bench, measured, b))
                        if model_catalog.speed_of(entry, bench, measured, b) is not None
                        else None,
                        "installed": tag in have,
                    }
                )
        rows.sort(key=lambda r: (r["name"], r["params_b"]))
        return {
            "models": rows,
            "fetched_at": lib["fetched_at"],
            "stale": lib["stale"],
            "budget": b,
            "ollama": o["target"].name if o["target"] else None,
            "ollama_running": bool(o["target"]) or o["running"],
            "benchmark": bench,
        }

    shared.recommendations, shared.library = recommendations, library  # marketplace de lezard-ia.fr

    # ----- mesure d'un modèle précis, installé sur ce PC (vitesse réelle, pas estimée) -----
    model_job: dict = {"state": "idle", "model": None, "step": None, "error": None}

    @router.get("/v1/admin/benchmark/model")
    async def admin_benchmark_model_status(request: Request):
        admin(request)
        public = {k: v for k, v in model_job.items() if k != "task"}
        return {**public, "results": holder["store"].get_setting(benchmark.MODEL_SETTING) or {}}

    @router.post("/v1/admin/benchmark/model", status_code=202)
    async def admin_benchmark_model_start(request: Request):
        """Mesure la vitesse réelle d'un modèle déjà installé (2 passages, ~1 min). Installez-le d'abord."""
        admin(request)
        body = await json_object(request)
        tag = str(body.get("model") or "")
        if not benchmark.valid_model(tag):
            raise VirtualKeyError(400, "'model' : nom Ollama, ex. qwen3.8:27b.")
        if model_job["state"] == "running":
            raise VirtualKeyError(409, f"Une mesure est déjà en cours ({model_job['model']}).")
        gw: Gateway = holder["gw"]
        o = await this_pc_ollama(gw)
        if not o["root"]:
            raise VirtualKeyError(409, "Ollama ne tourne pas sur ce PC : démarrez-le, puis relancez la mesure.")
        if tag not in o["installed"]:
            raise VirtualKeyError(409, f"{tag} n'est pas installé : installez-le depuis la liste, puis mesurez-le.")

        async def job() -> None:
            try:
                result = await benchmark.measure_model(
                    gw.client, o["root"], tag, step=lambda text: model_job.update(step=text)
                )
                measured = holder["store"].get_setting(benchmark.MODEL_SETTING) or {}
                measured[tag] = result
                holder["store"].set_setting(benchmark.MODEL_SETTING, measured)
                model_job.update(state="done", step=None)
            except benchmark.BenchmarkError as e:
                model_job.update(state="error", step=None, error=str(e))
            except Exception as e:  # jamais une tâche de fond perdue sans trace
                log.exception("mesure d'un modèle")
                model_job.update(state="error", step=None, error=f"Mesure interrompue ({type(e).__name__}).")

        model_job.update(state="running", model=tag, step="Préparation…", error=None, task=asyncio.create_task(job()))
        return {"state": "running", "model": tag}

    # ----- installation d'Ollama depuis la page (installateur Windows) : lezard.py ollama-install -----
    ollama_job: dict = {"proc": None}

    @router.get("/v1/admin/local/ollama/install")
    async def admin_ollama_install_status(request: Request):
        admin(request)
        proc = ollama_job["proc"]
        if proc is None:
            return {"state": "idle"}
        code = proc.poll()
        return {"state": "running" if code is None else "done" if code == 0 else "error", "code": code}

    @router.post("/v1/admin/local/ollama/install", status_code=202)
    async def admin_ollama_install(request: Request):
        """Installateur officiel d'Ollama, figé et vérifié (version et empreinte de cette version de Lezard IA)."""
        admin(request)
        if hosted_instance() or not updates.installer_mode():
            raise VirtualKeyError(409, f"Installez Ollama vous-même : {OLLAMA_DOWNLOAD}")
        if ollama_job["proc"] is not None and ollama_job["proc"].poll() is None:
            raise VirtualKeyError(409, "Installation d'Ollama déjà en cours.")
        root = updates.ROOT
        ollama_job["proc"] = await asyncio.to_thread(
            subprocess.Popen,
            [str(root / "python" / "python.exe"), str(root / "lezard.py"), "ollama-install"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {"state": "running"}

    # ----- fenêtre « Ajouter un modèle », onglet IA locale : recherche sur CE PC, à la demande -----
    def same_url(url: str) -> str:
        return url.rstrip("/").replace("//127.0.0.1:", "//localhost:", 1)

    def free_name(base: str, taken: set[str]) -> str:
        name, n = base, 2
        while name in taken:
            name, n = f"{base}-{n}", n + 1
        return name

    @router.get("/v1/admin/local/discover")
    async def admin_local_discover(request: Request):
        """Ce qui tourne déjà sur ce PC (serveurs et leurs modèles, ajoutés ou non), Ollama installé / démarré, et
        les modèles installables classés selon le matériel."""
        admin(request)
        if hosted_instance():
            raise ScanError(
                "Instance hébergée : ce serveur n'est pas votre PC. Utilisez un pont vers vos modèles locaux.", 409
            )
        gw: Gateway = holder["gw"]
        servers = await find_servers(gw.client, gw.instance_id)
        known = {same_url(p.cfg.base_url): name for name, p in gw.providers.items() if not p.cfg.owner}
        taken = set(gw.providers) | {"auto"}
        for s in servers:
            s["provider"] = known.get(same_url(s["base_url"]))
            if not s["provider"]:
                s["suggested_name"] = free_name(s["server"], taken)
                taken.add(s["suggested_name"])
        running = next((s for s in servers if s["server"] == "ollama"), None)
        exe = await asyncio.to_thread(ollama_executable)
        hw = await hardware.snapshot(list(gw.providers.values()), gw.client)
        ollama = {
            "installed": bool(exe or running),
            "running": bool(running),
            "download_url": OLLAMA_DOWNLOAD,
            "provider": running["provider"] if running else None,
        }
        if not ollama["provider"]:  # nom et adresse pour l'ajouter avant un téléchargement
            ollama["suggested_name"] = running["suggested_name"] if running else free_name("ollama", taken)
            ollama["base_url"] = (
                running["base_url"]
                if running
                else (f"http://{'localhost' if this_host() == '127.0.0.1' else this_host()}:11434/v1")
            )
        return {"servers": servers, "ollama": ollama, "catalog": recommend(hw, running["models"] if running else [])}

    @router.post("/v1/admin/local/ollama/start")
    async def admin_start_ollama(request: Request):
        """Démarre Ollama installé sur ce PC mais arrêté (« ollama serve », détaché de la gateway)."""
        admin(request)
        if hosted_instance():
            raise ScanError("Instance hébergée : impossible de démarrer un programme sur votre PC.", 409)
        exe = await asyncio.to_thread(ollama_executable)
        if not exe:
            raise VirtualKeyError(404, f"Ollama n'est pas installé sur ce PC : {OLLAMA_DOWNLOAD}")
        logs = Path(os.getenv("GATEWAY_LOG_DIR", "data/logs"))
        if not await start_ollama(exe, holder["gw"].client, logs):
            raise ProviderError(
                504, f"Ollama ne répond pas après son lancement : voir {logs / 'ollama.log'}.", "ollama"
            )
        return {"running": True}

    # ----- Ollama prêt en un clic (avant d'installer un modèle) : installé, démarré, ajouté à la gateway -----
    ready_job: dict = {"state": "idle", "step": None, "provider": None, "error": None, "task": None}

    async def make_ollama_ready() -> str:
        """Installe Ollama s'il manque (installateur figé et vérifié de Lezard IA), le démarre, l'ajoute comme IA
        locale ; renvoie le nom du fournisseur, prêt pour le téléchargement d'un modèle."""
        gw: Gateway = holder["gw"]
        exe = await asyncio.to_thread(ollama_executable)
        running = next((s for s in await find_servers(gw.client, gw.instance_id) if s["server"] == "ollama"), None)
        if not exe and not running:
            if not updates.installer_mode():
                raise VirtualKeyError(409, f"Installez Ollama vous-même : {OLLAMA_DOWNLOAD}")
            ready_job["step"] = "Installation d'Ollama (≈ 1,5 Go, quelques minutes)…"
            root = updates.ROOT
            code = await asyncio.to_thread(
                subprocess.call,
                [str(root / "python" / "python.exe"), str(root / "lezard.py"), "ollama-install"],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            exe = await asyncio.to_thread(ollama_executable)
            if code or not exe:
                raise VirtualKeyError(502, "Ollama n'a pas pu être installé : voir data\\logs\\service.log.")
        if not running:
            ready_job["step"] = "Démarrage d'Ollama…"
            logs = Path(os.getenv("GATEWAY_LOG_DIR", "data/logs"))
            if not await start_ollama(exe, gw.client, logs):
                raise VirtualKeyError(504, f"Ollama ne répond pas après son lancement : voir {logs / 'ollama.log'}.")
        if not running:
            await gw.detect(force=True)  # Ollama vient de démarrer : relevé neuf, pas celui d'avant
        o = await this_pc_ollama(gw)
        if o["target"]:
            return o["target"].name
        found = next((s for s in await find_servers(gw.client, gw.instance_id) if s["server"] == "ollama"), None)
        if not found:
            raise VirtualKeyError(504, "Ollama est démarré mais ne répond pas encore : réessayez dans un instant.")
        known = {p.cfg.base_url.rstrip("/"): name for name, p in gw.providers.items() if not p.cfg.owner}
        if found["base_url"].rstrip("/") in known:  # déjà ajouté, il vient de démarrer
            return known[found["base_url"].rstrip("/")]
        ready_job["step"] = "Ajout d'Ollama à Lezard IA…"
        taken = set(gw.providers) | {"auto"}
        name = validate_name(free_name("ollama", taken), taken)
        holder["store"].add_custom_provider(name, "openai", found["base_url"], "local")
        gw.add_provider(ProviderConfig(name, "openai", found["base_url"], "local"))
        await gw.detect(force=True)  # sonde neuve : la page voit aussitôt Ollama et ses modèles
        return name

    @router.get("/v1/admin/ollama/ready")
    async def admin_ollama_ready_status(request: Request):
        admin(request)
        return {k: v for k, v in ready_job.items() if k != "task"}

    @router.post("/v1/admin/ollama/ready", status_code=202)
    async def admin_ollama_ready(request: Request):
        """Un clic sur « Installer » d'un modèle sans Ollama : Ollama d'abord (installé, démarré, ajouté), suivi par
        la page (GET), puis le modèle par /v1/admin/ollama/pull avec le fournisseur obtenu."""
        admin(request)
        if hosted_instance():
            raise VirtualKeyError(409, "Instance hébergée : impossible d'installer un programme sur votre PC.")
        if ready_job["state"] == "running":
            return {k: v for k, v in ready_job.items() if k != "task"}
        request.state.audit = {"target": "ollama"}  # installation d'un programme : journal d'audit (20.2)

        async def job() -> None:
            try:
                ready_job.update(state="done", provider=await make_ollama_ready(), step=None)
            except VirtualKeyError as e:
                ready_job.update(state="error", step=None, error=e.message)
            except Exception as e:  # jamais une tâche de fond perdue sans trace
                log.exception("Ollama prêt")
                ready_job.update(
                    state="error", step=None, error=f"Ollama n'a pas pu être préparé ({type(e).__name__})."
                )

        ready_job.update(state="running", step="Recherche d'Ollama sur ce PC…", provider=None, error=None)
        ready_job["task"] = asyncio.create_task(job())
        return {k: v for k, v in ready_job.items() if k != "task"}

    MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")

    def ollama_target(provider: str, model: str) -> Provider:
        p = _provider_or_404(provider)
        if not is_ollama(p):
            raise VirtualKeyError(400, f"'{provider}' n'est pas un serveur Ollama.")
        if not isinstance(model, str) or not MODEL_NAME.match(model):
            raise VirtualKeyError(400, "Nom de modèle invalide (ex. llama3.1:8b).")
        return p

    @router.post("/v1/admin/ollama/pull")
    async def ollama_pull(request: Request):
        """Télécharge un modèle dans Ollama ; progression en NDJSON ({status, completed, total})."""
        admin(request)
        body = await json_object(request)
        p = ollama_target(body.get("provider") or "ollama", body.get("model"))
        request.state.audit = {"target": body.get("model"), "detail": {"provider": p.name}}  # 20.2
        if is_this_machine(p.cfg.base_url):  # 14.4 : jamais un téléchargement qui remplit le disque
            disk, need = await asyncio.to_thread(disk_info), disk_needed(body["model"])
            if disk["free"] is not None and disk["free"] < need:
                known = find(body["model"])
                raise VirtualKeyError(
                    507,
                    f"Espace disque insuffisant dans {disk['path']} : "
                    f"{disk['free'] / 1e9:.1f} Go libres, {need / 1e9:.1f} Go nécessaires"
                    f"{'' if known else ' au minimum (taille du modèle inconnue)'}.",
                )
        client: httpx.AsyncClient = holder["gw"].client

        async def progress():
            try:
                async with client.stream(
                    "POST", ollama_root(p) + "/api/pull", json={"model": body["model"], "stream": True}, timeout=None
                ) as r:
                    if r.status_code >= 400:
                        text = (await r.aread()).decode(errors="replace")[:300]
                        yield json.dumps({"error": f"Ollama : {text}"}) + "\n"
                        return
                    async for line in r.aiter_lines():
                        if line.strip():
                            yield line + "\n"
            except httpx.HTTPError as e:
                yield json.dumps({"error": f"Ollama injoignable : {type(e).__name__}"}) + "\n"
                return
            await holder["gw"].detect(force=True)  # le nouveau modèle devient routable
            yield json.dumps({"status": "done"}) + "\n"

        return StreamingResponse(progress(), media_type="application/x-ndjson")

    @router.delete("/v1/admin/ollama/models/{model:path}")
    async def ollama_delete(model: str, request: Request, provider: str = "ollama"):
        admin(request)
        p = ollama_target(provider, model)
        try:
            r = await holder["gw"].client.request(
                "DELETE", ollama_root(p) + "/api/delete", json={"model": model}, timeout=30
            )
        except httpx.HTTPError as e:
            raise ProviderError(502, f"Ollama injoignable : {type(e).__name__}", provider)
        if r.status_code == 404:
            raise VirtualKeyError(404, f"Modèle '{model}' absent d'Ollama.")
        if r.status_code >= 400:
            raise ProviderError(502, f"Ollama : {r.text[:300]}", provider)
        await holder["gw"].detect(force=True)
        return {"model": model, "deleted": True}

    return router
