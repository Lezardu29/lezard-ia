"""Offres gratuites OFFICIELLES des fournisseurs d'IA (13.8), par clé d'API — pas de sessions web (écartées en 14.1).

Une offre activée (page Modèles) rend ses modèles gratuits (0 €, catégorie « cloud gratuit » des FinOps) et
préremplit les règles du fournisseur (ProviderGuard, 9.6) avec des quotas PRUDENTS : les quotas réels dépendent du
compte et ne sont plus tous publiés (Google, Groq : console du compte). Quota atteint (compteur de la gateway ou
refus 429 du fournisseur) : le routage bascule sur une autre IA (fallback 2.5). Les modèles « :free »
d'OpenRouter sont gratuits même sans activer l'offre.

Usage des conversations pour l'entraînement : conditions relevées le CHECKED (sources dans `terms_url`).
"""

from __future__ import annotations

import fnmatch
from dataclasses import asdict, dataclass, field

CHECKED = "2026-10-19"


@dataclass(frozen=True)
class FreeTier:
    provider: str
    label: str
    base_url: str
    key_env: str
    signup_url: str
    terms_url: str
    models: tuple[str, ...]  # motifs des modèles couverts par l'offre gratuite
    rules: dict = field(default_factory=dict)  # règles préremplies (rpm, rpd, tpm…)
    training: str = "unknown"  # yes | no | depends | region
    training_note: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {**asdict(self), "models": list(self.models), "checked": CHECKED}


TIERS = {
    t.provider: t
    for t in (
        FreeTier(
            "gemini",
            "Google Gemini (AI Studio)",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "GEMINI_API_KEY",
            "https://aistudio.google.com/apikey",
            "https://ai.google.dev/gemini-api/terms",
            ("gemini-*flash*", "gemma-*"),
            {"rpm": 10, "rpd": 250, "tpm": 250_000},
            "region",
            "Hors Espace économique européen, Suisse et Royaume-Uni : Google utilise les requêtes et réponses de "
            "l'offre gratuite pour améliorer ses produits, et des relecteurs humains peuvent les lire. Dans l'EEE, en "
            "Suisse et au Royaume-Uni, les règles de l'offre payante s'appliquent : pas d'utilisation pour améliorer "
            "les produits.",
            "Modèles Flash et Gemma seulement (Pro réservé à l'offre payante). Dans l'EEE, en Suisse et au "
            "Royaume-Uni, Google exige l'offre payante pour une application mise à disposition d'autres "
            "utilisateurs : l'offre gratuite convient à votre usage personnel, pas aux clés distribuées à des tiers.",
        ),
        FreeTier(
            "groq",
            "Groq",
            "https://api.groq.com/openai/v1",
            "GROQ_API_KEY",
            "https://console.groq.com/keys",
            "https://console.groq.com/docs/your-data",
            ("*",),
            {"rpm": 30, "rpd": 1000, "tpm": 6000},
            "no",
            "Groq ne conserve pas les requêtes par défaut (sa page ne parle pas d'entraînement : sans conservation, "
            "pas d'entraînement possible) ; journaux temporaires (30 jours au plus) en cas d'erreur ou de suspicion "
            "d'abus. Zero Data Retention activable dans les réglages du compte.",
            "Quotas par modèle, affichés dans la console Groq (Settings → Limits).",
        ),
        FreeTier(
            "mistral",
            "Mistral AI (plan Experiment)",
            "https://api.mistral.ai/v1",
            "MISTRAL_API_KEY",
            "https://console.mistral.ai/api-keys",
            "https://help.mistral.ai/en/articles/455206",
            ("*",),
            {"rpm": 60, "tpm": 500_000},
            "yes",
            "Les requêtes du plan gratuit Experiment peuvent servir à entraîner les modèles de Mistral.",
            "Numéro de téléphone vérifié, sans carte bancaire.",
        ),
        FreeTier(
            "openrouter",
            "OpenRouter (modèles « :free »)",
            "https://openrouter.ai/api/v1",
            "OPENROUTER_API_KEY",
            "https://openrouter.ai/keys",
            "https://openrouter.ai/docs/features/privacy-and-logging",
            ("*:free",),
            {"models": {"allow": ["*:free"]}, "rpm": 20, "rpd": 50},
            "depends",
            "Dépend du fournisseur qui sert le modèle : certains gardent ou utilisent les requêtes. Le compte "
            "OpenRouter a un réglage de confidentialité séparé pour les modèles gratuits.",
            "50 requêtes par jour (1 000 après 10 $ de crédits achetés), 20 par minute. Activer l'offre limite ce "
            "fournisseur à ses modèles « :free ».",
        ),
    )
}


def covers(provider: str, model_id: str, enabled: set[str]) -> bool:
    """Le modèle est-il gratuit ? « :free » d'OpenRouter toujours ; sinon offre activée et modèle couvert."""
    m = model_id.lower()
    if provider == "openrouter" and m.endswith(":free"):
        return True
    tier = TIERS.get(provider)
    return bool(tier and provider in enabled and any(fnmatch.fnmatch(m, p) for p in tier.models))


def apply_rules(current: dict, tier: FreeTier) -> dict:
    """Règles du fournisseur avec les quotas de l'offre : les règles déjà réglées à la main sont gardées."""
    return {**tier.rules, **current}


def remove_rules(current: dict, tier: FreeTier) -> dict:
    """Désactivation : retire les quotas préremplis restés à leur valeur d'origine (pas ceux modifiés)."""
    return {k: v for k, v in current.items() if tier.rules.get(k) != v}
