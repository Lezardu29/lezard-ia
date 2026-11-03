"""Métriques (6.1), taux de change, grille tarifaire (6.2, 10.7), économies et exports (6.5)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, UTC
from types import SimpleNamespace

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from ..exports import csv_stream, json_stream, pdf_report
from ..keys import VirtualKeyError
from ..plans import PlanContext, PlanError
from ..pricing import (
    PricingEngine,
    PricingError,
    baseline_model,
    breakdown,
    auto_refresh_enabled as pricing_auto_refresh,
    savings_report,
)
from ..providers.base import ProviderError
from ..registry import BUILTIN_PRICES_DATE, usd_eur_rate
from ..store import DocumentStore
from ..validation import json_object
from ..core import Gateway, _err


def build(app: FastAPI, shared: SimpleNamespace) -> APIRouter:
    router = APIRouter()
    holder = shared.holder
    admin = shared.admin
    plan_ctx = shared.plan_ctx

    # ----- métriques & taux de change (6.1) -----
    @router.get("/v1/metrics/requests")
    async def list_metrics(
        request: Request,
        since: float | None = None,
        until: float | None = None,
        key_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        scope: str = "account",
    ):
        """Journal des requêtes, plus récentes d'abord. Clé virtuelle : ses requêtes seulement ;
        compte : tout le compte ; clé maître avec scope=all : toute l'instance."""
        ctx = plan_ctx({}, request)
        if not 1 <= limit <= 1000 or offset < 0:
            raise VirtualKeyError(400, "'limit' entre 1 et 1000, 'offset' >= 0.")
        owner, key_id = metrics_scope(ctx, scope, key_id, request)
        await holder["gw"].metrics.flush()  # lecture cohérente avec les captures en attente
        rows, total = await asyncio.to_thread(
            holder["store"].query_metrics,
            owner,
            since=since,
            until=until,
            key_id=key_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        return {"object": "list", "data": rows, "total": total, "limit": limit, "offset": offset}

    @router.get("/v1/fx")
    async def fx_rate():
        return holder["fx"].status()

    # ----- moteur de tarification & économies (6.2) -----
    @app.exception_handler(PricingError)
    async def pricing_error(_: Request, e: PricingError):
        return _err(400, str(e), "pricing")

    def metrics_scope(
        ctx: PlanContext, scope: str, key_id: str | None, request: Request
    ) -> tuple[str | None, str | None]:
        owner = ctx.identity
        if scope == "all":
            if not ctx.is_admin:
                raise PlanError(403, "scope=all est réservé à la clé maître.")
            shared.admin_session(request)  # toute l'instance : session d'administration (20.1)
            owner = None
        if ctx.key:  # une clé ne voit que ses propres requêtes
            key_id = ctx.key["id"]
        return owner, key_id

    @router.get("/v1/pricing")
    async def pricing_grid():
        return {
            "object": "list",
            "data": holder["pricing"].grid(),
            "fx": holder["fx"].status(),
            "last_import": holder["pricing"].last_import,
        }

    @router.post("/v1/pricing/estimate")
    async def pricing_estimate(request: Request):
        """{model | models[], input_tokens, output_tokens, cached_tokens?, cache_write_tokens?} :
        coût exact détaillé ; avec plusieurs modèles, classés du moins cher au plus cher."""
        body = await json_object(request)
        refs = body.get("models") or ([body["model"]] if body.get("model") else [])
        if not refs or len(refs) > 50:
            raise PricingError("'model' ou 'models' (50 max) est requis.")
        counts = {}
        for k in ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens"):
            v = body.get(k, 0)
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 100_000_000:
                raise PricingError(f"'{k}' doit être un entier entre 0 et 100 000 000.")
            counts[k] = v
        gw: Gateway = holder["gw"]
        results = []
        for ref in refs:
            if not isinstance(ref, str) or "/" not in ref:
                raise PricingError("Référence attendue : 'fournisseur/modèle'.")
            prov, mid = ref.split("/", 1)
            p = gw.providers.get(prov)
            results.append(
                breakdown(
                    gw.registry.get(prov, mid, p.hosting if p else "cloud"),
                    counts["input_tokens"],
                    counts["output_tokens"],
                    counts["cached_tokens"],
                    counts["cache_write_tokens"],
                )
            )
        results.sort(key=lambda r: r["cost_usd"])
        return results[0] if len(results) == 1 else {"object": "list", "data": results}

    @router.put("/v1/admin/pricing/{ref:path}")
    async def set_model_price(ref: str, request: Request):
        admin(request)
        info = holder["pricing"].set_price(ref, await json_object(request))
        return breakdown(info, 1_000_000, 1_000_000) | {"ref": info.ref, "source": "manual"}

    @router.delete("/v1/admin/pricing/{ref:path}")
    async def reset_model_price(ref: str, request: Request):
        admin(request)
        if not holder["pricing"].reset_price(ref):
            raise PricingError(f"Aucun prix personnalisé ou importé pour '{ref}'.")
        return {"ref": ref, "reset": True}

    @router.get("/v1/admin/pricing/unknown")
    async def unknown_prices(request: Request, days: float = 30):
        """Modèles cloud utilisés sans tarif connu (10.7) : leur coût a été compté à 0 $."""
        admin(request)
        if not 0 < days <= 366:
            raise PricingError("'days' doit être compris entre 0 et 366.")
        await holder["gw"].metrics.flush()
        p: PricingEngine = holder["pricing"]
        return {
            "object": "list",
            "data": await asyncio.to_thread(p.unknown_used, days),
            "days": days,
            "builtin_date": BUILTIN_PRICES_DATE,
            "last_import": p.last_import,
            "auto_refresh": pricing_auto_refresh(),
        }

    @router.post("/v1/admin/pricing/import")
    async def import_prices(request: Request):
        admin(request)
        gw: Gateway = holder["gw"]
        try:
            return await holder["pricing"].import_prices(gw.discovered, {n: p.hosting for n, p in gw.providers.items()})
        except httpx.HTTPError as e:
            raise ProviderError(502, f"Grille tarifaire injoignable : {e}", "pricing")

    @router.get("/v1/metrics/summary")
    async def metrics_summary(
        request: Request,
        since: float | None = None,
        until: float | None = None,
        key_id: str | None = None,
        scope: str = "account",
    ):
        """Indicateurs clés du tableau de bord (6.3) : dépense €, tokens, économies, latence."""
        ctx = plan_ctx({}, request)
        owner, key_id = metrics_scope(ctx, scope, key_id, request)
        await holder["gw"].metrics.flush()
        store: DocumentStore = holder["store"]
        out = await asyncio.to_thread(store.metrics_summary, owner, since=since, until=until, key_id=key_id)
        groups = await asyncio.to_thread(store.savings_groups, owner, since=since, until=until, key_id=key_id)
        try:
            s = savings_report(groups, baseline_model(holder["gw"].registry))
            out["savings"] = {
                "saved_eur": s["totals"]["saved_eur"],
                "saved_pct": s["totals"]["saved_pct"],
                "baseline_model": s["baseline_model"],
                "breakdown_eur": s["breakdown_eur"],
            }
        except PricingError:
            out["savings"] = None  # référence sans prix : KPI indisponible
        out["usd_eur"] = usd_eur_rate()
        return out

    @router.get("/v1/metrics/charts")
    async def metrics_charts(
        request: Request,
        since: float | None = None,
        until: float | None = None,
        key_id: str | None = None,
        scope: str = "account",
        tz_offset: int = 0,
    ):
        """Séries des graphiques du tableau de bord (6.4), en heure locale du lecteur."""
        ctx = plan_ctx({}, request)
        owner, key_id = metrics_scope(ctx, scope, key_id, request)
        if not -900 <= tz_offset <= 900:
            raise VirtualKeyError(400, "'tz_offset' (minutes) hors limites.")
        await holder["gw"].metrics.flush()
        until = until or time.time()
        store: DocumentStore = holder["store"]
        first = since or (
            (await asyncio.to_thread(store.metrics_summary, owner, key_id=key_id))["first_ts"] or until - 86400
        )
        bucket = 3600 if until - first <= 2 * 86400 else 86400  # heure sur 48 h, sinon jour
        try:
            base = baseline_model(holder["gw"].registry)
            pin, pout = base.input_per_mtok_usd / 1e6, base.output_per_mtok_usd / 1e6
        except PricingError:
            base, pin, pout = None, 0.0, 0.0
        raw = await asyncio.to_thread(
            store.chart_data,
            owner,
            since=since,
            until=until,
            key_id=key_id,
            tz_offset_min=tz_offset,
            bucket=bucket,
            base_in=pin,
            base_out=pout,
        )
        off = tz_offset * 60
        by_bucket = {b: (cost, ref, n) for b, cost, ref, n in raw["series"]}
        points, b = [], int((first - off) // bucket)
        while b <= int((until - off) // bucket) and len(points) < 800:  # seaux vides remplis à zéro
            cost, ref, n = by_bucket.get(b, (0.0, 0.0, 0))
            points.append(
                {
                    "t": b * bucket + off,
                    "cost_eur": round(cost, 6),
                    "baseline_eur": round(ref, 6) if base else None,
                    "requests": n,
                }
            )
            b += 1
        heat = [[0] * 24 for _ in range(7)]
        for day, hour, n in raw["heat"]:
            heat[day][hour] = n
        return {
            "timeseries": {
                "bucket": "hour" if bucket == 3600 else "day",
                "points": points,
                "baseline_model": base.ref if base else None,
            },
            "split": sorted(
                (
                    {"provider": p or "inconnu", "requests": n, "tokens": int(t), "cost_eur": round(c, 6)}
                    for p, n, t, c in raw["split"]
                ),
                key=lambda s: -s["requests"],
            ),
            "scatter": [
                {"latency_ms": lat, "cost_eur": round(c or 0.0, 8), "provider": p, "hosting": h, "model": m, "ts": ts}
                for lat, c, p, h, m, ts in raw["scatter"]
            ],
            "heatmap": {
                "days": ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."],
                "counts": heat,
                "max": max(max(r) for r in heat),
            },
        }

    @router.get("/v1/savings")
    async def savings(
        request: Request,
        since: float | None = None,
        until: float | None = None,
        key_id: str | None = None,
        scope: str = "account",
    ):
        """Économies cumulées vs. une référence « tout sur un modèle premium » (6.2)."""
        ctx = plan_ctx({}, request)
        owner, key_id = metrics_scope(ctx, scope, key_id, request)
        await holder["gw"].metrics.flush()
        return await asyncio.to_thread(full_savings, owner, since, until, key_id, baseline_model(holder["gw"].registry))

    def full_savings(owner, since, until, key_id, base) -> dict:
        store: DocumentStore = holder["store"]
        report = savings_report(store.savings_groups(owner, since=since, until=until, key_id=key_id), base)
        per_model = []
        for g in store.savings_groups(owner, since=since, until=until, key_id=key_id, by_model=True):
            r = savings_report([{**g, "category": "cloud"}], base)["totals"]
            per_model.append(
                {
                    "model": f"{g['provider']}/{g['model']}",
                    "requests": g["n"],
                    "cost_eur": r["cost_eur"],
                    "baseline_eur": r["baseline_eur"],
                    "saved_eur": r["saved_eur"],
                }
            )
        report["by_model"] = sorted(per_model, key=lambda m: -m["saved_eur"])
        return report

    # ----- exports CSV / JSON / PDF (6.5) -----
    @router.get("/v1/metrics/export")
    async def export_metrics(
        request: Request,
        format: str = "csv",
        since: float | None = None,
        until: float | None = None,
        key_id: str | None = None,
        scope: str = "account",
        locale: str = "fr",
        tz_offset: int = 0,
    ):
        ctx = plan_ctx({}, request)
        owner, key_id = metrics_scope(ctx, scope, key_id, request)
        if format not in ("csv", "json", "pdf") or locale not in ("fr", "en") or not -900 <= tz_offset <= 900:
            raise VirtualKeyError(400, "format : csv, json ou pdf ; locale : fr ou en.")
        await holder["gw"].metrics.flush()
        store: DocumentStore = holder["store"]
        names = store.key_names(owner)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
        disposition = lambda ext: {"Content-Disposition": f'attachment; filename="consommation-{stamp}.{ext}"'}  # noqa: E731
        rows = store.iter_metrics(owner, since=since, until=until, key_id=key_id)
        if format == "csv":
            return StreamingResponse(
                csv_stream(rows, names, locale), media_type="text/csv; charset=utf-8", headers=disposition("csv")
            )
        summary = await asyncio.to_thread(store.metrics_summary, owner, since=since, until=until, key_id=key_id)
        try:
            savings_data = await asyncio.to_thread(
                full_savings, owner, since, until, key_id, baseline_model(holder["gw"].registry)
            )
        except PricingError:
            savings_data = None
        if format == "json":
            meta = {
                "generated_at": time.time(),
                "scope": "instance" if owner is None else "compte",
                "key_id": key_id,
                "period": {"since": since, "until": until},
                "fx": holder["fx"].status(),
                "summary": summary,
                "savings": savings_data,
            }
            return StreamingResponse(
                json_stream(meta, rows, names), media_type="application/json", headers=disposition("json")
            )
        # PDF : agrégats seulement (pas de lignes brutes)
        off = tz_offset * 60
        base = savings_data and holder["gw"].registry.get(*savings_data["baseline_model"].split("/", 1))
        raw = await asyncio.to_thread(
            store.chart_data,
            owner,
            since=since,
            until=until or time.time(),
            key_id=key_id,
            tz_offset_min=tz_offset,
            bucket=86400,
            base_in=base.input_per_mtok_usd / 1e6 if base else 0.0,
            base_out=base.output_per_mtok_usd / 1e6 if base else 0.0,
        )
        # b*86400 = minuit LOCAL exprimé en « temps local » : c'est lui qui donne la date du jour
        days = [
            {
                "t": b * 86400 + off,
                "label": datetime.fromtimestamp(b * 86400, tz=UTC).strftime("%d/%m/%Y"),
                "cost_eur": c,
                "baseline_eur": r if base else None,
                "requests": n,
            }
            for b, c, r, n in raw["series"]
        ]
        fmt_d = lambda t: datetime.fromtimestamp(t - off, tz=UTC).strftime("%d/%m/%Y")  # noqa: E731
        period = f"du {fmt_d(since)} au {fmt_d(until or time.time())}" if since else "depuis le début"
        scope_label = (
            "Instance entière"
            if owner is None
            else f"Clé « {ctx.key['name']} »"
            if ctx.key
            else f"Clé {names.get(key_id, key_id)}"
            if key_id
            else "Compte"
        )
        sv = None
        if savings_data:
            sv = {
                **savings_data["totals"],
                "baseline_model": savings_data["baseline_model"],
                "breakdown_eur": savings_data["breakdown_eur"],
            }
        models = sorted(savings_data["by_model"] if savings_data else [], key=lambda m: -m["cost_eur"])
        content = await asyncio.to_thread(
            pdf_report,
            scope_label=scope_label,
            period_label=period,
            summary=summary,
            savings=sv,
            models=models,
            days=days,
            fx=holder["fx"].status(),
        )
        return Response(content, media_type="application/pdf", headers=disposition("pdf"))

    return router
