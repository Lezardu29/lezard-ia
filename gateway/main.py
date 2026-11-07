"""API Gateway unifiée (tâche 1.1) : assemblage de l'application FastAPI.

Le moteur (sélection, exécution, fallback) est dans gateway/core.py et les routes, par domaine, dans gateway/api/
(10.8). Ce module crée l'application : état partagé, cycle de vie, gestionnaires d'erreurs, middlewares, puis
inclut les routeurs dans l'ordre d'origine des routes.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from . import __version__
from .alerts import AlertEngine
from . import auto_tunnel, cloudflared, edition, mcp, services, sources
from .license import LicenseError
from . import audit
from .backup import BackupManager, apply_pending_restore, auto_enabled as backup_auto
from .conversations import Conversations, retention_days
from .body_limit import BodyLimitMiddleware
from .bridge import BridgeHub, BridgeTransport
from .cache import ResponseCache
from .config import ProviderConfig, bind_address, hosted_instance, lan_mode, load_providers, preset
from .counters import make_counters
from .crypto import Cipher, CryptoError
from .fx import FxRates, auto_refresh_enabled as fx_auto_refresh
from .limits import AuthThrottle, KeyLimiter
from .model_catalog import budget
from .metrics import MetricsRecorder
from .plans import PlanResolver, account_identity
from .pricing import PricingEngine, auto_refresh_enabled as pricing_auto_refresh
from .provider_rules import ProviderGuard
from .providers import local_tts, local_whisper
from .providers.base import GATEWAY_HEADER, instance_tag
from .providers.local_tts import LocalTtsProvider
from .providers.local_whisper import LocalWhisperProvider
from .remote import RemoteAccess, TunnelError
from .store import DocumentStore, default_db_path
from .tls import tls_port
from .totp import AdminTotp
from .vectors import VectorIndex
from .updates import UpdateChecker, check_enabled as update_check_enabled
from .api import (
    access,
    account,
    admin,
    alerts,
    audio,
    audit_log,
    backups,
    bridges,
    chat,
    context,
    conversations,
    documents,
    embeddings,
    errors,
    images,
    keys,
    local_gpu,
    marketplace,
    metrics,
    named_accounts,
    pages,
    site_account,
    supervision,
    totp,
    ui_packs,
    updates,
)
from .accounts import Accounts
from .supervision import Prometheus, Supervisor
from .supervision import interval as supervision_interval
from .core import Gateway, _err, lan_urls, load_credentials, log, secret_problems

# ordre d'inclusion = ordre d'origine des routes (une route plus haut est essayée avant)


def rediscover_interval() -> float:
    return max(0.05, float(os.getenv("REDISCOVER_INTERVAL", "15")))


def history_days(name: str, default: float) -> float | None:
    """Durée de conservation (jours) : GATEWAY_METRICS_DAYS (mesures des requêtes, 730), GATEWAY_NOTIFICATION_DAYS
    (notifications lues, 180) ; 0 = sans limite (None)."""
    try:
        days = float(os.getenv(name, str(default)) or default)
    except ValueError:
        days = default
    return days if days > 0 else None


def hardware_probe() -> bool:
    """Relevé du matériel en tâche de fond (choix du modèle d'embeddings) ; coupé pendant les tests."""
    return os.getenv("HARDWARE_PROBE", "true").lower() not in ("0", "false", "no")


ROUTERS = (
    chat,
    site_account,
    audio,
    embeddings,
    images,
    keys,
    admin,
    backups,
    metrics,
    bridges,
    alerts,
    access,
    account,
    conversations,
    totp,
    audit_log,
    updates,
    local_gpu,
    named_accounts,
    supervision,
    documents,
    marketplace,
    ui_packs,
    pages,
)


def create_app(
    configs: list[ProviderConfig] | None = None,
    client: httpx.AsyncClient | None = None,
    plans: PlanResolver | None = None,
    store: DocumentStore | None = None,
    cache: ResponseCache | None = None,
    clock=None,
    cipher: Cipher | None = None,
    remote: RemoteAccess | None = None,
) -> FastAPI:
    holder: dict = {
        "remote": remote or RemoteAccess(),
        "remote_anonymous": False,
        "auth": AuthThrottle(clock or time.time),
    }  # anti force brute (12.3)
    shared = context.build(holder)  # fonctions communes aux routes (api/context.py)
    auto_connect = shared.auto_connect

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        c = client or httpx.AsyncClient()
        holder["gw"] = Gateway(configs if configs is not None else load_providers(), c, plans=plans, cache=cache)
        problems = secret_problems(holder["gw"].plans)  # 9.2, AVANT de créer un fichier de clé
        if problems and hosted_instance():
            raise RuntimeError(
                "Démarrage refusé (instance hébergée, GATEWAY_HOSTED=true) :\n- " + "\n- ".join(problems)
            )
        for p in problems:
            if "absente" not in p:  # PC personnel : défauts normaux tolérés
                log.warning("Secret faible : %s", p)
        if lan_mode():  # réseau local (12.1) : même garde-fou qu'un tunnel
            if weak := holder["gw"].plans.weak_tokens():
                raise RuntimeError(
                    f"Mode réseau local refusé (GATEWAY_BIND={bind_address()}) : identifiant(s) "
                    f"devinable(s) ({', '.join(weak)}). Remplacez-les par des jetons aléatoires "
                    "(relancez lancer.ps1 pour régénérer le compte de test)."
                )
            log.warning(
                "Mode réseau local : gateway joignable sur %s ; clé requise depuis les autres appareils.",
                ", ".join(lan_urls(holder["remote"].port)) or bind_address(),
            )
            if not tls_port():  # 12.7 : clés et conversations en clair
                log.warning(
                    "Réseau local en HTTP NON CHIFFRÉ : clés et conversations lisibles par les autres "
                    "appareils du Wi-Fi. Préférez lancer.ps1 -Lan -Https (GATEWAY_TLS_PORT) ou Tailscale."
                )
        # 18.2 : restauration préparée depuis l'admin ou la ligne de commande, appliquée AVANT d'ouvrir la base
        restored = apply_pending_restore(default_db_path()) if store is None else None
        holder["store"] = store or DocumentStore()
        holder["instance"] = holder["gw"].instance_id = holder["store"].instance_id()
        try:  # licence signée (30.4) : collée dans « Accès par le site », sinon GATEWAY_LICENSE
            token = (holder["store"].get_setting("license") or {}).get("token") or os.getenv("GATEWAY_LICENSE")
            if edition.load_license(token, holder["instance"]):
                log.warning("Licence d'accès par le site : %s.", edition.license_info()["license"]["status"])
        except LicenseError as e:
            log.error("Licence refusée, pas d'accès par le site : %s", e)
        holder["backups"] = BackupManager(holder["store"])
        if restored:
            holder["store"].set_setting("last_restore", restored)
            if holder["gw"].plans.master_key:
                holder["store"].add_notification(
                    account_identity(holder["gw"].plans.master_key),
                    "info",
                    "Sauvegarde restaurée",
                    f"La base a été remplacée par la sauvegarde « {restored['source']} ». L'état d'avant est "
                    f"conservé dans « {restored['safety_backup']} » (section Sauvegardes).",
                    {"event": "backup.restored", **restored},
                )

        def url_changed(old: str | None, new: str | None) -> None:
            """Tunnel relancé avec une autre adresse (12.4) : prévenir l'admin, les accès distribués sont cassés."""
            master = holder["gw"].plans.master_key
            if master:
                holder["store"].add_notification(
                    account_identity(master),
                    "warning",
                    "Adresse d'accès distant modifiée",
                    f"Le tunnel a redémarré avec une nouvelle adresse : {new} (avant : {old}). Donnez-la aux "
                    "appareils et clients qui utilisaient l'ancienne. Pour une adresse fixe : tunnel nommé "
                    "Cloudflare ou domaine ngrok (panneau Accès distant).",
                    {"event": "remote.url_changed", "old": old, "new": new},
                )

        holder["remote"].on_url_change = url_changed
        counters = holder["counters"] = make_counters(holder["store"])  # partagés : SQLite ou Redis (10.3)
        holder["auth"].counters = counters
        holder["gw"].limiter = KeyLimiter(holder["store"], clock or time.time, counters)
        holder["gw"].guard = ProviderGuard(holder["store"], clock or time.time, counters)  # restrictions des IA
        # files d'attente des serveurs locaux, PC occupé, keep_alive d'Ollama (18.4)
        holder["gw"].local.attach(holder["store"])
        holder["gw"].embedding_choice.attach(holder["store"])  # modèle d'embeddings fixé ou automatique
        holder["gw"].stt_choice.attach(holder["store"])  # modèles de dictée et de lecture vocale, idem
        holder["gw"].tts_choice.attach(holder["store"])
        holder["gw"].local.on_change = lambda on, state: audit.record_system(
            holder["store"],
            "gpu.busy_on" if on else "gpu.busy_off",
            {"source": state.get("source"), "until": state.get("until")},
        )
        holder["gw"].local.on_eco_change = lambda on, state: audit.record_system(holder["store"], "gpu.eco_off", {})
        holder["gw"].registry.free_enabled = set(  # offres gratuites (13.8)
            (holder["store"].get_setting("free_tiers") or {}).get("providers", [])
        )
        holder["cipher"] = cipher or Cipher()
        for cp in holder["store"].list_custom_providers():  # ajoutés depuis la page Modèles
            if cp["name"] not in holder["gw"].providers:
                pre = preset(cp["name"])  # fournisseur proposé : sa clé .env reste liée
                holder["gw"].add_provider(
                    ProviderConfig(cp["name"], cp["kind"], cp["base_url"], cp["hosting"], pre.key_env if pre else None)
                )
        if local_whisper.enabled() and local_whisper.NAME not in holder["gw"].providers:
            whisper = LocalWhisperProvider(c)  # dictée transcrite sur ce PC (13.9), taille choisie pour ce PC
            holder["gw"].stt_choice.builtin = whisper
            holder["gw"]._gated(whisper)  # file d'attente et PC occupé, comme Ollama (18.4)
            whisper.preload()
        if local_tts.enabled() and local_tts.NAME not in holder["gw"].providers:
            tts = LocalTtsProvider(c)  # lecture vocale sur ce PC (19.4)
            holder["gw"]._gated(tts)
            tts.preload()
        load_credentials(holder["gw"], holder["store"], holder["cipher"])
        holder["conversations"] = Conversations(holder["store"], holder["cipher"])  # 19.1
        holder["accounts"] = holder["gw"].plans.accounts = Accounts(holder["store"])  # comptes nommés (20.3)
        holder["totp"] = AdminTotp(holder["store"], holder["cipher"], clock or time.time)  # 20.1
        holder["gw"].metrics = MetricsRecorder(holder["store"])  # capture asynchrone (6.1)
        holder["alerts"] = AlertEngine(holder["store"], c)  # alertes budgétaires (6.5)
        # supervision (18.5) : compteurs de /metrics et alertes d'exploitation (serveur local tombé, disque)
        holder["prometheus"] = Prometheus()
        holder["gw"].metrics.prometheus = holder["prometheus"]
        holder["gw"].health.on_failure = holder["prometheus"].provider_failure
        master = holder["gw"].plans.master_key
        holder["supervisor"] = Supervisor(
            holder["gw"], holder["store"], holder["alerts"], account_identity(master) if master else None
        )
        alert_tasks: set[asyncio.Task] = set()

        async def on_written(rows: list[dict]) -> None:
            # tâche séparée : un webhook lent ne retarde ni l'écriture ni la lecture des métriques
            owners = {r["owner"] for r in rows if r.get("status") == "ok"}
            keys = {r["key_id"] for r in rows if r.get("status") == "ok" and r.get("key_id")}
            if owners:
                t = asyncio.create_task(holder["alerts"].evaluate(owners, keys))
                alert_tasks.add(t)
                t.add_done_callback(alert_tasks.discard)

        holder["gw"].metrics.on_written = on_written
        holder["alert_tasks"] = alert_tasks
        holder["gw"].metrics.start()
        holder["fx"] = FxRates(holder["store"], c)  # taux USD->EUR actualisé (6.1)
        fx_task = asyncio.create_task(holder["fx"].run()) if fx_auto_refresh() else None
        holder["pricing"] = PricingEngine(holder["gw"].registry, holder["store"], c)  # grille dynamique (6.2)
        unpriced_tasks: set[asyncio.Task] = set()

        def on_unpriced(ref: str) -> None:
            if pricing_auto_refresh() and holder["pricing"].wants_import(ref):
                t = asyncio.get_running_loop().create_task(holder["pricing"].import_for_unknown(holder["gw"]))
                unpriced_tasks.add(t)  # référence gardée jusqu'à la fin
                t.add_done_callback(unpriced_tasks.discard)

        holder["gw"].on_unpriced = on_unpriced
        holder["hub"] = BridgeHub()  # ponts vers les modèles locaux (7.4)
        holder["bridge_client"] = httpx.AsyncClient(transport=BridgeTransport(holder["hub"]))
        for b in holder["store"].list_bridges():
            holder["gw"].add_bridge(b["name"], b["owner"], holder["bridge_client"])
        if hosted_instance() and not holder["gw"].plans.require_key:
            log.warning("GATEWAY_HOSTED=true sans GATEWAY_REQUIRE_KEY=true : les requêtes anonymes sont acceptées.")
        if weak := holder["gw"].plans.weak_tokens():
            log.warning("Identifiant(s) devinable(s) (%s) : l'accès distant public sera refusé.", ", ".join(weak))
        await holder["gw"].detect()
        # recherche hybride (17.1) : vectorisation des documents en tâche de fond, par un modèle local
        holder["vectors"] = VectorIndex(holder["gw"], holder["store"])
        holder["vectors"].start()

        async def vector_refresh():
            while True:
                try:
                    holder["vectors"].refresh()  # nouveaux documents, ou modèle d'embeddings changé
                except Exception:
                    log.exception("Recherche hybride : relance de la vectorisation impossible")
                await asyncio.sleep(300)

        vector_task = asyncio.create_task(vector_refresh())

        async def rediscover():
            """Serveur local absent (Ollama encore en démarrage ou en mise à jour à l'allumage du PC, arrêté puis
            relancé…) : resondé toutes les REDISCOVER_INTERVAL secondes, et routable dès qu'il répond — sans attendre
            qu'on ouvre l'administration. Rien n'est sondé tant que tous les serveurs locaux répondent."""
            while True:
                await asyncio.sleep(rediscover_interval())
                try:
                    gw = holder["gw"]
                    if gw.missing_local():
                        await gw.detect(force=True)
                        if not gw.missing_local():
                            log.warning("Serveurs locaux de nouveau détectés : leurs modèles sont routables.")
                            holder["vectors"].refresh()  # modèle d'embeddings revenu : documents à vectoriser
                except Exception:
                    log.exception("Détection des serveurs locaux impossible")

        rediscover_task = asyncio.create_task(rediscover())

        async def hardware_refresh():  # mémoire de ce PC : classe les modèles d'embeddings (tient en VRAM ?)
            while True:
                try:
                    gw = holder["gw"]
                    hw = await gw.hardware.snapshot(list(gw.providers.values()), gw.client)
                    gw.embedding_choice.budget = gw.stt_choice.budget = gw.tts_choice.budget = budget(hw)
                    holder["vectors"].refresh()
                except Exception:
                    log.exception("Matériel : relevé impossible")
                await asyncio.sleep(600)

        hw_task = asyncio.create_task(hardware_refresh()) if hardware_probe() else None
        # version installée ; nouvelle version signalée si GATEWAY_UPDATE_CHECK=on (18.3), jamais installée d'ici
        holder["updates"] = UpdateChecker(holder["store"], backups=holder["backups"])
        update_task = asyncio.create_task(holder["updates"].run()) if update_check_enabled() else None
        price_task = asyncio.create_task(holder["pricing"].run(holder["gw"])) if pricing_auto_refresh() else None
        # sauvegarde quotidienne (18.2) : base ouverte ici seulement (une base fournie appartient à l'appelant)
        backup_task = (
            asyncio.create_task(holder["backups"].run())
            if store is None and holder["backups"].available and backup_auto()
            else None
        )

        async def purge_conversations():
            # conservation limitée (19.1) : conversations sans activité depuis GATEWAY_CONVERSATION_DAYS jours
            while True:
                try:
                    n = await asyncio.to_thread(
                        holder["store"].purge_conversations, time.time() - retention_days() * 86400
                    )
                    if n:
                        log.info("%d conversation(s) supprimée(s) (conservation : %g jours)", n, retention_days())
                except Exception:
                    log.exception("Purge des conversations impossible")
                await asyncio.sleep(3600)

        purge_task = asyncio.create_task(purge_conversations()) if retention_days() else None

        # journal d'audit (20.2) : mode réseau local changé depuis le dernier démarrage ; conservation limitée
        lan = {"enabled": lan_mode(), "bind": bind_address()}
        previous = holder["store"].get_setting("audit_lan")
        if previous is not None and previous.get("enabled") != lan["enabled"] or previous is None and lan["enabled"]:
            audit.record_system(holder["store"], "lan.enabled" if lan["enabled"] else "lan.disabled", lan)
        holder["store"].set_setting("audit_lan", lan)

        async def purge_audit():
            while True:
                try:
                    days = audit.retention_days(holder["store"])
                    if days:
                        n = await asyncio.to_thread(holder["store"].purge_audit, time.time() - days * 86400)
                        if n:
                            log.info("Journal d'audit : %d entrée(s) de plus de %g jours supprimée(s)", n, days)
                    # captures d'écran du contrôle du PC (21.4) : données sensibles, gardées moins longtemps
                    keep = audit.capture_days()
                    n = await asyncio.to_thread(holder["store"].purge_captures, time.time() - keep * 86400)
                    if n:
                        log.info("Journal d'audit : %d capture(s) d'écran de plus de %g jours supprimée(s)", n, keep)
                    # mesures des requêtes, alertes déclenchées, notifications lues : la base ne grossit plus sans fin
                    metrics, notes = (
                        history_days("GATEWAY_METRICS_DAYS", 730),
                        history_days("GATEWAY_NOTIFICATION_DAYS", 180),
                    )
                    now = time.time()
                    done = await asyncio.to_thread(
                        holder["store"].purge_history,
                        now - metrics * 86400 if metrics else None,
                        now - notes * 86400 if notes else None,
                    )
                    if any(done.values()):
                        log.info("Historique allégé : %s", ", ".join(f"{k} {v}" for k, v in done.items() if v))
                except Exception:
                    log.exception("Purge du journal d'audit impossible")
                await asyncio.sleep(3600)

        audit_task = asyncio.create_task(purge_audit())
        gpu_task = asyncio.create_task(holder["gw"].local.run())  # plages horaires « PC occupé » (18.4)

        async def supervise():
            await asyncio.sleep(supervision_interval())  # premier passage après un intervalle : détection faite
            await holder["supervisor"].run()

        supervision_task = asyncio.create_task(supervise())

        def saved_tunnel(managed: dict | None = None) -> dict | None:
            """Tunnel géré gardé sur ce PC, jeton chiffré : l'accès à distance repart même si le serveur central est
            injoignable. Effacé quand le serveur le refuse ou en version gratuite ; Cloudflare coupe de toute façon
            un tunnel supprimé par le serveur central (fin d'abonnement)."""
            store, cipher = holder["store"], holder["cipher"]
            if managed is not None:
                sealed = {"hostname": managed["hostname"], "token": cipher.encrypt(managed["token"], "managed-tunnel")}
                store.set_setting("managed_tunnel", sealed)
                return managed
            saved = store.get_setting("managed_tunnel") or {}
            try:
                return {"hostname": saved["hostname"], "token": cipher.decrypt(saved["token"], "managed-tunnel")}
            except (KeyError, TypeError, CryptoError):
                return None

        async def managed_remote() -> None:
            """Accès à distance géré (31.1) : un PC avec licence sans tunnel configuré à la main (REMOTE_ACCESS) reçoit son
            tunnel du serveur central ; repassé en Gratuit, le tunnel géré est arrêté."""
            remote = holder["remote"]
            if os.getenv("REMOTE_ACCESS", "").strip():
                return  # tunnel du propriétaire (ex. ia.votre-domaine.fr) : jamais remplacé
            auto = bool((holder["store"].get_setting("managed_tunnel") or {}).get("auto"))
            if not edition.web_access() and not auto:  # licence finie : le tunnel géré par le serveur central s'arrête
                if holder["store"].get_setting("managed_tunnel"):
                    holder["store"].set_setting("managed_tunnel", {})
                if remote.active and holder.get("managed_tunnel"):
                    await remote.stop()
                    holder["managed_tunnel"] = None
                    log.warning("Accès à distance géré arrêté : version gratuite.")
                return
            if remote.active:
                return
            if auto:  # accès à distance automatique (lezard-ia.fr) : cloudflared retéléchargé s'il a disparu
                try:
                    await cloudflared.ensure(holder["gw"].client)
                except cloudflared.CloudflaredError as e:
                    log.error("Accès à distance automatique non démarré : %s", e)
                    return
                if await auto_tunnel.forget_if_deleted(
                    holder["gw"].client, holder["store"], holder["cipher"], holder["instance"], remote.port
                ):
                    log.warning(
                        "Adresse fixe supprimée par lezard-ia.fr (aucune connexion depuis %d jours) : « Créer mon "
                        "compte web » en crée une nouvelle.",
                        auto_tunnel.IDLE_DAYS,
                    )
                    return
            if not remote.available().get("cloudflare"):
                return
            managed = None if auto else await edition.managed_tunnel(holder["gw"].client, remote.port)
            if managed is False:  # refusé par le serveur central : on oublie le tunnel gardé
                holder["store"].set_setting("managed_tunnel", {})
                return
            managed = saved_tunnel(managed) if managed else saved_tunnel()
            if managed:
                try:
                    status = await shared.start_remote("cloudflare", None, False, managed)
                    holder["managed_tunnel"] = managed["hostname"]
                    log.warning("Accès à distance géré : %s", status["public_url"])
                except TunnelError as e:
                    log.error("Accès à distance géré non démarré : %s", e.message)

        holder["managed_remote"] = managed_remote  # aussi après l'activation d'une licence (api/admin.py)

        async def renew_license():  # licence renouvelée par le serveur central avant sa fin (30.4)
            await asyncio.sleep(5)  # après auto_connect : un tunnel du propriétaire est prioritaire
            while True:
                try:
                    token = await edition.renew(holder["gw"].client, holder["instance"])
                    if token:
                        holder["store"].set_setting("license", {"token": token})
                        log.warning(
                            "Licence d'accès par le site renouvelée : %s.", edition.license_info()["license"]["status"]
                        )
                    await managed_remote()
                except Exception:
                    log.exception("Renouvellement de la licence ou accès à distance géré impossible")
                await asyncio.sleep(6 * 3600)

        license_task = asyncio.create_task(renew_license())
        connect = asyncio.create_task(auto_connect())  # n'empêche pas le démarrage (5.6)
        sources.load_nextcloud(holder["store"], holder["cipher"])  # Nextcloud saisi dans la page (sinon .env)
        sources.register_sources()  # Paperless-ngx, Nextcloud : outils des sources configurées
        # serveurs MCP déclarés dans data/mcp.json ; une clé de service (page Modèles) = un serveur de plus
        mcp_task = asyncio.create_task(mcp.load(lambda: services.server_configs(holder["store"], holder["cipher"])))
        yield
        connect.cancel()
        mcp_task.cancel()
        await mcp.stop()
        for task in (
            fx_task,
            price_task,
            backup_task,
            purge_task,
            audit_task,
            vector_task,
            rediscover_task,
            hw_task,
            update_task,
            gpu_task,
            supervision_task,
            license_task,
        ):
            if task:
                task.cancel()
        await holder["remote"].stop()  # ne jamais laisser un tunnel orphelin
        await holder["vectors"].stop()
        await holder["gw"].metrics.stop()  # écrit les dernières métriques
        await holder["bridge_client"].aclose()
        if client is None:
            await c.aclose()
        if store is None:  # base ouverte ici : fermée ici (une base fournie
            holder["store"].close()  # appartient à l'appelant, ex. réutilisée par un test)

    # /docs et /openapi.json servies par api/access.py, avec contrôle d'accès (9.6) ; /redoc supprimé
    app = FastAPI(
        title="Smart AI Gateway",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    errors.register(app)

    def tunnel_scheme(request: Request) -> str | None:
        """Protocole du visiteur d'un tunnel (http / https), None hors tunnel. CF-Visitor : posé par Cloudflare ;
        X-Forwarded-Proto : ngrok, Tailscale. Pas de test sur l'adresse du pair : uvicorn (proxy_headers) l'a déjà
        remplacée par celle du visiteur. Un appareil qui forgerait ces en-têtes ne fait que se rediriger lui-même."""
        if not (holder["remote"].active or shared.unmanaged_proxy(request)):
            return None
        visitor = request.headers.get("cf-visitor", "")
        if visitor:
            return "https" if '"https"' in visitor.replace(" ", "") else "http"
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        return proto or None

    @app.middleware("http")
    async def https_through_tunnel(request: Request, call_next):
        """Tunnel aussi servi en HTTP simple (Cloudflare sans « Always Use HTTPS ») : clé et conversations
        circuleraient en clair jusqu'au tunnel. Pages redirigées vers HTTPS, le reste refusé ; HSTS sinon."""
        scheme = tunnel_scheme(request)
        if scheme == "http":
            if request.method in ("GET", "HEAD") and not request.url.path.startswith("/v1/"):
                return RedirectResponse(str(request.url.replace(scheme="https", port=None)), status_code=308)
            return _err(
                403,
                "HTTPS obligatoire à travers l'accès distant : utilisez l'adresse https://. Une clé envoyée en "
                "HTTP a circulé en clair : faites-la tourner.",
                "remote",
            )
        response = await call_next(request)
        if scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.middleware("http")
    async def capture_errors(request: Request, call_next):
        response = await call_next(request)
        if response.status_code >= 400 and request.url.path in ("/v1/chat/completions", "/v1/messages"):
            ctx, gw = getattr(request.state, "gw_ctx", None), holder.get("gw")
            if ctx and gw and gw.metrics:  # identité connue : l'échec est imputé à la clé
                gw.metrics.capture(
                    {
                        "owner": ctx.identity,
                        "key_id": (ctx.key or {}).get("id"),
                        "plan": ctx.plan,
                        "endpoint": "messages" if request.url.path.endswith("messages") else "chat",
                        "status": "error",
                        "error_code": response.status_code,
                        "model": getattr(request.state, "gw_model", None),
                    }
                )
        if holder.get("instance"):  # permet de se reconnaître parmi les serveurs détectés
            response.headers[GATEWAY_HEADER] = instance_tag(holder["instance"])
        return response

    for module in ROUTERS:
        app.include_router(module.build(app, shared))

    app.add_middleware(audit.AuditMiddleware, holder=holder, shared=shared)  # actions sensibles (20.2)
    app.add_middleware(BodyLimitMiddleware)  # ajouté en dernier = le plus externe (9.1)
    return app


app = create_app()
