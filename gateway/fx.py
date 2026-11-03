"""Taux de change USD -> EUR actualisé (tâche 6.1).

Source : taux de référence quotidiens de la Banque centrale européenne (gratuit, sans clé),
qui publie « 1 EUR = x USD » ; on en déduit EUR par USD = 1 / x.
- Rafraîchi toutes les FX_REFRESH_HOURS (12 h) ; FX_AUTO_REFRESH=false le désactive (hors ligne).
- Dernier taux connu persisté en base : un redémarrage sans réseau garde un taux récent.
- Priorité dans registry.usd_eur_rate() : USD_EUR_RATE (forçage manuel) > taux BCE > 0.92.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time

import httpx

from . import registry

log = logging.getLogger("gateway.fx")
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_USD = re.compile(r"currency=['\"]USD['\"]\s+rate=['\"]([0-9.]+)['\"]")
_DATE = re.compile(r"time=['\"](\d{4}-\d{2}-\d{2})['\"]")


class FxRates:
    def __init__(self, store, client: httpx.AsyncClient):
        self.store = store
        self.client = client
        self.source, self.updated_at, self.as_of = "default", None, None
        saved = store.get_setting("fx_usd_eur")
        if saved:  # dernier taux connu
            registry.set_live_rate(saved["rate"])
            self.source, self.updated_at, self.as_of = saved["source"], saved["updated_at"], saved.get("as_of")

    def status(self) -> dict:
        override = os.getenv("USD_EUR_RATE")
        return {
            "usd_eur": registry.usd_eur_rate(),
            "source": "manuel (USD_EUR_RATE)" if override else self.source,
            "as_of": self.as_of,
            "updated_at": self.updated_at,
        }

    async def refresh(self) -> float:
        url = os.getenv("FX_SOURCE_URL", ECB_URL)
        r = await self.client.get(url, timeout=15)
        r.raise_for_status()
        m = _USD.search(r.text)
        if not m:
            raise ValueError("Taux USD absent de la réponse BCE.")
        eur_per_usd = round(1 / float(m.group(1)), 6)
        if not 0.3 < eur_per_usd < 3:  # garde-fou contre une donnée aberrante
            raise ValueError(f"Taux aberrant {eur_per_usd}.")
        d = _DATE.search(r.text)
        self.source, self.updated_at, self.as_of = "BCE", time.time(), d.group(1) if d else None
        registry.set_live_rate(eur_per_usd)
        self.store.set_setting(
            "fx_usd_eur",
            {"rate": eur_per_usd, "source": self.source, "updated_at": self.updated_at, "as_of": self.as_of},
        )
        log.info("Taux USD->EUR actualisé : %s (BCE du %s)", eur_per_usd, self.as_of)
        return eur_per_usd

    async def run(self) -> None:
        hours = float(os.getenv("FX_REFRESH_HOURS", "12"))
        while True:
            try:
                await self.refresh()
            except (httpx.HTTPError, ValueError) as e:
                log.warning(
                    "Taux de change non actualisé (%s) ; dernier taux conservé : %s", e, registry.usd_eur_rate()
                )
            await asyncio.sleep(hours * 3600)


def auto_refresh_enabled() -> bool:
    return os.getenv("FX_AUTO_REFRESH", "true").lower() in ("1", "true", "yes")
