"""Supervision (18.5) : /metrics au format Prometheus et alertes d'exploitation.

/metrics (texte Prometheus 0.0.4) : réservé à ce PC (127.0.0.1 sans tunnel), à la clé maître, ou à un jeton de
lecture dédié GATEWAY_METRICS_TOKEN (24 caractères au moins) — à mettre dans Prometheus plutôt que la clé maître.
Contenu : requêtes (fournisseur, hébergement, statut), latence (histogramme), tokens, coût, échecs des fournisseurs,
serveurs locaux joignables, tunnel, files d'attente locales et mode PC occupé (18.4), disque, file des métriques.
Compteurs propres au processus (remis à zéro au redémarrage, comme le veut Prometheus ; un worker par série).

Alertes (même canaux que les alertes budgétaires 6.5 : notification dans l'interface de la clé maître + webhook
optionnel Slack / Discord / JSON signé) :
- serveur local tombé (2 échecs de suite, après avoir répondu au moins une fois) puis rétabli ;
- disque de la base sous le seuil (SUPERVISION_DISK_MIN_GB, 2 Go par défaut ; réglable dans l'administration),
  puis revenu au-dessus (avec une marge, pour ne pas alerter en boucle).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from collections import defaultdict
from pathlib import Path

import httpx

from .alerts import AlertError, check_webhook_url, new_secret
from .providers.base import ProviderError

log = logging.getLogger("gateway.supervision")

SETTING = "supervision"
LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)
FAILURES_BEFORE_DOWN = 2
DISK_REARM = 1.2  # l'alerte disque se réarme au-dessus de 120 % du seuil
GB = 1024**3


def interval() -> float:
    try:
        return max(10.0, float(os.getenv("SUPERVISION_INTERVAL", "60")))
    except ValueError:
        return 60.0


def default_disk_min_gb() -> float:
    try:
        return max(0.0, float(os.getenv("SUPERVISION_DISK_MIN_GB", "2")))
    except ValueError:
        return 2.0


def metrics_token() -> str | None:
    token = os.getenv("GATEWAY_METRICS_TOKEN", "").strip()
    return token if len(token) >= 24 else None


# ---------- compteurs Prometheus ----------


def _escape(value) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _num(value: float) -> str:
    """Valeur exacte : entiers sans exposant (octets du disque…), sinon la représentation la plus courte."""
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def _labels(labels: dict) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"


class Prometheus:
    def __init__(self, clock=time.time):
        self.started = clock()
        self.clock = clock
        self.counters: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
        self.histograms: dict[tuple, list] = {}  # labels -> [compte par palier…, somme, total]

    def inc(self, name: str, labels: dict, value: float = 1.0) -> None:
        if value:
            self.counters[name][tuple(sorted(labels.items()))] += value

    def observe_request(self, row: dict) -> None:
        """Ligne de métrique d'une requête (6.1), au moment de sa capture."""
        provider = row.get("provider") or "aucun"
        labels = {
            "provider": provider,
            "hosting": row.get("hosting") or "inconnu",
            "status": row.get("status") or "ok",
            "endpoint": row.get("endpoint") or "chat",
        }
        self.inc("gateway_requests_total", labels)
        self.inc("gateway_tokens_total", {"provider": provider, "type": "prompt"}, row.get("prompt_tokens") or 0)
        self.inc(
            "gateway_tokens_total", {"provider": provider, "type": "completion"}, row.get("completion_tokens") or 0
        )
        self.inc("gateway_cost_usd_total", {"provider": provider}, row.get("cost_usd") or 0)
        if row.get("cache_hit"):
            self.inc("gateway_cache_hits_total", {"provider": provider})
        latency = row.get("latency_ms")
        if isinstance(latency, (int, float)) and row.get("status", "ok") == "ok":
            key = (("provider", provider),)
            h = self.histograms.setdefault(key, [0] * len(LATENCY_BUCKETS) + [0.0, 0])
            seconds = latency / 1000
            for i, bound in enumerate(LATENCY_BUCKETS):
                if seconds <= bound:
                    h[i] += 1
            h[-2] += seconds
            h[-1] += 1

    def provider_failure(self, ref: str, status: int) -> None:
        self.inc("gateway_provider_failures_total", {"provider": ref.split("/", 1)[0], "status": str(status)})

    def render(self, gauges: list[tuple[str, str, str, list[tuple[dict, float]]]]) -> str:
        """Texte Prometheus : compteurs, histogramme de latence puis jauges fournies (nom, type, aide, valeurs)."""
        helps = {
            "gateway_requests_total": "Requêtes traitées (chat, messages, embeddings…).",
            "gateway_tokens_total": "Tokens consommés.",
            "gateway_cost_usd_total": "Coût réel estimé, en dollars.",
            "gateway_cache_hits_total": "Réponses servies par le cache de la gateway.",
            "gateway_provider_failures_total": "Échecs des fournisseurs (avant bascule sur une autre IA).",
        }
        out: list[str] = []
        for name in sorted(set(helps) | set(self.counters)):
            out += [f"# HELP {name} {helps.get(name, name)}", f"# TYPE {name} counter"]
            for labels, value in sorted(self.counters.get(name, {}).items()):
                out.append(f"{name}{_labels(dict(labels))} {_num(value)}")
        name = "gateway_request_latency_seconds"
        out += [f"# HELP {name} Durée des requêtes réussies.", f"# TYPE {name} histogram"]
        for labels, h in sorted(self.histograms.items()):
            base = dict(labels)
            for i, bound in enumerate(LATENCY_BUCKETS):
                out.append(f"{name}_bucket{_labels({**base, 'le': f'{bound:g}'})} {h[i]}")
            out.append(f"{name}_bucket{_labels({**base, 'le': '+Inf'})} {h[-1]}")
            out.append(f"{name}_sum{_labels(base)} {_num(h[-2])}")
            out.append(f"{name}_count{_labels(base)} {h[-1]}")
        for gname, kind, help_text, values in gauges:
            out += [f"# HELP {gname} {help_text}", f"# TYPE {gname} {kind}"]
            out += [f"{gname}{_labels(labels)} {_num(value)}" for labels, value in values]
        return "\n".join(out) + "\n"


# ---------- alertes d'exploitation ----------


def validate_settings(data) -> dict:
    if not isinstance(data, dict) or set(data) - {"webhook_url", "disk_min_gb", "enabled"}:
        raise AlertError("Réglages : webhook_url, disk_min_gb, enabled.")
    out: dict = {}
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise AlertError("'enabled' : true ou false.")
        out["enabled"] = data["enabled"]
    if "disk_min_gb" in data:
        v = data["disk_min_gb"]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 100_000:
            raise AlertError("'disk_min_gb' : nombre de Go entre 0 (désactivé) et 100000.")
        out["disk_min_gb"] = float(v)
    if "webhook_url" in data:
        url = data["webhook_url"]
        out["webhook_url"] = check_webhook_url(url) if url else None
    return out


class Supervisor:
    def __init__(self, gw, store, alerts, owner: str | None, clock=time.time):
        self.gw, self.store, self.alerts, self.owner, self.clock = gw, store, alerts, owner, clock
        self.servers: dict[str, dict] = {}  # serveur local -> {up, seen_up, failures, since, error}
        self.disk_low = False
        self.events = 0

    # -- réglages --
    def settings(self) -> dict:
        saved = self.store.get_setting(SETTING) or {}
        return {
            "enabled": saved.get("enabled", True),
            "disk_min_gb": saved.get("disk_min_gb", default_disk_min_gb()),
            "webhook_url": saved.get("webhook_url"),
            "webhook_secret": saved.get("webhook_secret"),
        }

    def set_settings(self, data) -> dict:
        changes = validate_settings(data)
        saved = {**(self.store.get_setting(SETTING) or {}), **changes}
        if changes.get("webhook_url") and not saved.get("webhook_secret"):
            saved["webhook_secret"] = new_secret()
        self.store.set_setting(SETTING, saved)
        return self.status()

    # -- état --
    def disk(self) -> dict | None:
        path = self.store.path
        if not path or path == ":memory:":
            path = os.getenv("GATEWAY_DATA_DIR", "data")
        folder = Path(path).resolve()
        folder = folder if folder.is_dir() else folder.parent
        try:
            usage = shutil.disk_usage(folder)
        except OSError:
            return None
        return {"path": str(folder), "free": usage.free, "total": usage.total}

    def local_providers(self) -> list:
        return [
            p
            for p in self.gw.providers.values()
            if p.hosting == "local"
            and p.cfg.enabled
            and p.cfg.owner is None
            and hasattr(p, "list_models")
            and getattr(p, "gate", None) is not None
        ]

    def status(self) -> dict:
        s = self.settings()
        return {
            "enabled": s["enabled"],
            "disk_min_gb": s["disk_min_gb"],
            "webhook_url": s["webhook_url"],
            "webhook_secret": s["webhook_secret"],
            "servers": [{"name": n, **v} for n, v in sorted(self.servers.items())],
            "disk": self.disk(),
            "disk_low": self.disk_low,
            "metrics_token": bool(metrics_token()),
        }

    # -- vérification périodique --
    async def _probe(self, p) -> str | None:
        try:
            await asyncio.wait_for(p.list_models(), 5)
            return None
        except (ProviderError, httpx.HTTPError, TimeoutError) as e:
            return getattr(e, "message", None) or type(e).__name__
        except Exception as e:  # serveur qui répond n'importe quoi : injoignable pour nous
            return type(e).__name__

    async def check(self) -> int:
        """Un passage : serveurs locaux et disque ; renvoie le nombre d'alertes émises."""
        settings = self.settings()
        fired = 0
        providers = self.local_providers()
        results = await asyncio.gather(*(self._probe(p) for p in providers))
        now = self.clock()
        for p, error in zip(providers, results):
            st = self.servers.setdefault(
                p.name, {"up": None, "seen_up": False, "failures": 0, "since": now, "error": None}
            )
            if error is None:
                if st["up"] is False and st["seen_up"] and settings["enabled"]:
                    fired += await self.notify(
                        "info",
                        f"Serveur local « {p.name} » rétabli",
                        f"{p.name} répond de nouveau ({p.cfg.base_url}).",
                        {"event": "local.up", "server": p.name},
                    )
                if st["up"] is not True:
                    st["since"] = now
                st.update(up=True, seen_up=True, failures=0, error=None)
                continue
            st["failures"] += 1
            st["error"] = error[:200]
            if st["failures"] >= FAILURES_BEFORE_DOWN and st["up"] is not False:
                st.update(up=False, since=now)
                if st["seen_up"] and settings["enabled"]:
                    fired += await self.notify(
                        "critical",
                        f"Serveur local « {p.name} » injoignable",
                        f"{p.name} ({p.cfg.base_url}) ne répond plus : {st['error']}. Les requêtes basculent sur "
                        "les autres IA autorisées.",
                        {"event": "local.down", "server": p.name},
                    )
        for gone in set(self.servers) - {p.name for p in providers}:
            del self.servers[gone]
        disk = self.disk()
        limit = settings["disk_min_gb"] * GB
        if disk and limit:
            if disk["free"] < limit and not self.disk_low:
                self.disk_low = True
                if settings["enabled"]:
                    fired += await self.notify(
                        "critical",
                        "Disque presque plein",
                        f"{disk['free'] / GB:.1f} Go libres sur le disque de la base ({disk['path']}), sous le "
                        f"seuil de {settings['disk_min_gb']:g} Go : métriques, sauvegardes et documents risquent "
                        "de ne plus s'écrire.",
                        {"event": "disk.low", "free_bytes": disk["free"]},
                    )
            elif disk["free"] > limit * DISK_REARM and self.disk_low:
                self.disk_low = False
                if settings["enabled"]:
                    fired += await self.notify(
                        "info",
                        "Espace disque rétabli",
                        f"{disk['free'] / GB:.1f} Go libres sur le disque de la base.",
                        {"event": "disk.ok", "free_bytes": disk["free"]},
                    )
        elif not limit:
            self.disk_low = False
        return fired

    async def notify(self, level: str, title: str, message: str, data: dict) -> int:
        event = {**data, "title": title, "message": message, "at": self.clock()}
        if self.owner:
            await asyncio.to_thread(self.store.add_notification, self.owner, level, title, message, event)
        s = self.settings()
        if s["webhook_url"]:
            await self.alerts.send(s["webhook_url"], s["webhook_secret"], event)
        self.events += 1
        log.warning("%s — %s", title, message)
        return 1

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("Supervision : vérification impossible")
            await asyncio.sleep(interval())

    # -- jauges de /metrics --
    def gauges(self, holder: dict) -> list:
        gw = holder["gw"]
        out = []
        out.append(("gateway_up", "gauge", "La gateway répond.", [({}, 1)]))
        out.append(
            (
                "gateway_uptime_seconds",
                "gauge",
                "Durée depuis le démarrage.",
                [({}, round(self.clock() - holder["prometheus"].started, 1))],
            )
        )
        servers = [({"server": n}, 1 if v["up"] else 0) for n, v in sorted(self.servers.items()) if v["up"] is not None]
        out.append(("gateway_local_server_up", "gauge", "Serveur local joignable (1) ou non (0).", servers))
        remote = holder.get("remote")
        if remote is not None:
            st = remote.status
            out.append(
                (
                    "gateway_tunnel_up",
                    "gauge",
                    "Accès distant actif et joignable.",
                    [
                        (
                            {"provider": st.provider or "aucun", "mode": st.mode or "aucun", "state": st.state},
                            1 if st.state == "running" else 0,
                        )
                    ],
                )
            )
            out.append(
                (
                    "gateway_tunnel_restarts_total",
                    "counter",
                    "Reconnexions automatiques du tunnel.",
                    [({}, st.restarts)],
                )
            )
        local = gw.local
        queues = []
        for s in local.status()["servers"]:
            queues.append(({"server": s["name"], "state": "active"}, s["active"]))
            queues.append(({"server": s["name"], "state": "waiting"}, s["waiting"]))
        out.append(
            ("gateway_local_queue_requests", "gauge", "Requêtes en cours / en attente par serveur local.", queues)
        )
        out.append(
            (
                "gateway_local_busy",
                "gauge",
                "Mode « PC occupé » actif (18.4).",
                [({}, 1 if local.busy_state()["on"] else 0)],
            )
        )
        disk = self.disk()
        if disk:
            out.append(
                ("gateway_disk_free_bytes", "gauge", "Espace libre sur le disque de la base.", [({}, disk["free"])])
            )
            out.append(("gateway_disk_total_bytes", "gauge", "Taille du disque de la base.", [({}, disk["total"])]))
        if gw.metrics:
            out.append(
                (
                    "gateway_metrics_dropped_total",
                    "counter",
                    "Lignes de métriques perdues (file pleine).",
                    [({}, gw.metrics.dropped)],
                )
            )
        return out
