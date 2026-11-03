"""Moteur de tarification & conversion (tâche 6.2).

Grille tarifaire dynamique, par priorité :
  1. prix saisis par l'administrateur (source « manual ») ;
  2. prix importés de la base publique LiteLLM (model_prices_and_context_window.json), actualisée
     chaque jour (PRICING_AUTO_REFRESH, PRICING_SOURCE_URL) — met aussi à prix les modèles cloud
     détectés mais absents de la grille intégrée ;
  3. grille intégrée (registry.CLOUD_MODELS).

Coût exact d'une requête :
  Coût($) = Input_frais × Tarif_Input + Lus_cache × Tarif_Cache_Lecture
          + Écrits_cache × Tarif_Cache_Écriture + Output × Tarif_Output
  Coût(€) = Coût($) × Taux_USD_EUR

Économies cumulées : comparées à une référence naïve « tout sur un modèle premium »
(SAVINGS_BASELINE_MODEL, défaut openai/gpt-4o), réparties en local / routage / prompt caching /
cache de réponses, converties au taux de change de chaque requête. Total NET : une requête routée
vers plus cher que la référence réduit l'économie.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from .registry import PRICE_FIELDS, ModelInfo, ModelRegistry, usd_eur_rate

log = logging.getLogger("gateway.pricing")
LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
MAX_PRICE = 1000.0  # $/M tokens : au-delà, donnée jugée aberrante
PER_M = 1_000_000


class PricingError(ValueError):
    pass


def validate_price(fields: dict) -> dict:
    if not isinstance(fields, dict) or not fields or set(fields) - set(PRICE_FIELDS):
        raise PricingError(f"Champs acceptés : {', '.join(PRICE_FIELDS)}.")
    out = {}
    for k, v in fields.items():
        if v is None:
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            raise PricingError(f"'{k}' doit être un nombre positif.")
        if k == "context_window":
            if not 256 <= v <= 100_000_000:
                raise PricingError("'context_window' hors limites.")
            out[k] = int(v)
        else:
            if v > MAX_PRICE:
                raise PricingError(f"'{k}' > {MAX_PRICE:g} $/M tokens : valeur aberrante.")
            out[k] = float(v)
    for need in ("input_per_mtok_usd", "output_per_mtok_usd"):
        if need not in out:
            raise PricingError(f"'{need}' est requis.")
    return out


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None  # OpenRouter : -1 = prix variable (routeur), inutilisable


def item_prices(item: dict) -> dict | None:
    """Tarif publié dans une entrée de /v1/models (10.7), en $/M tokens, ou None.
    - OpenRouter : "pricing": {"prompt": "0.00000015", "completion": "0.0000006", "input_cache_read": …} ($/token) ;
    - Together : "pricing": {"input": 0.88, "output": 0.88} ($/M tokens).
    Un modèle à 0 / 0 (variante gratuite) reste « sans tarif » : rien ne garantit qu'il le restera."""
    p = item.get("pricing")
    if not isinstance(p, dict):
        return None
    if "prompt" in p or "completion" in p:
        scale, keys = (
            PER_M,
            {
                "input_per_mtok_usd": "prompt",
                "output_per_mtok_usd": "completion",
                "cache_read_per_mtok_usd": "input_cache_read",
                "cache_write_per_mtok_usd": "input_cache_write",
            },
        )
    else:
        scale, keys = (
            1,
            {"input_per_mtok_usd": "input", "output_per_mtok_usd": "output", "cache_read_per_mtok_usd": "cached_input"},
        )
    fields = {k: round(v * scale, 6) for k, src in keys.items() if (v := _num(p.get(src))) is not None}
    if not (fields.get("input_per_mtok_usd") or fields.get("output_per_mtok_usd")):
        return None
    try:
        return validate_price(fields)
    except PricingError:  # incomplet ou aberrant
        return None


def breakdown(
    info: ModelInfo, input_tokens: int, output_tokens: int, cached_tokens: int = 0, cache_write_tokens: int = 0
) -> dict:
    """Détail du calcul de coût, terme par terme, en $ et en €."""
    if cached_tokens + cache_write_tokens > input_tokens:
        raise PricingError("Les tokens de cache ne peuvent pas dépasser les tokens d'entrée.")
    rate = usd_eur_rate()
    local = info.hosting == "local"
    fresh = input_tokens - cached_tokens - cache_write_tokens
    terms = [
        ("input", fresh, info.input_per_mtok_usd),
        ("cache_read", cached_tokens, info.cache_read_price),
        ("cache_write", cache_write_tokens, info.cache_write_price),
        ("output", output_tokens, info.output_per_mtok_usd),
    ]
    lines = []
    for name, tokens, price in terms:
        usd = 0.0 if local else tokens * price / PER_M
        lines.append(
            {
                "term": name,
                "tokens": tokens,
                "usd_per_mtok": 0.0 if local else price,
                "usd": round(usd, 10),
                "eur": round(usd * rate, 10),
            }
        )
    total = sum(l["usd"] for l in lines)
    return {
        "model": info.ref,
        "hosting": info.hosting,
        "usd_eur": rate,
        "lines": lines,
        "cost_usd": round(total, 10),
        "cost_eur": round(total * rate, 10),
        "priced": local or bool(info.input_per_mtok_usd or info.output_per_mtok_usd),
        "formula": "((input×tarif_in + cache_lu×tarif_cache + cache_écrit×tarif_écriture "
        "+ output×tarif_out) / 1 000 000) × taux_USD_EUR",
    }


class PricingEngine:
    def __init__(self, registry: ModelRegistry, store, client: httpx.AsyncClient):
        self.registry = registry
        self.store = store
        self.client = client
        self.last_import: dict | None = store.get_setting("pricing_import")
        self._tried: dict[str, float] = {}  # modèle sans tarif -> dernier import tenté pour lui
        self._importing = False
        for row in store.list_prices():  # surcharges persistées
            prov, mid = row["ref"].split("/", 1)
            self.registry.update_price(prov, mid, **row["fields"])

    # ---------- grille ----------
    def grid(self) -> list[dict]:
        rate = usd_eur_rate()
        sources = {r["ref"]: r for r in self.store.list_prices()}
        out = []
        for m in sorted(self.registry.all(), key=lambda m: m.ref):
            src = sources.get(m.ref)
            out.append(
                {
                    "ref": m.ref,
                    "hosting": m.hosting,
                    "context_window": m.context_window,
                    "usd_per_mtok": {
                        "input": m.input_per_mtok_usd,
                        "output": m.output_per_mtok_usd,
                        "cache_read": m.cache_read_price,
                        "cache_write": m.cache_write_price,
                    },
                    "eur_per_mtok": {
                        "input": round(m.input_per_mtok_usd * rate, 6),
                        "output": round(m.output_per_mtok_usd * rate, 6),
                    },
                    "source": src["source"] if src else "provider" if m.ref in self.registry.announced else "builtin",
                    "updated_at": src["updated_at"] if src else None,
                }
            )
        return out

    def set_price(self, ref: str, fields: dict, source: str = "manual") -> ModelInfo:
        if "/" not in ref:
            raise PricingError("Référence attendue : 'fournisseur/modèle'.")
        clean = validate_price(fields)
        prov, mid = ref.split("/", 1)
        self.store.set_price(ref, clean, source)
        if source == "manual":
            self.registry.announced.discard(ref)  # l'administrateur a le dernier mot
        return self.registry.update_price(prov, mid, **clean)

    def reset_price(self, ref: str) -> bool:
        if not self.store.delete_price(ref):
            return False
        self.registry.reset_price(ref)
        return True

    # ---------- import LiteLLM ----------
    @staticmethod
    def _candidates(provider: str, model_id: str) -> list[str]:
        return [model_id, f"{provider}/{model_id}"]

    async def import_prices(
        self, discovered: dict[str, list[str]] | None = None, hosting: dict[str, str] | None = None
    ) -> dict:
        """Met à jour les modèles cloud connus + détectés. Ne touche jamais aux prix manuels."""
        url = os.getenv("PRICING_SOURCE_URL", LITELLM_URL)
        r = await self.client.get(url, timeout=60)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise PricingError("Format de grille inattendu.")
        manual = {p["ref"] for p in self.store.list_prices() if p["source"] == "manual"}
        targets = {(m.provider, m.id) for m in self.registry.all() if m.hosting == "cloud"}
        for prov, ids in (discovered or {}).items():
            if (hosting or {}).get(prov) == "cloud":
                targets |= {(prov, i) for i in ids}
        updated, skipped = [], 0
        for prov, mid in sorted(targets):
            ref = f"{prov}/{mid}"
            entry = next((data[k] for k in self._candidates(prov, mid) if isinstance(data.get(k), dict)), None)
            # uniquement les modèles de chat (pas d'embeddings, TTS, images… dans le routage) ; un tarif publié par
            # le fournisseur lui-même (registry.announced) est plus fiable qu'une base tierce
            if (
                ref in manual
                or ref in self.registry.announced
                or not entry
                or entry.get("mode") not in (None, "chat", "responses")
            ):
                continue
            fields = {
                "input_per_mtok_usd": entry.get("input_cost_per_token"),
                "output_per_mtok_usd": entry.get("output_cost_per_token"),
                "cache_read_per_mtok_usd": entry.get("cache_read_input_token_cost"),
                "cache_write_per_mtok_usd": entry.get("cache_creation_input_token_cost"),
            }
            fields = {k: round(v * PER_M, 6) for k, v in fields.items() if isinstance(v, (int, float))}
            ctx = entry.get("max_input_tokens") or entry.get("max_tokens")
            if isinstance(ctx, int):
                fields["context_window"] = ctx
            try:
                self.set_price(ref, fields, source="litellm")
                updated.append(ref)
            except PricingError:
                skipped += 1  # donnée incomplète ou aberrante
        self.last_import = {"at": time.time(), "source": url, "updated": len(updated), "skipped": skipped}
        self.store.set_setting("pricing_import", self.last_import)
        log.info("Grille tarifaire importée : %s modèle(s) mis à jour", len(updated))
        return {**self.last_import, "models": updated}

    # ---------- modèles utilisés sans tarif (10.7) ----------
    def unknown_used(self, days: float = 30) -> list[dict]:
        """Modèles cloud réellement utilisés ces `days` derniers jours dont le coût n'est pas calculable : leurs
        requêtes ont été comptées à 0 $ (budgets et économies faussés)."""
        out = []
        for row in self.store.cloud_models_used(time.time() - days * 86400):
            info = self.registry.get(row["provider"], row["model"])
            if not info.priced:
                out.append(
                    {
                        "ref": info.ref,
                        "requests": row["requests"],
                        "tokens": int(row["tokens"]),
                        "last_used": row["last_used"],
                    }
                )
        return sorted(out, key=lambda r: -r["requests"])

    def wants_import(self, ref: str) -> bool:
        """Premier usage d'un modèle sans tarif : un import est tenté tout de suite (au plus une fois toutes les
        PRICING_UNKNOWN_RETRY_HOURS par modèle, défaut 6 h, et un seul import à la fois)."""
        now, every = time.time(), float(os.getenv("PRICING_UNKNOWN_RETRY_HOURS", "6")) * 3600
        if self._importing or now - self._tried.get(ref, 0.0) < every:
            return False
        self._tried[ref] = now
        return True

    async def import_for_unknown(self, gw) -> None:
        self._importing = True
        try:
            await self.import_prices(gw.discovered, {n: p.hosting for n, p in gw.providers.items()})
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Import de tarif pour un modèle inconnu impossible : %s", e)
        finally:
            self._importing = False

    async def run(self, gw) -> None:
        hours = float(os.getenv("PRICING_REFRESH_HOURS", "24"))
        while True:
            try:
                await self.import_prices(gw.discovered, {n: p.hosting for n, p in gw.providers.items()})
            except (httpx.HTTPError, ValueError) as e:
                log.warning("Grille tarifaire non actualisée : %s", e)
            await asyncio.sleep(hours * 3600)


def auto_refresh_enabled() -> bool:
    return os.getenv("PRICING_AUTO_REFRESH", "true").lower() in ("1", "true", "yes")


# ---------- économies ----------


def baseline_model(registry: ModelRegistry) -> ModelInfo:
    ref = os.getenv("SAVINGS_BASELINE_MODEL", "openai/gpt-4o")
    prov, mid = ref.split("/", 1)
    info = registry.get(prov, mid)
    if not (info.input_per_mtok_usd or info.output_per_mtok_usd):
        raise PricingError(f"Modèle de référence '{ref}' sans prix connu.")
    return info


def savings_report(groups: list[dict], base: ModelInfo) -> dict:
    """`groups` : agrégats SQL par catégorie (local, free, cloud, response_cache) et par modèle."""
    pin, pout = base.input_per_mtok_usd / PER_M, base.output_per_mtok_usd / PER_M

    def calc(g: dict) -> dict:
        baseline_usd = g["prompt"] * pin + g["completion"] * pout
        baseline_eur = g["prompt_fx"] * pin + g["completion_fx"] * pout
        return {
            "requests": g["n"],
            "cost_usd": g["cost"],
            "cost_eur": g["cost_fx"],
            "baseline_usd": baseline_usd,
            "baseline_eur": baseline_eur,
            "saved_usd": baseline_usd - g["cost"],
            "saved_eur": baseline_eur - g["cost_fx"],
            "prompt_cache_usd": g["saved"],
            "prompt_cache_eur": g["saved_fx"],
        }

    by_cat = {g["category"]: calc(g) for g in groups if g.get("category")}
    zero = {
        k: 0.0
        for k in (
            "requests",
            "cost_usd",
            "cost_eur",
            "baseline_usd",
            "baseline_eur",
            "saved_usd",
            "saved_eur",
            "prompt_cache_usd",
            "prompt_cache_eur",
        )
    }
    local, free, cloud, hit = (by_cat.get(k, zero) for k in ("local", "free", "cloud", "response_cache"))
    totals = {k: sum(c[k] for c in (local, free, cloud, hit)) for k in zero}
    r6 = lambda v: round(v, 6)  # noqa: E731
    return {
        "baseline_model": base.ref,
        "totals": {
            "requests": int(totals["requests"]),
            "cost_usd": r6(totals["cost_usd"]),
            "cost_eur": r6(totals["cost_eur"]),
            "baseline_usd": r6(totals["baseline_usd"]),
            "baseline_eur": r6(totals["baseline_eur"]),
            "saved_usd": r6(totals["saved_usd"]),
            "saved_eur": r6(totals["saved_eur"]),
            "saved_pct": round(100 * totals["saved_usd"] / totals["baseline_usd"], 2)
            if totals["baseline_usd"]
            else 0.0,
        },
        "breakdown_eur": {
            "local_models": r6(local["saved_eur"]),
            "free_cloud": r6(free["saved_eur"]),  # offres gratuites officielles (13.8)
            # le routage cloud gagne (ou perd) par rapport à la référence, hors effet du prompt caching
            "smart_routing": r6(cloud["saved_eur"] - cloud["prompt_cache_eur"]),
            "prompt_caching": r6(cloud["prompt_cache_eur"]),
            "response_cache": r6(hit["saved_eur"]),
        },
        "requests_by_category": {
            "local": int(local["requests"]),
            "free": int(free["requests"]),
            "cloud": int(cloud["requests"]),
            "response_cache": int(hit["requests"]),
        },
    }
