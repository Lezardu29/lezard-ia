"""API Gateway unifiée (tâche 1.1) : /v1/chat/completions (OpenAI) et /v1/messages (Anthropic).

Pipeline d'une requête : plan (2.4) -> classification (2.1) -> effort (2.3) -> routage (2.2)
-> exécution mono-agent ou orchestration multi-agents (2.4) -> métadonnées `x_gateway`.

Moteur extrait de main.py (10.8) : sélection, exécution, fallback, métadonnées. Les routes HTTP sont dans
gateway/api/, l'assemblage de l'application dans gateway/main.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from collections.abc import AsyncIterator, Iterator

import httpx
from fastapi.responses import JSONResponse

from . import crew, local_context, orchestrator, splitter, structured
from .bridge import base_url as bridge_base_url
from .cache import ResponseCache, StreamRecorder, apply_prompt_cache, replay_chunks, response_key
from .classifier import Classification, classify
from .compression import SummaryCache
from .config import ProviderConfig, bind_address
from .counters import Reservation
from .crypto import Cipher, CryptoError, raw_key
from .embedding_choice import EmbeddingChoice
from .voice_choice import VoiceChoice
from .effort import apply_effort, normalize_effort
from .hardware import HardwareScanner
from .health import HealthTracker, is_retryable, max_attempts
from .limits import KeyLimiter, RateLimited
from .local_gpu import CALLER, LocalBusy, LocalGpu, this_pc
from .metrics import MetricsRecorder
from .permissions import (
    PRIVATE_NONE,
    PermissionDenied,
    cap_effort,
    check_model,
    filter_models,
    resolve_mode,
    resolve_orchestration,
)
from .plans import (
    MIN_TOKEN_LENGTH,
    PlanContext,
    PlanError,
    PlanResolver,
    is_placeholder,
)
from .provider_rules import ProviderGuard
from .providers.base import Provider, ProviderError, is_self
from .providers.openai_compat import OpenAICompatProvider
from .registry import ModelInfo, ModelRegistry, usage_tokens, usd_eur_rate
from .remote import lan_addresses
from .router import LatencyTracker, RouteDecision, RoutingError, candidates, route
from .store import DocumentStore
from .tls import ca_fingerprint, tls_port
from .validation import RequestError

WEB_DIR = Path(__file__).parent / "web"


CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'; "
    "worker-src 'self'; manifest-src 'self'; media-src 'self' blob:"
)  # application installée (13.6) ; lecture vocale (19.4) : son reçu de la gateway, joué depuis un blob

# Aperçu d'une page, d'une image SVG ou d'un diagramme écrits par l'IA (panneau d'aperçu du chat) : leurs styles et
# scripts intégrés s'exécutent, mais dans une origine opaque (sandbox sans allow-same-origin) : ni la clé gardée par
# le navigateur, ni l'API de la gateway. Aucune requête réseau, sauf les bibliothèques des CDN courants.
CDNS = "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com"
PREVIEW_CSP = (
    f"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' {CDNS}; "
    f"style-src 'unsafe-inline' https://fonts.googleapis.com {CDNS}; font-src data: https://fonts.gstatic.com {CDNS}; "
    "img-src data: blob: https:; media-src data: blob: https:; connect-src 'none'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'self'; sandbox allow-scripts"
)


ORCHESTRATION = ("auto", "single", "multi")


log = logging.getLogger("gateway")


def auto_mode(model: str) -> str | None:
    """'auto' -> 'balanced', 'auto/speed' ou 'auto-speed' -> 'speed', sinon None."""
    m = model.lower()
    if m == "auto":
        return ""
    for sep in ("auto/", "auto-"):
        if m.startswith(sep):
            return m[len(sep) :]
    return None


def has_budget(ctx: PlanContext) -> bool:
    return bool((ctx.limits or {}).get("budget"))


def check_priced(ctx: PlanContext, info: ModelInfo) -> None:
    """8.3 : un modèle cloud sans tarif coûterait 0 $ pour la gateway et contournerait le budget de la clé."""
    if has_budget(ctx) and not info.priced:
        raise PermissionDenied(
            403,
            f"Modèle '{info.ref}' sans tarif connu : interdit aux clés ayant un budget, "
            "sa dépense ne pourrait pas être comptée. L'administrateur peut renseigner son "
            f"prix (page Modèles ou PUT /v1/admin/pricing/{info.ref}).",
        )


@dataclass
class Selection:
    provider: Provider
    upstream: str
    info: ModelInfo
    cls: Classification
    effort: str
    payload: dict  # requête upstream (effort appliqué)
    plan: PlanContext
    decision: RouteDecision | None = None
    orchestrate: bool = False
    candidates: list[ModelInfo] = field(default_factory=list)
    fallbacks: list[ModelInfo] = field(default_factory=list)  # chaîne de secours (2.5)
    context: dict | None = None  # documents injectés (3.1)
    project: dict | None = None  # projet / prompt appliqués (3.2)
    prompt_cache: str = "auto"  # politique de prompt caching provider (3.3)
    cache_key: str | None = None  # clé de routage de cache (OpenAI prompt_cache_key)
    response_ttl: int | None = None  # cache de réponses gateway (None = désactivé)
    response_key: str | None = None
    compression: dict | None = None  # rapport de compression de l'historique (3.4)
    endpoint: str = "chat"  # chat | messages (métriques 6.1)
    stream: bool = False
    estimated_usage: bool = False  # usage estimé faute de chiffres du provider (5.5)
    reservation: Reservation | None = None  # consommation estimée réservée jusqu'au règlement (10.4)
    planning: dict | None = None  # découpage tenté mais demande indivisible : son coût s'ajoute (10.5)
    split: splitter.SplitPlan | None = None  # message trop long découpé en map-reduce (13.4)
    # appels partis vers les fournisseurs, réservés dans leurs limites jusqu'au règlement (9.6) ; liste partagée
    # avec les variantes de secours (replace() copie la référence)
    provider_holds: list[Reservation] = field(default_factory=list)

    def release(self) -> None:
        if self.reservation:
            self.reservation.release()
        for r in self.provider_holds:
            r.release()


def provider_class(kind: str):
    """API native d'Anthropic, sinon l'API compatible OpenAI."""
    if kind == "anthropic":
        from .providers.anthropic import AnthropicProvider

        return AnthropicProvider
    return OpenAICompatProvider


class Gateway:
    def __init__(
        self,
        configs: list[ProviderConfig],
        client: httpx.AsyncClient,
        registry: ModelRegistry | None = None,
        plans: PlanResolver | None = None,
        cache: ResponseCache | None = None,
    ):
        self.client = client
        self.registry = registry or ModelRegistry()
        self.plans = plans or PlanResolver()
        self.cache = cache or ResponseCache()
        self.summaries = SummaryCache()
        self.limiter: KeyLimiter | None = None  # branché sur le store au démarrage
        self.metrics: MetricsRecorder | None = None  # capture asynchrone (6.1)
        self.on_unpriced = None  # rappel : modèle cloud utilisé sans tarif connu (10.7)
        self.guard = ProviderGuard()  # restrictions des IA en entrée (store au démarrage)
        self.providers: dict[str, Provider] = {}
        self.local = LocalGpu(self.providers)  # files d'attente, PC occupé, keep_alive (18.4)
        self.hardware = HardwareScanner()  # matériel de ce PC (13.1), relevé à la demande, mis en cache
        self.embedding_choice = EmbeddingChoice()  # catégorie « Embeddings » : le meilleur modèle pour ce PC
        self.embedding_choice.this_pc = lambda name: (
            name in self.providers and this_pc(self.providers[name].cfg.base_url)
        )
        self.stt_choice = VoiceChoice("stt")  # catégories « Dictée » et « Lecture vocale » : idem, pour la voix
        self.tts_choice = VoiceChoice("tts")
        self.stt_choice.this_pc = self.tts_choice.this_pc = self.embedding_choice.this_pc
        for c in configs:
            cls = provider_class(c.kind)
            self._gated(cls(c, client))
        self.discovered: dict[str, list[str]] = {}  # provider -> modèles live
        self.probe_errors: dict[str, str] = {}  # provider -> raison de la dernière détection ratée (page Modèles)
        self.detect_ttl = float(os.getenv("DETECT_TTL", "30"))  # cache de la détection (9.3)
        self._detect_lock = asyncio.Lock()
        self._probed: dict[str, list[str] | None] | None = None
        self._probe_started = 0.0
        # numéro de la dernière sonde lancée : « partie après cet appel » sans comparer d'horloges, car
        # time.monotonic() n'avance que tous les 15,6 ms sous Windows avant Python 3.13 (15.2)
        self._probe_gen = 0
        self.probes = 0  # sondes réellement effectuées (tests, diagnostic)
        self._ctx_probed: set[str] = set()  # modèles locaux dont la fenêtre a été demandée (10.6)
        self.instance_id: str | None = None  # identifiant de cette installation (réponses X-Smart-Gateway)
        self._self_warned: set[str] = set()
        self.latency = LatencyTracker()
        self.health = HealthTracker()

    # ----- détection (1.2) -----
    def missing_local(self) -> list[str]:
        """Serveurs locaux actifs sans modèle détecté (injoignables à la dernière sonde, ou jamais sondés)."""
        return sorted(
            n
            for n, p in self.providers.items()
            if p.hosting == "local" and p.cfg.enabled and not p.cfg.owner and not self.discovered.get(n)
        )

    async def detect(self, force: bool = False, max_age: float | None = None) -> dict[str, dict]:
        """Sonde les providers actifs (locaux ET cloud avec clé) et mémorise leurs modèles.
        9.3 : résultat réutilisé pendant DETECT_TTL secondes (ou `max_age`), et une seule sonde à la fois —
        les appels simultanés attendent celle en cours au lieu d'en lancer d'autres. `force` : sonde
        neuve (après un changement de configuration), mutualisée elle aussi."""
        asked, seen = time.monotonic(), self._probe_gen
        age = self.detect_ttl if max_age is None else max_age
        if not force and self._probed is not None and asked - self._probe_started < age:
            return self._status()
        async with self._detect_lock:
            # une sonde partie APRÈS cet appel vient de finir : son résultat est assez récent
            if self._probed is not None and (
                self._probe_gen > seen or (not force and time.monotonic() - self._probe_started < age)
            ):
                return self._status()
            self._probe_gen += 1
            self._probe_started = time.monotonic()
            active = [p for p in self.providers.values() if p.cfg.enabled]

            async def probe(p: Provider):
                try:
                    models = await p.list_models()
                    self.probe_errors.pop(p.name, None)
                except (httpx.HTTPError, ProviderError) as e:
                    models = None
                    self.probe_errors[p.name] = probe_error(e)
                if is_self(p.served_by, self.instance_id):
                    # ce « serveur » est la gateway elle-même (ex. vLLM par défaut sur le port 8000) : l'utiliser
                    # renverrait chaque requête vers soi, en boucle
                    if p.name not in self._self_warned:
                        self._self_warned.add(p.name)
                        log.warning(
                            "Fournisseur « %s » ignoré : %s est l'adresse de cette gateway.", p.name, p.cfg.base_url
                        )
                    return p.name, None
                return p.name, models

            self._probed = dict(await asyncio.gather(*(probe(p) for p in active)))
            self.discovered = {k: v for k, v in self._probed.items() if v is not None}
            self.probes += 1
            self._announced_prices()
            await self._local_contexts(force)
            return self._status()

    def _announced_prices(self) -> None:
        """Tarifs publiés par les fournisseurs cloud dans /models (OpenRouter…), 10.7 : un modèle détecté n'y reste
        pas « tarif inconnu » en attendant l'import quotidien (qui ne connaît d'ailleurs pas OpenRouter)."""
        for name in self.discovered:
            p = self.providers.get(name)
            if not p or p.hosting != "cloud":
                continue
            for mid, fields in p.prices.items():
                ctx = p.context_lengths.get(mid)
                self.registry.announce(name, mid, {**fields, **({"context_window": ctx} if ctx else {})})

    def too_long(self, info: ModelInfo, cls: Classification, window: int | None = None) -> bool:
        """Le prompt dépasse la fenêtre MESURÉE d'un modèle local (une fenêtre seulement supposée ne bloque pas).
        `window` : fenêtre demandée au serveur pour cette requête (contrôle du PC : _num_ctx)."""
        return info.ref in self.registry.local_context and cls.prompt_tokens > max(info.context_window, window or 0)

    async def _local_contexts(self, force: bool) -> None:
        """Fenêtre réelle des modèles locaux (10.6) : annoncée par /models, sinon demandée au serveur (Ollama,
        LM Studio) — une fois par modèle, ou à chaque détection forcée (page Modèles, changement de config)."""
        if force:
            self._ctx_probed.clear()
        ask = os.getenv("LOCAL_CONTEXT_PROBE", "true").lower() in ("1", "true", "yes")
        jobs = []
        for name, ids in self.discovered.items():
            p = self.providers.get(name)
            if not p or p.hosting != "local":
                continue
            for mid, ctx in p.context_lengths.items():
                self.registry.local_context[f"{name}/{mid}"] = ctx
            todo = [m for m in ids if f"{name}/{m}" not in self._ctx_probed and m not in p.context_lengths]
            # pont : seule l'API d'inférence est relayée ; « builtin:// » : fournisseur intégré, pas de serveur
            if ask and todo and not p.cfg.owner and not p.cfg.base_url.startswith("builtin://"):
                self._ctx_probed.update(f"{name}/{m}" for m in todo)
                caps: dict[str, set[str]] = {}
                jobs.append(
                    (
                        name,
                        caps,
                        local_context.probe(
                            name,
                            p.cfg.base_url,
                            todo,
                            self.client,
                            {"Authorization": f"Bearer {k}"} if (k := p.cfg.resolved_key()) else None,
                            caps,
                        ),
                    )
                )
        for (name, caps, _), found in zip(jobs, await asyncio.gather(*(j for _, _, j in jobs))):
            for mid, ctx in found.items():
                self.registry.local_context[f"{name}/{mid}"] = ctx
            for mid, found_caps in caps.items():  # outils, vision (16.2)
                self.registry.local_caps[f"{name}/{mid}"] = found_caps

    def _status(self) -> dict[str, dict]:
        results, cds = self._probed or {}, self.health.cooldowns()
        return {
            p.name: {
                "hosting": p.hosting,
                "kind": p.cfg.kind,
                "base_url": p.cfg.base_url,
                "enabled": p.cfg.enabled,
                "online": results.get(p.name) is not None,
                "models": results.get(p.name) or [],
                "error": None if results.get(p.name) is not None else self.probe_errors.get(p.name),
                "cooldown": cds.get(p.name),
                "model_cooldowns": {k: v for k, v in cds.items() if k.startswith(p.name + "/")},
            }
            for p in self.providers.values()
        }

    # ----- résolution du modèle -----
    def visible(self, name: str, owner: str | None) -> bool:
        """Un pont vers des modèles locaux (7.4) n'existe que pour le compte qui l'a créé."""
        p = self.providers.get(name)
        return bool(p) and (p.cfg.owner is None or p.cfg.owner == owner)

    async def resolve(self, model: str, owner: str | None = None) -> tuple[Provider, str]:
        if "/" in model:
            prefix, rest = model.split("/", 1)
            p = self.providers.get(prefix)
            if p and self.visible(prefix, owner):
                self._ensure_enabled(p)
                return p, rest
        for attempt in range(2):
            if attempt:  # 2e passage après redétection : au plus une sonde réelle par DETECT_UNKNOWN_INTERVAL (9.3)
                await self.detect(max_age=float(os.getenv("DETECT_UNKNOWN_INTERVAL", "5")))
            for name, models in self.discovered.items():
                if model in models and self.visible(name, owner):
                    return self.providers[name], model
            known = self.registry.find_bare(model)
            if (
                known
                and known.provider in self.providers
                and self.providers[known.provider].cfg.enabled
                and self.visible(known.provider, owner)
            ):
                return self.providers[known.provider], model
        raise ProviderError(404, f"Modèle inconnu : '{model}'. Utilisez 'provider/modèle'.", "gateway")

    def _gated(self, p: Provider) -> Provider:
        """Serveur local, y compris Whisper et Kokoro intégrés : ses appels passent par sa file d'attente (18.4)."""
        if p.hosting == "local" and (isinstance(p, OpenAICompatProvider) or p.cfg.base_url.startswith("builtin://")):
            p.gate = self.local.gate_for(p)
        self.providers[p.name] = p
        return p

    def add_provider(self, cfg: ProviderConfig) -> Provider:
        cls = provider_class(cfg.kind)
        return self._gated(cls(cfg, self.client))

    # ----- ponts vers les modèles locaux (7.4) -----
    def add_bridge(self, name: str, owner: str, client: httpx.AsyncClient) -> None:
        cfg = ProviderConfig(name, "openai", bridge_base_url(name), "local", owner=owner)
        self._gated(OpenAICompatProvider(cfg, client))

    def remove_bridge(self, name: str) -> None:
        self.providers.pop(name, None)
        self.discovered.pop(name, None)

    def _routable(self, ctx: PlanContext) -> list[ModelInfo]:
        """Candidats en ligne, autorisés par le plan, hors cooldown (sauf si tous le sont)."""
        hosting = {n: p.hosting for n, p in self.providers.items()}
        discovered = {n: m for n, m in self.discovered.items() if self.visible(n, ctx.identity)}
        models = candidates(self.registry, discovered, hosting)
        models = filter_models(ctx.permissions, models)  # permissions de la clé (4.2)
        models = self.local.filter(models, ctx.is_admin, ctx.permissions)  # PC occupé (18.4)
        models = self.local.eco_filter(models)  # mode économie : modèles locaux légers
        if has_budget(ctx):  # budget (4.3) : coût calculable requis (8.3)
            models = [m for m in models if m.priced]
        usable = [m for m in models if self.guard.usable(m, ctx.plan, ctx.is_admin)]  # règles fournisseurs
        if models and not usable:
            for m in models:
                self.guard.check(m, ctx.plan, ctx.is_admin)  # relève la raison (403 / 429)
        models = usable
        healthy = [m for m in models if self.health.available(m.ref)]
        return healthy or models

    async def _explicit_fallbacks(
        self,
        refs: list[str],
        primary: ModelInfo,
        cls: Classification,
        ctx: PlanContext,
        mode_hint,
        effort: str,
        json_required: bool,
    ):
        out: list[ModelInfo] = []
        for ref in refs:
            if ref.lower() == "auto":  # alternatives calculées par le routeur
                try:
                    d = route(
                        cls,
                        resolve_mode(ctx.permissions, mode_hint),
                        self._routable(ctx),
                        self.latency,
                        json_required=json_required,
                        effort=effort,
                    )
                except (RoutingError, PermissionDenied, RateLimited, LocalBusy):
                    continue
                by_ref = {m.ref: m for m in self._routable(ctx)}
                out += [by_ref[r] for r in [d.ref] + d.alternatives if r in by_ref]
                continue
            p, mid = await self.resolve(ref, ctx.identity)
            info = self.registry.get(p.name, mid, p.hosting)
            check_model(ctx.permissions, info)
            check_priced(ctx, info)
            if self.guard.usable(info, ctx.plan, ctx.is_admin) and not self.too_long(info, cls):  # sinon ignoré
                out.append(info)
        seen = {primary.ref}
        return [m for m in out if not (m.ref in seen or seen.add(m.ref))]

    async def select(
        self,
        body: dict,
        ctx: PlanContext,
        mode_hint: str | None = None,
        effort_hint: str | None = None,
        orchestration: str | None = None,
        fallbacks: list[str] | str | None = None,
        caching: dict | None = None,
        split: str = "auto",
    ) -> Selection:
        CALLER.set((ctx.identity, ctx.is_admin))  # files d'attente des serveurs locaux (18.4)
        caching = caching or {}
        policy, ckey, ttl = caching.get("policy", "auto"), caching.get("key"), caching.get("ttl")
        cls = classify(body)
        raw_effort = effort_hint or body.get("reasoning_effort")
        effort = cap_effort(
            ctx.permissions,
            normalize_effort(raw_effort, cls),
            explicit=bool(raw_effort) and str(raw_effort).lower() != "auto",
        )
        orchestration = resolve_orchestration(ctx.permissions, orchestration)
        orch = (orchestration or "auto").lower()
        if orch not in ORCHESTRATION:
            raise PlanError(400, f"Orchestration inconnue '{orchestration}'. Valeurs : auto, single, multi.")

        suffix = auto_mode(body["model"])
        rf = (body.get("response_format") or {}).get("type")
        json_required = rf in ("json_object", "json_schema")
        decision, models, chain, split_plan = None, [], [], None
        if suffix is None:
            p, upstream = await self.resolve(body["model"], ctx.identity)
            info = self.registry.get(p.name, upstream, p.hosting)
            check_model(ctx.permissions, info)
            check_priced(ctx, info)
            if not fallbacks:  # PC occupé (18.4) ; avec secours : bascule à l'envoi
                self.local.check_busy(p.name, ctx.is_admin)
            if cls.needs_tools and not info.tools and info.hosting == "local":
                raise RequestError(
                    f"{info.ref} ne gère pas l'appel d'outils (function calling)"
                    + (" d'après son serveur" if info.ref in self.registry.local_caps else "")
                    + " : choisissez un modèle qui le gère (llama3.1, qwen2.5, mistral…) ou model "
                    "« auto »."
                )
            if fallbacks:  # saturé : l'envoi bascule sur un secours (admit, 9.6)
                reason = self.guard.denial(info, ctx.plan, ctx.is_admin)
                if reason:
                    raise PermissionDenied(403, reason)
            else:
                self.guard.check(info, ctx.plan, ctx.is_admin)  # restrictions du fournisseur
            window = body.get("_num_ctx")
            if self.too_long(info, cls, window) and split == "auto" and splitter.splittable(body, cls) is not None:
                split_plan = splitter.make_plan(body, cls, [info])  # 13.4 : le modèle demandé, à la suite
            elif self.too_long(info, cls, window):  # fenêtre locale mesurée (10.6)
                raise RequestError(
                    f"Message trop long pour {info.ref} : environ {cls.prompt_tokens} tokens, pour une fenêtre de "
                    f"{info.context_window} tokens sur ce serveur (au-delà, le modèle ne verrait qu'une partie du "
                    "texte). Utilisez model « auto », un modèle à plus grande fenêtre, ou augmentez la fenêtre du "
                    "serveur (Ollama : OLLAMA_CONTEXT_LENGTH)."
                )
            if fallbacks and not split_plan:  # modèle explicite : fallback seulement si demandé
                refs = [fallbacks] if isinstance(fallbacks, str) else list(fallbacks)
                chain = await self._explicit_fallbacks(refs, info, cls, ctx, mode_hint, effort, json_required)
        else:
            if not self.discovered:
                await self.detect()
            models = self._routable(ctx)
            if not models and self.local.blocks(ctx.is_admin):
                raise self.local.busy_error()  # PC occupé et rien d'autre de permis (18.4)
            if not models and (ctx.permissions or {}).get("private"):
                raise PermissionDenied(503, PRIVATE_NONE)
            if not models and ctx.permissions:
                raise PermissionDenied(403, "Aucun modèle disponible n'est autorisé pour cette clé.")
            mode = resolve_mode(ctx.permissions, suffix or mode_hint)
            try:
                decision = route(cls, mode, models, self.latency, json_required=json_required, effort=effort)
            except RoutingError:  # aucun modèle autorisé n'a une fenêtre suffisante : découpage (13.4)
                workers = (
                    splitter.choose_workers(self, cls, mode, models, json_required, effort)
                    if split == "auto" and splitter.splittable(body, cls) is not None
                    else []
                )
                if not workers:
                    raise
                split_plan = splitter.make_plan(body, cls, workers)
                decision = route(
                    replace(cls, min_context=0),
                    mode,
                    workers[:1],
                    self.latency,
                    json_required=json_required,
                    effort=effort,
                )
                decision.reason += (
                    " · historique réduit"
                    if split_plan.single
                    else f" · message découpé en {len(split_plan.chunks)} parties"
                )
            p, upstream = self.providers[decision.provider], decision.model
            info = self.registry.get(p.name, upstream, p.hosting)
            by_ref = {m.ref: m for m in models}
            chain = [] if split_plan else [by_ref[r] for r in decision.alternatives if r in by_ref]

        # multi-agents si routage auto, sans tools, et requête assez complexe (ou forcé). Gros modèle (CrewAI) :
        # demande complexe seulement, sinon il répond seul ; modèle plus petit (local) : dès qu'elle n'est pas triviale
        complex_enough = crew.worth_experts(cls.complexity, info)
        orchestrate = (
            decision is not None
            and not cls.needs_tools
            and not split_plan
            and not json_required
            and len(models) > 1  # JSON : une seule réponse contrôlée (16.3)
            and (orch == "multi" or (orch == "auto" and complex_enough))
        )
        payload = self.upstream_payload(body, info, effort, policy, ckey)
        rkey = None
        if ttl:  # la clé décrit la demande (avant routage) : un hit évite aussi le routage
            rkey = response_key(
                ctx.identity,
                body["model"],
                body,
                {
                    "effort": effort,
                    "mode": decision.mode if decision else None,
                    "orchestrate": orchestrate,
                    "plan": ctx.plan,
                },
            )
        return Selection(
            p,
            upstream,
            info,
            cls,
            effort,
            payload,
            ctx,
            decision,
            orchestrate,
            models,
            chain[: max_attempts() - 1],
            prompt_cache=policy,
            cache_key=ckey,
            response_ttl=ttl,
            response_key=rkey,
            split=split_plan,
        )

    def upstream_payload(
        self, body: dict, info: ModelInfo, effort: str, policy: str = "auto", key: str | None = None
    ) -> dict:
        """Requête envoyée au provider : effort de raisonnement (2.3) + prompt caching (3.3)."""
        p = self.providers[info.provider]
        payload = self.guard.cap(p.name, {**body, "model": info.id})  # max_tokens du fournisseur
        window = self.registry.local_context.get(info.ref) or 0
        if (
            window > local_context.OLLAMA_DEFAULT_CTX
            and local_context.ollama_server_default() > local_context.OLLAMA_DEFAULT_CTX
            and not payload.get("_num_ctx")
            and local_context.looks_ollama(p.name, p.cfg.base_url)
        ):
            # OLLAMA_CONTEXT_LENGTH agrandi : fenêtre imposée à Ollama à chaque requête. L'application Ollama lance
            # son serveur avec son propre réglage (4 096) et coupait la réponse en silence avant la fenêtre affichée
            payload["_num_ctx"] = window
        payload = apply_effort(payload, info, p.cfg.kind, effort)
        return apply_prompt_cache(payload, info, p.name, p.cfg.kind, policy, key)

    def admit(self, sel: Selection, info: ModelInfo, payload: dict) -> Reservation | None:
        """Réserve l'appel dans les limites de son fournisseur juste avant l'envoi (9.6) ; RateLimited si saturé
        (le fallback passe alors à l'IA suivante). Libéré avec la sélection, ou tout de suite par `unadmit`."""
        prompt = classify(payload).prompt_tokens
        out = payload.get("max_tokens") or payload.get("max_completion_tokens") or sel.cls.max_output_tokens or 500
        out = min(int(out), 4096)  # estimation, pas le plafond demandé
        res = self.guard.admit(info.provider, prompt + out, info.cost_usd(prompt, out))
        if res:
            sel.provider_holds.append(res)
        return res

    @staticmethod
    def unadmit(sel: Selection, res: Reservation | None) -> None:
        """Appel échoué : sa réservation est rendue aussitôt (la suite de la chaîne ne doit pas la voir)."""
        if res:
            res.release()
            sel.provider_holds.remove(res)

    def variants(self, sel: Selection, body: dict) -> Iterator[Selection]:
        """Modèle principal puis modèles de secours, chacun avec la requête adaptée."""
        yield sel
        for info in sel.fallbacks:
            payload = self.upstream_payload(body, info, sel.effort, sel.prompt_cache, sel.cache_key)
            yield replace(sel, provider=self.providers[info.provider], upstream=info.id, info=info, payload=payload)

    def _failed(self, v: Selection, e: ProviderError | RateLimited, attempts: list[dict], last: bool) -> None:
        """Enregistre un échec ; relève l'erreur si on ne peut/doit pas basculer."""
        if isinstance(e, RateLimited):  # limite du fournisseur côté gateway : l'IA n'est pas en panne
            attempts.append({"ref": v.info.ref, "status": 429, "error": e.message[:200]})
            if last and len(attempts) == 1:
                raise e  # seul candidat : 429 avec Retry-After
            if last:
                e = ProviderError(429, e.message, v.info.provider, e.retry_after)
            else:
                return
        else:
            self.health.mark_failure(v.info.ref, e)
            attempts.append({"ref": v.info.ref, "status": e.status, "error": e.message[:200]})
        if not is_retryable(e):
            raise e
        if last:
            tried = ", ".join(f"{a['ref']} ({a['status']})" for a in attempts)
            raise ProviderError(
                e.status,
                f"Tous les modèles ont échoué : {tried}. Dernière erreur : {e.message[:200]}",
                "gateway",
                e.retry_after,
            )

    @staticmethod
    def _fallback_meta(meta: dict, v: Selection, attempts: list[dict]) -> dict:
        if attempts:
            meta["fallback"] = {"used": v.info.ref, "attempts": attempts}
        return meta

    @staticmethod
    def _ensure_enabled(p: Provider):
        if not p.cfg.enabled:
            raise ProviderError(400, f"Provider '{p.name}' désactivé (clé API manquante ?).", p.name)

    def base_meta(self, sel: Selection, usage: dict | None, t0: float) -> dict:
        usd = sel.info.usage_cost(usage)
        meta = {
            "provider": sel.provider.name,
            "model": sel.upstream,
            "hosting": sel.provider.hosting,
            "cost_usd": round(usd, 8),
            "cost_eur": round(usd * usd_eur_rate(), 8),
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }
        _, _, cached, write = usage_tokens(usage)
        if cached or write:
            meta["cache"] = {
                "cached_tokens": cached,
                "cache_write_tokens": write,
                "savings_usd": round(sel.info.cache_savings_usd(usage), 8),
            }
        if not sel.info.priced:
            meta["pricing"] = "unknown"  # modèle cloud sans tarif : coût non fiable (6.2)
        if sel.info.free:
            meta["free_tier"] = True  # offre gratuite officielle (13.8) : 0 €, dans la limite des quotas
        if sel.provider.hosting == "local" and self.local.eco_on():
            meta["eco"] = True  # mode économie : réponse locale peut-être plafonnée
        return meta

    def finish(self, sel: Selection, meta: dict, usage: dict | None, billable: bool = True) -> dict:
        """Complète les métadonnées : plan, effort, routage, coûts et compteurs."""
        u = usage or {}
        i, o = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        meta.update(plan=sel.plan.plan, effort=sel.effort, classification=sel.cls.to_dict())
        if sel.estimated_usage:  # signalé AVANT la capture des métriques
            meta["usage_estimated"] = True
        if sel.plan.key:
            meta["key"] = {"id": sel.plan.key["id"], "name": sel.plan.key["name"]}
        if sel.decision:
            meta["routing"] = sel.decision.to_dict()
        if sel.context:
            meta["documents"] = sel.context
        if sel.project:
            meta["project"] = sel.project
        extra_tokens = 0
        if sel.compression:
            meta["compression"] = sel.compression
            if billable and sel.compression.get("cost_usd"):  # le résumé fait partie du coût
                meta["cost_usd"] = round(meta["cost_usd"] + sel.compression["cost_usd"], 8)
                meta["cost_eur"] = round(meta["cost_usd"] * usd_eur_rate(), 8)
            extra_tokens = sel.compression.get("tokens_used", 0)
        if sel.planning and billable:  # 10.5 : découpage tenté, demande indivisible
            a = sel.planning
            answer = {
                "ref": f"{meta.get('provider')}/{meta.get('model')}",
                "role": "worker",
                "ok": True,
                "prompt_tokens": i,
                "completion_tokens": o,
                "cost_usd": meta["cost_usd"],
                "latency_ms": meta.get("latency_ms"),
                "part": 1,
            }
            if a["ok"]:
                meta["cost_usd"] = round(meta["cost_usd"] + a["cost_usd"], 8)
                meta["cost_eur"] = round(meta["cost_usd"] * usd_eur_rate(), 8)
                extra_tokens += a["prompt_tokens"] + a["completion_tokens"]
            meta["orchestration"] = {"strategy": orchestrator.STRATEGY, "decomposed": False, "agents": [a, answer]}
        if not sel.info.priced and self.on_unpriced:  # tarif inconnu : tenter un import (10.7)
            self.on_unpriced(sel.info.ref)
        real_cost = meta["cost_usd"] if billable else 0.0  # coût réellement payé aux providers
        sel.release()  # l'estimation cède la place à l'usage réel
        if billable and self.limiter:  # compteurs & dépenses de la clé (4.3)
            self.limiter.record(
                sel.plan.key, sel.plan.limits, (u.get("total_tokens") or i + o) + extra_tokens, meta["cost_usd"]
            )
        agents = [a for a in (meta.get("orchestration") or meta.get("split") or {}).get("agents", []) if a.get("ok")]
        if billable:  # débit & dépense par fournisseur
            for a in agents:
                self.guard.record(a["ref"].split("/", 1)[0], a["prompt_tokens"] + a["completion_tokens"], a["cost_usd"])
            if not agents and meta.get("provider"):
                self.guard.record(meta["provider"], (u.get("total_tokens") or i + o) + extra_tokens, real_cost)
        if self.metrics:  # capture asynchrone (6.1)
            self.metrics.capture(self.metric_row(sel, meta, u, real_cost, extra_tokens))
        return meta

    def metric_row(self, sel: Selection, meta: dict, u: dict, real_cost: float, extra_tokens: int) -> dict:
        _, _, cached, write = usage_tokens(u)
        cache = meta.get("cache") or {}
        hit = bool(cache.get("response_hit"))
        rate = usd_eur_rate()
        prompt, completion = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        return {
            "owner": sel.plan.identity,
            "key_id": (sel.plan.key or {}).get("id"),
            "project_id": (sel.project or {}).get("id"),
            "plan": sel.plan.plan,
            "endpoint": sel.endpoint,
            "stream": sel.stream,
            "status": "ok",
            "provider": meta.get("provider"),
            "model": meta.get("model"),
            # « free » : catégorie « cloud gratuit » des FinOps (13.8), distincte du local et du cloud payant
            "hosting": "free" if meta.get("free_tier") else meta.get("hosting"),
            "mode": sel.decision.mode if sel.decision else None,
            "effort": sel.effort,
            "task": sel.cls.task,
            "complexity": sel.cls.complexity,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cached_tokens": cached,
            "cache_write_tokens": write,
            "total_tokens": (u.get("total_tokens") or prompt + completion) + extra_tokens,
            "cost_usd": round(real_cost, 8),
            "billed_usd": meta["cost_usd"],
            "billed_eur": round(meta["cost_usd"] * rate, 8),
            "fx_rate": rate,
            "saved_usd": round(cache.get("saved_usd", 0.0) if hit else cache.get("savings_usd", 0.0), 8),
            "compression_usd": (sel.compression or {}).get("cost_usd"),
            "latency_ms": meta.get("latency_ms"),
            "ttft_ms": meta.get("ttft_ms"),
            "cache_hit": hit,
            "usage_estimated": bool(meta.get("usage_estimated")),
            "fallback_from": ",".join(a["ref"] for a in (meta.get("fallback") or {}).get("attempts", [])) or None,
            "agents": len((meta.get("orchestration") or meta.get("split") or {}).get("agents", [])) or None,
        }

    # ----- cache de réponses (3.3) -----
    def _cache_lookup(self, sel: Selection) -> dict | None:
        if not sel.response_ttl:
            return None
        hit = self.cache.get(sel.response_key, sel.plan.identity)
        if not hit:
            return None
        resp = hit.response
        meta = {
            "provider": hit.meta["provider"],
            "model": hit.meta["model"],
            "hosting": hit.meta["hosting"],
            "cost_usd": 0.0,
            "cost_eur": 0.0,
            "latency_ms": 0,
            "cache": {
                "response_hit": True,
                "age_s": round(self.cache.clock() - hit.created, 1),
                "saved_usd": hit.meta["cost_usd"],
            },
        }
        resp["x_gateway"] = self.finish(sel, meta, resp.get("usage"), billable=False)
        return resp

    def _cache_store(self, sel: Selection, resp: dict | None, meta: dict | None) -> None:
        if not (sel.response_ttl and resp and meta):
            return
        ch = resp["choices"][0]
        if ch.get("finish_reason") != "stop" or ch["message"].get("tool_calls"):
            return  # seulement les réponses texte complètes
        self.cache.put(
            sel.response_key,
            sel.plan.identity,
            resp,
            {k: meta.get(k) for k in ("provider", "model", "hosting", "cost_usd")},
            sel.response_ttl,
        )

    # ----- exécution -----
    async def execute(self, sel: Selection, body: dict) -> dict:
        try:
            cached = self._cache_lookup(sel)
            if cached:
                return cached
            resp = await self._execute(sel, body)
            self._cache_store(sel, resp, resp.get("x_gateway"))
            return resp
        finally:
            sel.release()  # échec : la réservation ne doit pas rester (10.4)

    async def execute_stream(self, sel: Selection, body: dict) -> AsyncIterator[dict]:
        try:
            cached = self._cache_lookup(sel)
            if cached:
                chunks = replay_chunks(cached)
                chunks[-1]["x_gateway"] = cached["x_gateway"]
                for c in chunks:
                    yield c
                return
            rec, meta = StreamRecorder(), None
            async for chunk in self._execute_stream(sel, body):
                rec.feed(chunk)
                meta = chunk.get("x_gateway") or meta
                yield chunk
            self._cache_store(sel, rec.response(), meta)
        finally:
            sel.release()  # échec ou client déconnecté en cours de flux

    async def _execute(self, sel: Selection, body: dict) -> dict:
        t0 = time.monotonic()
        if sel.split:  # message trop long : map-reduce (13.4)
            resp, meta = await splitter.run(self, sel, body, t0)
            resp["x_gateway"] = self.finish(sel, meta, resp.get("usage"))
            return resp
        if sel.orchestrate:
            plan = await orchestrator.plan(self, sel, body)
            if plan.parts:
                run = crew.run if crew.applies(sel.info) else orchestrator.run  # gros modèle : CrewAI (facultatif)
                resp, meta = await run(self, sel, body, plan, t0)
                resp["x_gateway"] = self.finish(sel, meta, resp.get("usage"))
                return resp
            sel.planning = plan.planner  # indivisible : un seul appel, chemin normal
        attempts: list[dict] = []
        chain = list(self.variants(sel, body))
        for i, v in enumerate(chain):
            t_try = time.monotonic()
            hold = None
            rf, report = structured.requested(v.payload), None
            try:
                hold = self.admit(v, v.info, v.payload)
                if rf:  # sortie structurée : natif ou consigne, contrôlée (16.3)
                    resp, report = await structured.chat(v.provider, v.payload, rf)
                else:
                    resp = await v.provider.chat(v.payload)
            except (ProviderError, RateLimited) as e:
                self.unadmit(v, hold)
                self._failed(v, e, attempts, last=i == len(chain) - 1)
                continue
            self.health.mark_success(v.info.ref)
            self.latency.observe(v.info.ref, (time.monotonic() - t_try) * 1000)
            meta = self._fallback_meta(self.base_meta(v, resp.get("usage"), t0), v, attempts)
            if report:
                meta["structured"] = report
            resp["x_gateway"] = self.finish(v, meta, resp.get("usage"))
            return resp

    async def _execute_stream(self, sel: Selection, body: dict) -> AsyncIterator[dict]:
        """Chunks OpenAI ; le chunk d'usage final porte `x_gateway`. Le fallback n'est possible
        qu'avant le premier chunk (rien n'a encore été envoyé au client)."""
        t0 = time.monotonic()
        if sel.split:  # message trop long : map-reduce (13.4)
            async for chunk in splitter.stream(self, sel, body, t0):
                if chunk.get("usage"):
                    chunk["x_gateway"] = self.finish(sel, chunk["x_gateway"], chunk["usage"])
                yield chunk
            return
        if sel.orchestrate:
            plan = await orchestrator.plan(self, sel, body)
            if plan.parts and crew.applies(sel.info):  # CrewAI : réponse complète, envoyée d'un bloc
                resp, meta = await crew.run(self, sel, body, plan, t0)
                content = resp["choices"][0]["message"]["content"]
                yield {
                    "object": "chat.completion.chunk",
                    "model": sel.info.id,
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                    ],
                }
                final = {"object": "chat.completion.chunk", "model": sel.info.id, "choices": [], "usage": resp["usage"]}
                yield {**final, "x_gateway": self.finish(sel, meta, resp["usage"])}
                return
            if plan.parts:
                async for chunk in orchestrator.stream(self, sel, body, plan, t0):
                    if chunk.get("usage"):
                        chunk["x_gateway"] = self.finish(sel, chunk["x_gateway"], chunk["usage"])
                    yield chunk
                return
            sel.planning = plan.planner  # indivisible : un seul appel, chemin normal
        attempts: list[dict] = []
        chain = list(self.variants(sel, body))
        for i, v in enumerate(chain):
            t_try = time.monotonic()
            hold = None
            rf = structured.requested(v.payload)
            if rf and not structured.native(v.provider):
                # JSON sans support natif : réponse complète, contrôlée (nouvelle tentative possible), puis
                # rejouée en flux (16.3) — impossible de corriger des tokens déjà envoyés au client
                try:
                    hold = self.admit(v, v.info, v.payload)
                    resp, report = await structured.chat(v.provider, v.payload, rf)
                except (ProviderError, RateLimited) as e:
                    self.unadmit(v, hold)
                    self._failed(v, e, attempts, last=i == len(chain) - 1)
                    continue
                self.health.mark_success(v.info.ref)
                self.latency.observe(v.info.ref, (time.monotonic() - t_try) * 1000)
                meta = self._fallback_meta(self.base_meta(v, resp.get("usage"), t0), v, attempts)
                meta["structured"] = {**report, "buffered": True}
                chunks = replay_chunks(resp)
                chunks[-1]["x_gateway"] = self.finish(v, meta, resp.get("usage"))
                for c in chunks:
                    yield c
                return
            try:
                hold = self.admit(v, v.info, v.payload)
                upstream = v.provider.stream(v.payload)
                first = await upstream.__anext__()
            except StopAsyncIteration:
                first = None
            except (ProviderError, RateLimited) as e:
                self.unadmit(v, hold)
                self._failed(v, e, attempts, last=i == len(chain) - 1)
                continue
            self.health.mark_success(v.info.ref)
            self.latency.observe(v.info.ref, (time.monotonic() - t_try) * 1000)  # TTFT réel

            seen = {"usage": False, "chars": 0, "ttft": None, "text": [] if rf else None, "tools": False}

            def enrich(chunk: dict, v=v, seen=seen, rf=rf) -> dict:  # liés à cette tentative
                for ch in chunk.get("choices") or []:
                    delta = ch.get("delta") or {}
                    n = len(delta.get("content") or "")
                    if n and seen["ttft"] is None:  # premier token, côté serveur (6.1)
                        seen["ttft"] = int((time.monotonic() - t0) * 1000)
                    seen["chars"] += n
                    if seen["text"] is not None and n:  # sortie structurée native : contrôlée à la fin (16.3)
                        seen["text"].append(delta["content"])
                    seen["tools"] = seen["tools"] or bool(delta.get("tool_calls"))
                if chunk.get("usage"):
                    seen["usage"] = True
                    meta = self._fallback_meta(self.base_meta(v, chunk["usage"], t0), v, attempts)
                    meta["ttft_ms"] = seen["ttft"]
                    if rf and not seen["tools"]:
                        meta["structured"] = structured.stream_report("".join(seen["text"]), rf)
                    chunk["x_gateway"] = self.finish(v, meta, chunk["usage"])
                return chunk

            if first is not None:
                yield enrich(first)
                async for chunk in upstream:
                    yield enrich(chunk)
            if not seen["usage"]:
                # provider sans usage en streaming (certains serveurs locaux) : on estime, pour que
                # coût, quotas, limites et métriques (5.5) soient tout de même comptabilisés
                completion = (seen["chars"] + 3) // 4
                usage = {
                    "prompt_tokens": sel.cls.prompt_tokens,
                    "completion_tokens": completion,
                    "total_tokens": sel.cls.prompt_tokens + completion,
                }
                v.estimated_usage = True
                yield enrich({"object": "chat.completion.chunk", "model": v.upstream, "choices": [], "usage": usage})
            return


HOSTED_MASTER_MIN = 32


def secret_problems(plans: PlanResolver) -> list[str]:
    """Secrets faibles ou recopiés de deploy/.env.example (9.2). Bloquant sur une instance hébergée,
    simple avertissement sur un PC personnel."""
    out = []
    enc = os.getenv("GATEWAY_ENCRYPTION_KEY")
    if not enc:
        out.append(
            "GATEWAY_ENCRYPTION_KEY absente : la clé de chiffrement serait générée dans data/, à côté de "
            "la base qu'elle protège (une copie du volume livrerait les deux)."
        )
    elif is_placeholder(enc) or raw_key(enc) is None:
        out.append(
            "GATEWAY_ENCRYPTION_KEY doit être une clé aléatoire de 32 octets en base64 ou hex, pas une "
            "phrase : générez-la avec « python -m gateway.crypto genkey » et sauvegardez-la à part."
        )
    master = plans.master_key
    if not master:
        out.append("GATEWAY_MASTER_KEY absente : personne ne pourrait administrer l'instance.")
    elif len(master) < HOSTED_MASTER_MIN or is_placeholder(master):
        out.append(
            f"GATEWAY_MASTER_KEY : {HOSTED_MASTER_MIN} caractères aléatoires minimum, jamais la valeur "
            "d'exemple (ex. « python -m gateway.crypto genkey »)."
        )
    weak = [w for w in plans.weak_tokens() if w != "GATEWAY_MASTER_KEY"]
    if weak:
        out.append(
            f"Jetons de compte devinables (moins de {MIN_TOKEN_LENGTH} caractères ou valeur d'exemple) : "
            f"{', '.join(weak)}."
        )
    return out


def probe_error(e: Exception) -> str:
    """Raison lisible d'une détection ratée : message d'erreur du fournisseur (« Please pass a valid API key »…),
    code HTTP, ou serveur injoignable."""
    if not isinstance(e, ProviderError):
        return f"injoignable ({type(e).__name__})"
    text = e.message
    try:
        data = json.loads(text)
        data = data[0] if isinstance(data, list) and data else data
        err = data.get("error", data) if isinstance(data, dict) else None
        msg = err.get("message") if isinstance(err, dict) else err
        if isinstance(msg, str) and msg.strip():
            text = msg.strip()
    except (ValueError, AttributeError):
        pass
    return f"HTTP {e.status} : {text[:200]}"


def load_credentials(gw: Gateway, store: DocumentStore, cipher: Cipher) -> list[str]:
    """Déchiffre en mémoire les secrets des providers stockés en base. Un secret illisible
    (mauvaise clé de chiffrement) est ignoré et signalé, sans bloquer le démarrage."""
    errors = []
    for cred in store.list_credentials():
        p = gw.providers.get(cred["provider"])
        if not p:
            continue
        try:
            p.cfg.stored_key = cipher.decrypt(store.get_credential(cred["provider"]), cred["provider"])
            p.cfg.enabled = True
        except CryptoError:
            errors.append(cred["provider"])
            log.error("Secret de '%s' indéchiffrable : clé de chiffrement différente ?", cred["provider"])
    return errors


def _err(status: int, message: str, provider: str = "gateway", headers: dict | None = None) -> JSONResponse:
    kind = "rate_limit_error" if status == 429 else "invalid_request_error" if status < 500 else "api_error"
    return JSONResponse(
        {"error": {"message": message, "type": kind, "code": status, "provider": provider}},
        status_code=status,
        headers=headers,
    )


def lan_urls(http_port: int) -> list[str]:
    """Adresses réseau local : https://…:GATEWAY_TLS_PORT si le HTTPS réseau local est actif (12.7), sinon http."""
    tls = tls_port()
    return [f"https://{ip}:{tls}" if tls else f"http://{ip}:{http_port}" for ip in lan_addresses(bind_address())]


def tls_info() -> dict:
    tls = tls_port()
    return {
        "enabled": bool(tls),
        "port": tls,
        "fingerprint": ca_fingerprint() if tls else None,
        "ca_url": "/ca.crt" if tls else None,
    }
