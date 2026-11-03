"""Modèles locaux au quotidien (18.4) : la carte graphique du propriétaire reste à lui.

- File d'attente par serveur local : au plus `max_concurrent` requêtes envoyées à la fois (2 par défaut, réglable
  par serveur) ; les suivantes attendent leur tour, au plus `queue_wait` secondes. L'ordre est équitable : le
  propriétaire (clé maître) d'abord, puis l'utilisateur qui a le moins de requêtes en cours, puis chacun son tour
  — un utilisateur distant qui en envoie dix ne fait pas attendre les autres. Au-delà de LOCAL_QUEUE_PER_USER requêtes en attente pour un
  même utilisateur, refus immédiat (429). Le contrôle est fait à l'ENVOI, par le connecteur : routage, secours,
  résumé de l'historique, titres, multi-agents, embeddings passent tous par là.
- Mode « PC occupé » (bouton, durée ou horaire) : les serveurs de ce PC (localhost, host.docker.internal ; liste
  réglable) sont réservés au propriétaire. Les autres (clés virtuelles, comptes) basculent selon le réglage
  de leur clé (« when_busy ») : offres cloud gratuites (13.8, défaut), tout le cloud permis, ou refus 503. Les
  modèles chargés peuvent être déchargés au passage, pour libérer la mémoire vidéo.
- keep_alive d'Ollama : durée pendant laquelle un modèle reste en mémoire après une requête de la gateway
  (0 = déchargé aussitôt, -1 = toujours chargé ; absent = réglage d'Ollama, 5 min). L'API compatible OpenAI d'Ollama
  n'accepte pas keep_alive : la gateway le règle juste après la requête (POST /api/generate sans prompt). Une
  requête faite directement à Ollama (hors gateway) garde son propre réglage.
- Mode « économie » (bouton, pour une durée ou jusqu'à nouvel ordre) : les IA locales consomment le moins possible
  de ressources de ce PC, pour TOUT le monde (propriétaire compris) — une requête à la fois par serveur local,
  modèles libérés de la mémoire après 60 s, réponses locales plafonnées (1 024 tokens), routage automatique vers les
  modèles locaux légers (8 milliards de paramètres au plus, s'il y en a), vectorisation des documents en pause,
  Ollama de ce PC et IA intégrées (Kokoro, Whisper) en priorité basse / EcoQoS. Tout est réglable ; tout est
  rétabli à la fin (y compris après un redémarrage de la gateway pendant le mode).
Files et compteurs vivent dans le processus : avec plusieurs workers, la limite s'applique à chacun.
"""

from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx

from . import power
from .local_context import looks_ollama, server_root
from .providers.base import LocalUnavailable
from .providers.ollama_native import to_native

log = logging.getLogger("gateway.local_gpu")

SETTING = "local_gpu"
KEEP_ALIVE_MAX = 7 * 86400
THIS_PC_HOSTS = {"localhost", "host.docker.internal", "gateway.docker.internal"}
DAYS = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")
ECO_DEFAULTS = {
    "max_concurrent": 1,  # requêtes envoyées à la fois à chaque serveur local
    "keep_alive": 60,  # s : modèle libéré de la mémoire vidéo après ce délai sans requête
    "max_tokens": 1024,  # réponse locale plafonnée (0 = pas de plafond)
    "max_params_b": 8,  # routage automatique : modèles locaux de cette taille au plus, s'il y en a (0 = tous)
    "low_priority": True,  # Ollama de ce PC et IA intégrées en priorité basse / EcoQoS
    "pause_indexing": True,  # vectorisation des documents en pause
    "unload": True,  # modèles chargés déchargés au début du mode
    # Ollama de ce PC, par son API native (18.7) : vrai bridage du chargement des modèles
    "gpu_share": 50,  # % des couches du modèle sur la carte graphique (0 = processeur seul, 100 = pas de limite)
    "cpu_share": 50,  # % des cœurs du processeur utilisés pour la part hors carte graphique (100 = pas de limite)
    "context": 4096,  # tokens de contexte réservés en mémoire (0 = réglage d'Ollama)
}
ECO_LIMITS = {
    "max_concurrent": (1, 8),
    "keep_alive": (0, 3600),
    "max_tokens": (0, 32768),
    "max_params_b": (0, 1000),
    "gpu_share": (0, 100),
    "cpu_share": (10, 100),
    "context": (0, 262144),
}
OLLAMA_PROCESSES = {"ollama.exe", "ollama"}
_PARAMS = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)

# appelant de la requête en cours : (identité, propriétaire ?) — posé par Gateway.select, hérité par les tâches
CALLER: contextvars.ContextVar[tuple[str, bool]] = contextvars.ContextVar("gw_caller", default=("système", False))


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def default_max_concurrent() -> int:
    return _int_env("LOCAL_MAX_CONCURRENT", 2)


def default_queue_wait() -> int:
    return _int_env("LOCAL_QUEUE_WAIT_SECONDS", 60)


def per_user_waiting() -> int:
    return _int_env("LOCAL_QUEUE_PER_USER", 4)


class LocalBusy(LocalUnavailable):
    pass


class LocalQueueFull(LocalUnavailable):
    pass


class LocalGpuError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


def this_pc(base_url: str) -> bool:
    """Serveur qui tourne sur ce PC : sa carte graphique est celle du propriétaire (intégrés compris)."""
    if base_url.startswith("builtin://"):
        return True
    host = (urlparse(base_url).hostname or "").lower()
    if host in THIS_PC_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ---------- réglages ----------


def _hhmm(v, name: str) -> str:
    try:
        t = datetime.strptime(str(v), "%H:%M")
    except ValueError:
        raise LocalGpuError(f"'{name}' : heure au format HH:MM.")
    return t.strftime("%H:%M")


def validate(data, providers: set[str]) -> dict:
    """Réglages envoyés par l'administration (PUT /v1/admin/gpu/settings)."""
    if not isinstance(data, dict):
        raise LocalGpuError("Réglages : objet attendu.")
    unknown = set(data) - {"keep_alive", "max_concurrent", "queue_wait", "servers", "busy", "eco"}
    if unknown:
        raise LocalGpuError(f"Réglages inconnus : {', '.join(sorted(unknown))}.")
    out: dict = {}
    ka = data.get("keep_alive")
    if ka is not None:
        if not isinstance(ka, int) or isinstance(ka, bool) or not -1 <= ka <= KEEP_ALIVE_MAX:
            raise LocalGpuError("'keep_alive' : secondes (0 = décharger aussitôt, -1 = toujours chargé).")
        out["keep_alive"] = ka
    for name, hi in (("max_concurrent", 64), ("queue_wait", 600)):
        v = data.get(name)
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= hi:
                raise LocalGpuError(f"'{name}' : entier entre 1 et {hi}.")
            out[name] = v
    servers = data.get("servers") or {}
    if not isinstance(servers, dict):
        raise LocalGpuError("'servers' : {serveur: {\"max_concurrent\": n}}.")
    out["servers"] = {}
    for name, s in servers.items():
        if name not in providers:
            raise LocalGpuError(f"Serveur local inconnu : '{name}'.")
        n = (s or {}).get("max_concurrent") if isinstance(s, dict) else None
        if n is not None:
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 64:
                raise LocalGpuError(f"'{name}' : requêtes simultanées entre 1 et 64.")
            out["servers"][name] = {"max_concurrent": n}
    busy = data.get("busy") or {}
    if not isinstance(busy, dict) or set(busy) - {"providers", "schedule", "unload"}:
        raise LocalGpuError('\'busy\' : {"providers", "schedule", "unload"}.')
    b: dict = {"unload": bool(busy.get("unload", True)), "schedule": []}
    if busy.get("providers") is not None:
        names = busy["providers"]
        if not isinstance(names, list) or set(names) - providers:
            raise LocalGpuError("'busy.providers' : liste de serveurs locaux.")
        b["providers"] = sorted(set(names))
    sched = busy.get("schedule") or []
    if not isinstance(sched, list) or len(sched) > 14:
        raise LocalGpuError("'busy.schedule' : 14 plages au plus.")
    for slot in sched:
        if not isinstance(slot, dict):
            raise LocalGpuError('Plage horaire : {"days": [1..7], "start": "HH:MM", "end": "HH:MM"}.')
        days = slot.get("days")
        if not isinstance(days, list) or not days or not all(isinstance(d, int) and 1 <= d <= 7 for d in days):
            raise LocalGpuError("Plage horaire : jours de 1 (lundi) à 7 (dimanche).")
        start, end = _hhmm(slot.get("start"), "start"), _hhmm(slot.get("end"), "end")
        if start == end:
            raise LocalGpuError("Plage horaire : début et fin identiques.")
        b["schedule"].append({"days": sorted(set(days)), "start": start, "end": end})
    out["busy"] = b
    eco = {} if data.get("eco") is None else data["eco"]
    if not isinstance(eco, dict) or set(eco) - set(ECO_DEFAULTS):
        raise LocalGpuError(f"'eco' : {{{', '.join(ECO_DEFAULTS)}}}.")
    e: dict = {}
    for k, v in eco.items():
        if k in ECO_LIMITS:
            lo, hi = ECO_LIMITS[k]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise LocalGpuError(f"'eco.{k}' : entier entre {lo} et {hi}.")
        elif not isinstance(v, bool):
            raise LocalGpuError(f"'eco.{k}' : true ou false.")
        e[k] = v
    out["eco"] = e
    return out


def params_b(model_id: str) -> float | None:
    """Taille d'un modèle d'après son nom (llama3.1:8b → 8, gemma4:e4b → 4) ; None si le nom ne la donne pas."""
    m = _PARAMS.search(model_id.replace(":", " ").replace("-", " "))
    return float(m.group(1)) if m else None


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def scheduled_until(schedule: list[dict], now: float) -> float | None:
    """Fin de la plage horaire en cours (heure locale du PC), None hors plage. Une plage dont la fin précède le
    début passe minuit (22:00 → 07:00) : elle appartient au jour où elle commence."""
    t = datetime.fromtimestamp(now)
    for slot in schedule:
        start, end = _minutes(slot["start"]), _minutes(slot["end"])
        overnight = end <= start
        for back in (0, 1) if overnight else (0,):  # plage commencée aujourd'hui, ou hier soir
            day = t - timedelta(days=back)
            if day.isoweekday() not in slot["days"]:
                continue
            begin = day.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
            finish = begin.replace(hour=end // 60, minute=end % 60) + timedelta(days=1 if overnight else 0)
            if begin <= t < finish:
                return finish.timestamp()
    return None


# ---------- file d'attente d'un serveur ----------


@dataclass(eq=False)
class _Waiter:
    seq: int
    identity: str
    owner: bool
    future: asyncio.Future


class Gate:
    """File d'attente équitable d'un serveur local (une par connecteur)."""

    def __init__(self, manager: LocalGpu, provider):
        self.manager = manager
        self.provider = provider
        self.active: dict[str, int] = {}
        self.waiting: list[_Waiter] = []
        self.last_turn: dict[str, int] = {}  # identité -> numéro de son dernier passage
        self._turn = 0
        self._seq = 0
        self.served = 0
        self.waited = 0

    @property
    def name(self) -> str:
        return self.provider.name

    @property
    def total(self) -> int:
        return sum(self.active.values())

    def limit(self) -> int:
        n = self.manager.max_concurrent(self.name)
        return min(n, self.manager.eco_setting("max_concurrent")) if self.manager.eco_on() else n

    def adjust(self, payload: dict) -> dict:
        """Mode économie : réponse locale plafonnée (max_tokens), demandée plus longue ou sans limite."""
        cap = self.manager.eco_setting("max_tokens") if self.manager.eco_on() else 0
        if not cap:
            return payload
        asked = [payload.get(k) for k in ("max_tokens", "max_completion_tokens") if payload.get(k)]
        if asked and min(asked) <= cap:
            return payload
        out = {k: v for k, v in payload.items() if k != "max_completion_tokens"}
        out["max_tokens"] = cap
        return out

    def _grant(self, identity: str) -> None:
        self.active[identity] = self.active.get(identity, 0) + 1
        self._turn += 1
        self.last_turn[identity] = self._turn
        if len(self.last_turn) > 1000:  # mémoire bornée : on oublie les plus anciens
            for k in sorted(self.last_turn, key=self.last_turn.get)[:500]:
                del self.last_turn[k]

    def _rank(self, w: _Waiter) -> tuple:
        """Propriétaire d'abord ; puis le moins de requêtes en cours ; puis celui servi il y a le plus longtemps
        (tour de rôle : une rafale d'un utilisateur ne passe pas devant les autres) ; puis l'ordre d'arrivée."""
        return (not w.owner, self.active.get(w.identity, 0), self.last_turn.get(w.identity, 0), w.seq)

    def _wake(self) -> None:
        while self.waiting and self.total < self.limit():
            best = min(self.waiting, key=self._rank)  # recalculé : les requêtes en cours ont changé
            self.waiting.remove(best)
            if best.future.done():  # abandonnée entre-temps
                continue
            self._grant(best.identity)
            best.future.set_result(True)

    async def acquire(self, identity: str, owner: bool) -> None:
        if self.total < self.limit() and not self.waiting:
            self._grant(identity)
            return
        mine = sum(1 for w in self.waiting if w.identity == identity)
        if not owner and mine >= per_user_waiting():
            raise LocalQueueFull(
                429,
                f"Serveur local '{self.name}' : déjà {mine} de vos requêtes en attente. "
                "Attendez leurs réponses avant d'en envoyer d'autres.",
                self.name,
                5,
            )
        self._seq += 1
        w = _Waiter(self._seq, identity, owner, asyncio.get_running_loop().create_future())
        self.waiting.append(w)
        self.waited += 1
        self._wake()  # une place a pu se libérer (limite relevée)
        wait = self.manager.queue_wait()
        try:
            await asyncio.wait_for(asyncio.shield(w.future), wait)
        except (TimeoutError, asyncio.CancelledError) as e:
            if w.future.done() and not w.future.cancelled():
                self.release(identity)  # place accordée au moment de l'abandon : rendue
            else:
                w.future.cancel()
                if w in self.waiting:
                    self.waiting.remove(w)
            if isinstance(e, asyncio.CancelledError):
                raise
            raise LocalQueueFull(
                503,
                f"Serveur local '{self.name}' occupé : {self.total} requête(s) en cours, "
                f"{len(self.waiting)} en attente après {wait} s. Réessayez dans un instant.",
                self.name,
                10,
            )

    def release(self, identity: str) -> None:
        n = self.active.get(identity, 0) - 1
        if n > 0:
            self.active[identity] = n
        else:
            self.active.pop(identity, None)
        self._wake()

    @asynccontextmanager
    async def slot(self, model: str | None = None, kind: str = "chat"):
        identity, owner = CALLER.get()
        self.manager.check_busy(self.name, owner)
        await self.acquire(identity, owner)
        try:
            yield
        finally:
            self.release(identity)
            self.served += 1
            if kind == "chat" and model:
                self.manager.after_use(self, model)

    def status(self) -> dict:
        return {
            "active": self.total,
            "waiting": len(self.waiting),
            "max_concurrent": self.limit(),
            "owner_waiting": sum(1 for w in self.waiting if w.owner),
            "users": len(self.active),
            "served": self.served,
            "queued": self.waited,
        }


# ---------- gestionnaire ----------


class LocalGpu:
    def __init__(self, providers: dict, clock=time.time):
        self.providers = providers  # Gateway.providers (référence partagée)
        self.clock = clock
        self.store = None
        self.config: dict = {}
        self.manual: dict | None = None  # {"until": ts | None, "at": ts}
        self.gates: dict[str, Gate] = {}
        self._tasks: set[asyncio.Task] = set()
        self._was_busy = False
        self.on_change = None  # rappel (audit) : début / fin du mode occupé
        self.eco_manual: dict | None = None  # mode économie : {"until": ts | None, "at": ts}
        self.lowered: dict[str, int] = {}  # pid -> priorité d'origine (processus Ollama ralentis)
        self._was_eco = False
        self.on_eco_change = None  # rappel (audit) : fin du mode économie (durée écoulée)
        self._eco_event = asyncio.Event()  # réveille les tâches en pause (vectorisation) au changement de mode
        self._layers: dict[tuple[str, str], int | None] = {}  # (serveur, modèle) -> nombre de couches (/api/show)

    def attach(self, store) -> None:
        self.store = store
        saved = store.get_setting(SETTING) or {}
        self.config = saved.get("config") or {}
        self.manual = saved.get("manual")
        self._was_busy = bool(self.busy_state()["on"])
        self.eco_manual = saved.get("eco")
        self.lowered = {str(k): int(v) for k, v in (saved.get("lowered") or {}).items()}
        # redémarrage : mode toujours en cours → réappliqué au premier tick ; terminé entre-temps → priorités rendues
        self._was_eco = False

    def _save(self) -> None:
        if self.store:
            self.store.set_setting(
                SETTING, {"config": self.config, "manual": self.manual, "eco": self.eco_manual, "lowered": self.lowered}
            )

    def local_names(self) -> list[str]:
        return sorted(
            n for n, p in self.providers.items() if p.hosting == "local" and getattr(p, "gate", None) is not None
        )

    def set_config(self, data) -> dict:
        self.config = validate(data, set(self.local_names()))
        self._save()
        for g in self.gates.values():
            g._wake()  # limite relevée : les suivants partent tout de suite
        return self.config

    # -- file d'attente --
    def gate_for(self, provider) -> Gate:
        g = self.gates.get(provider.name)
        if g is None:
            g = self.gates[provider.name] = Gate(self, provider)
        g.provider = provider
        return g

    def max_concurrent(self, name: str) -> int:
        s = (self.config.get("servers") or {}).get(name) or {}
        return s.get("max_concurrent") or self.config.get("max_concurrent") or default_max_concurrent()

    def queue_wait(self) -> int:
        return self.config.get("queue_wait") or default_queue_wait()

    # -- PC occupé --
    def busy_providers_config(self) -> list[str]:
        names = (self.config.get("busy") or {}).get("providers")
        if names is not None:
            return [n for n in names if n in self.providers]
        return [n for n in self.local_names() if this_pc(self.providers[n].cfg.base_url)]

    def busy_state(self) -> dict:
        now = self.clock()
        if self.manual and self.manual.get("until") and self.manual["until"] <= now:
            self.manual = None  # durée écoulée
            self._save()
        if self.manual:
            return {"on": True, "source": "manual", "until": self.manual.get("until"), "since": self.manual["at"]}
        until = scheduled_until((self.config.get("busy") or {}).get("schedule") or [], now)
        if until:
            return {"on": True, "source": "schedule", "until": until, "since": None}
        return {"on": False, "source": None, "until": None, "since": None}

    def busy(self) -> set[str]:
        return set(self.busy_providers_config()) if self.busy_state()["on"] else set()

    def busy_error(self) -> LocalBusy:
        st = self.busy_state()
        until = st.get("until")
        when = (
            f" jusqu'à {datetime.fromtimestamp(until):%H:%M}"
            if until and until - self.clock() < 86400
            else f" jusqu'au {datetime.fromtimestamp(until):%d/%m %H:%M}"
            if until
            else ""
        )
        wait = max(30, int(until - self.clock())) if until else 300
        return LocalBusy(
            503,
            f"PC occupé : les modèles locaux de ce PC sont réservés à son propriétaire{when}. "
            "Réessayez plus tard ou utilisez une IA cloud.",
            "local_busy",
            wait,
        )

    def check_busy(self, name: str, owner: bool) -> None:
        if not owner and name in self.busy():
            raise self.busy_error()

    def blocks(self, owner: bool) -> bool:
        return not owner and bool(self.busy())

    def filter(self, models: list, owner: bool, perms: dict | None) -> list:
        """Routage : modèles permis à cet appelant pendant le mode occupé, selon le réglage de sa clé."""
        busy = self.busy() if not owner else set()
        if not busy:
            return models
        models = [m for m in models if m.provider not in busy]
        policy = (perms or {}).get("when_busy") or "free"
        if policy == "free":
            return [m for m in models if m.hosting == "local" or m.free]
        if policy == "refuse":
            return [m for m in models if m.hosting == "local"]
        return models

    async def set_busy(self, on: bool, minutes: int | None = None) -> dict:
        if on:
            now = self.clock()
            self.manual = {"at": now, "until": now + minutes * 60 if minutes else None}
        else:
            self.manual = None
        self._save()
        await self.tick(by_request=True)
        return self.busy_state()

    # -- mode économie --
    def eco_setting(self, name: str):
        v = (self.config.get("eco") or {}).get(name)
        return ECO_DEFAULTS[name] if v is None else v

    def eco_state(self) -> dict:
        now = self.clock()
        if self.eco_manual and self.eco_manual.get("until") and self.eco_manual["until"] <= now:
            self.eco_manual = None  # durée écoulée
            self._save()
        if self.eco_manual:
            return {"on": True, "until": self.eco_manual.get("until"), "since": self.eco_manual["at"]}
        return {"on": False, "until": None, "since": None}

    def eco_on(self) -> bool:
        return bool(self.eco_state()["on"])

    def eco_filter(self, models: list) -> list:
        """Routage automatique en mode économie : les modèles locaux légers seulement, s'il y en a ; le cloud et un
        modèle demandé explicitement ne sont pas concernés."""
        limit = self.eco_setting("max_params_b") if self.eco_on() else 0
        if not limit:
            return models
        local = [m for m in models if m.hosting == "local"]
        light = [m for m in local if (params_b(m.id) or 7.0) <= limit]
        if not light or len(light) == len(local):
            return models
        heavy = {id(m) for m in local if m not in light}
        return [m for m in models if id(m) not in heavy]

    def eco_native(self, provider) -> bool:
        """Chat de ce serveur traduit vers l'API native d'Ollama (bridage du chargement) : mode économie actif,
        Ollama de CE PC (la carte graphique d'une autre machine n'est pas la nôtre), au moins un réglage actif."""
        return (
            self.eco_on()
            and looks_ollama(provider.name, provider.cfg.base_url)
            and this_pc(provider.cfg.base_url)
            and (
                self.eco_setting("gpu_share") < 100
                or self.eco_setting("cpu_share") < 100
                or self.eco_setting("context")
            )
        )

    async def layers(self, provider, model: str) -> int | None:
        """Nombre de couches du modèle (/api/show : « <architecture>.block_count »), gardé en mémoire."""
        key = (provider.name, model)
        if key not in self._layers:
            try:
                r = await provider.client.post(
                    f"{server_root(provider.cfg.base_url)}/api/show",
                    json={"model": model},
                    headers=provider._headers(),
                    timeout=5,
                )
                info = (r.json().get("model_info") or {}) if r.status_code == 200 else {}
                found = next((v for k, v in info.items() if k.endswith(".block_count") and isinstance(v, int)), None)
            except (httpx.HTTPError, ValueError, AttributeError):
                return None  # serveur injoignable : redemandé la prochaine fois
            self._layers[key] = found
        return self._layers[key]

    async def eco_options(self, provider, model: str, needed: int = 0) -> dict:
        """Options natives de chargement : couches sur la carte graphique (num_gpu), cœurs (num_thread),
        contexte (num_ctx, laissé à Ollama si la requête n'y tiendrait pas : jamais de troncature silencieuse).
        Toujours les mêmes pour un modèle : Ollama ne le recharge pas à chaque requête."""
        opts: dict = {}
        share = self.eco_setting("gpu_share")
        if share < 100:
            blocks = await self.layers(provider, model)
            if share == 0:
                opts["num_gpu"] = 0
            elif blocks:
                opts["num_gpu"] = max(1, round((blocks + 1) * share / 100))  # + la couche de sortie
        cpu = self.eco_setting("cpu_share")
        if cpu < 100:
            cores = max(1, (os.cpu_count() or 2) // 2)  # cœurs physiques (Ollama en utilise autant par défaut)
            opts["num_thread"] = max(1, round(cores * cpu / 100))
        ctx = self.eco_setting("context")
        if ctx and needed <= ctx:
            opts["num_ctx"] = ctx
        return opts

    def eco_keep_alive(self) -> int:
        ka = self.config.get("keep_alive")
        eco = self.eco_setting("keep_alive")
        return eco if ka is None or ka < 0 or eco < ka else ka

    async def eco_request(self, provider, payload: dict) -> dict | None:
        """Corps /api/chat bridé pour `payload` (format OpenAI), ou None : requête à envoyer telle quelle."""
        if not self.eco_native(provider):
            return None
        chars = sum(len(json.dumps(m.get("content"), ensure_ascii=False)) for m in payload.get("messages") or [])
        needed = chars // 3 + int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 0)
        options = await self.eco_options(provider, payload.get("model") or "", needed)
        return to_native(payload, options, self.eco_keep_alive())

    def indexing_paused(self) -> bool:
        return self.eco_on() and bool(self.eco_setting("pause_indexing"))

    async def eco_wait(self, seconds: float) -> None:
        """Attend un changement du mode économie (bouton) ou `seconds` secondes (fin de durée : vérifiée ensuite)."""
        try:
            await asyncio.wait_for(self._eco_event.wait(), seconds)
        except TimeoutError:
            pass

    async def set_eco(self, on: bool, minutes: int | None = None) -> dict:
        if on:
            now = self.clock()
            self.eco_manual = {"at": now, "until": now + minutes * 60 if minutes else None}
        else:
            self.eco_manual = None
        self._save()
        await self.tick(by_request=True)
        self._eco_event.set()
        self._eco_event = asyncio.Event()
        return self.eco_state()

    def _ollama_here(self) -> bool:
        return any(
            p.hosting == "local" and looks_ollama(n, p.cfg.base_url) and this_pc(p.cfg.base_url)
            for n, p in self.providers.items()
        )

    def _lower_priorities(self) -> None:
        """Ollama de ce PC (et ses processus de modèles, lancés entre-temps) en priorité basse + EcoQoS ;
        la gateway elle-même en EcoQoS (Kokoro, Whisper intégrés). Relancé à chaque vérification."""
        power.set_power_throttling(True)
        if not self._ollama_here():
            return
        changed = False
        for pid in power.find_processes(OLLAMA_PROCESSES):
            if str(pid) in self.lowered:
                continue
            before = power.get_priority(pid)
            if before is not None and power.set_priority(pid, power.low_value()):
                power.set_power_throttling(True, pid)
                self.lowered[str(pid)] = before
                changed = True
        if changed:
            self._save()

    def _restore_priorities(self) -> None:
        from .providers import local_tts

        # gateway : bridage refusé si Kokoro est chargé (sinon ~4 s au lieu de ~1 s), sinon laissé à Windows
        power.set_power_throttling(False if local_tts.NAME in self.providers else None)
        alive = set(power.find_processes(OLLAMA_PROCESSES)) if self.lowered else set()
        for pid, before in self.lowered.items():
            if int(pid) in alive:  # un PID réutilisé par un autre programme n'est jamais touché
                power.set_priority(int(pid), before)
                power.set_power_throttling(None, int(pid))
        if self.lowered:
            self.lowered = {}
            self._save()

    async def _eco_tick(self, by_request: bool) -> None:
        on = self.eco_on()
        if on and self.eco_setting("low_priority"):
            await asyncio.to_thread(self._lower_priorities)
        elif self.lowered or (self._was_eco and not on):
            await asyncio.to_thread(self._restore_priorities)
        if on == self._was_eco:
            return
        self._was_eco = on
        if not on and self.on_eco_change and not by_request:
            try:
                self.on_eco_change(on, self.eco_state())
            except Exception:
                log.exception("Mode économie : rappel impossible")
        if on:
            for g in self.gates.values():
                g._wake()
            if self.eco_setting("unload"):
                for name in self.local_names():
                    if this_pc(self.providers[name].cfg.base_url) and looks_ollama(
                        name, self.providers[name].cfg.base_url
                    ):
                        try:
                            await self.unload(name)
                        except LocalGpuError:
                            pass
        else:
            for g in self.gates.values():
                g._wake()  # limite rendue : les requêtes en attente partent

    async def tick(self, by_request: bool = False) -> None:
        """Changement d'état (bouton, horaire, durée écoulée) : modèles déchargés au début si demandé. Rappel
        d'audit pour les changements sans requête (la route du bouton est déjà journalisée)."""
        await self._eco_tick(by_request)
        on = bool(self.busy_state()["on"])
        if on == self._was_busy:
            return
        self._was_busy = on
        if self.on_change and not by_request:
            try:
                self.on_change(on, self.busy_state())
            except Exception:
                log.exception("Mode PC occupé : rappel impossible")
        if on and (self.config.get("busy") or {}).get("unload", True):
            for name in self.busy_providers_config():
                try:
                    await self.unload(name)
                except LocalGpuError:
                    pass

    async def run(self, every: float = 30) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Mode PC occupé : vérification impossible")
            await asyncio.sleep(every)

    # -- Ollama : modèles chargés, keep_alive, déchargement --
    def _ollama(self, name: str):
        p = self.providers.get(name)
        if p is None or p.hosting != "local":
            raise LocalGpuError(f"Serveur local inconnu : '{name}'.", 404)
        if not looks_ollama(p.name, p.cfg.base_url):
            raise LocalGpuError(
                f"'{name}' n'est pas un serveur Ollama : le déchargement se fait depuis son "
                "application (LM Studio : bouton Eject).",
                400,
            )
        return p

    def after_use(self, gate: Gate, model: str) -> None:
        if self.eco_native(gate.provider):
            return  # requête native : keep_alive déjà envoyé, avec les mêmes options (sinon Ollama rechargerait)
        ka = self.config.get("keep_alive")
        if self.eco_on():  # mode économie : libéré plus tôt (jamais plus tard que le réglage habituel)
            ka = self.eco_keep_alive()
        if ka is None or gate.total or not looks_ollama(gate.name, gate.provider.cfg.base_url):
            return  # d'autres requêtes en cours : la dernière s'en charge
        try:
            t = asyncio.get_running_loop().create_task(self._keep_alive(gate.provider, model, ka))
        except RuntimeError:
            return
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _keep_alive(self, p, model: str, seconds: int) -> None:
        root = server_root(p.cfg.base_url)
        try:
            r = await p.client.post(
                f"{root}/api/generate", json={"model": model, "keep_alive": seconds}, headers=p._headers(), timeout=30
            )
            if r.status_code >= 400:  # modèle d'embeddings : pas de génération
                await p.client.post(
                    f"{root}/api/embed",
                    json={"model": model, "input": [], "keep_alive": seconds},
                    headers=p._headers(),
                    timeout=30,
                )
        except httpx.HTTPError as e:
            log.info("keep_alive de %s/%s non appliqué : %s", p.name, model, type(e).__name__)

    async def loaded(self, name: str) -> list[dict]:
        p = self._ollama(name)
        try:
            r = await p.client.get(f"{server_root(p.cfg.base_url)}/api/ps", headers=p._headers(), timeout=3)
            items = (r.json().get("models") or []) if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError, AttributeError):
            raise LocalGpuError(f"Serveur '{name}' injoignable.", 503)
        return [
            {
                "model": m.get("name") or m.get("model"),
                "size": m.get("size"),
                "vram": m.get("size_vram"),
                "expires_at": m.get("expires_at"),
                "context": m.get("context_length"),
            }
            for m in items
            if isinstance(m, dict)
        ]

    async def unload(self, name: str, model: str | None = None) -> list[str]:
        """Décharge un modèle (ou tous ceux chargés) d'un serveur Ollama : la mémoire vidéo est libérée dès que
        ses requêtes en cours sont terminées."""
        p = self._ollama(name)
        models = [model] if model else [m["model"] for m in await self.loaded(name) if m["model"]]
        for m in models:
            await self._keep_alive(p, m, 0)
        return models

    def status(self) -> dict:
        busy_set = set(self.busy_providers_config())
        servers = []
        for n in self.local_names():
            p = self.providers[n]
            g = self.gates.get(n)
            servers.append(
                {
                    "name": n,
                    "this_pc": this_pc(p.cfg.base_url),
                    "ollama": looks_ollama(n, p.cfg.base_url),
                    "busy_reserved": n in busy_set,
                    "max_concurrent_override": ((self.config.get("servers") or {}).get(n) or {}).get("max_concurrent"),
                    **(
                        g.status()
                        if g
                        else {
                            "active": 0,
                            "waiting": 0,
                            "max_concurrent": self.max_concurrent(n),
                            "owner_waiting": 0,
                            "users": 0,
                            "served": 0,
                            "queued": 0,
                        }
                    ),
                }
            )
        return {
            "config": {
                "keep_alive": self.config.get("keep_alive"),
                "max_concurrent": self.config.get("max_concurrent"),
                "queue_wait": self.config.get("queue_wait"),
                "busy": {
                    "providers": (self.config.get("busy") or {}).get("providers"),
                    "schedule": (self.config.get("busy") or {}).get("schedule") or [],
                    "unload": (self.config.get("busy") or {}).get("unload", True),
                },
            },
            "defaults": {
                "max_concurrent": default_max_concurrent(),
                "queue_wait": default_queue_wait(),
                "per_user_waiting": per_user_waiting(),
            },
            "busy": {**self.busy_state(), "providers": sorted(busy_set)},
            "eco": {
                **self.eco_state(),
                "settings": {k: self.eco_setting(k) for k in ECO_DEFAULTS},
                "defaults": ECO_DEFAULTS,
                "lowered_processes": len(self.lowered),
            },
            "servers": servers,
        }
