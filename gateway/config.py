"""Configuration des providers (tâches 1.2 / 1.3).

Charge `providers.json` (ou $GATEWAY_CONFIG) s'il existe. Sinon, aucune IA n'est déclarée ni recherchée au
démarrage : l'administrateur les ajoute une à une depuis la page Modèles (bouton « Ajouter un modèle »), en partant
d'un fournisseur proposé (PRESETS) ou d'une URL personnalisée. GATEWAY_AUTO_DETECT=true rétablit l'ancien
comportement (serveurs locaux courants sondés + providers cloud dont la clé est en env).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass
class ProviderConfig:
    name: str
    kind: str  # "openai" (compatible OpenAI) | "anthropic"
    base_url: str
    hosting: str  # "local" | "cloud"
    api_key_env: str | None = None
    api_key: str | None = None
    enabled: bool = True
    # secret déchiffré depuis la base (4.4) ; jamais affiché (repr=False)
    stored_key: str | None = field(default=None, repr=False)
    owner: str | None = None  # pont vers des modèles locaux (7.4) : réservé à ce compte

    def resolved_key(self) -> str | None:
        """Priorité : secret chiffré en base > api_key de la config > variable d'environnement."""
        return self.stored_key or self.api_key or (os.getenv(self.api_key_env) if self.api_key_env else None)


DEFAULT_PROVIDERS: list[ProviderConfig] = [
    # Locaux (hôte : GATEWAY_LOCAL_HOST, ex. host.docker.internal quand la gateway tourne en conteneur)
    ProviderConfig("ollama", "openai", "http://localhost:11434/v1", "local"),
    ProviderConfig("lmstudio", "openai", "http://localhost:1234/v1", "local"),
    ProviderConfig("vllm", "openai", "http://localhost:8000/v1", "local"),
    ProviderConfig("textgen", "openai", "http://localhost:5000/v1", "local"),
    ProviderConfig("localai", "openai", "http://localhost:8080/v1", "local"),
    ProviderConfig("litellm", "openai", "http://localhost:4000/v1", "local"),
    # Cloud
    ProviderConfig("openai", "openai", "https://api.openai.com/v1", "cloud", "OPENAI_API_KEY"),
    ProviderConfig("anthropic", "anthropic", "https://api.anthropic.com/v1", "cloud", "ANTHROPIC_API_KEY"),
    ProviderConfig("mistral", "openai", "https://api.mistral.ai/v1", "cloud", "MISTRAL_API_KEY"),
    ProviderConfig("groq", "openai", "https://api.groq.com/openai/v1", "cloud", "GROQ_API_KEY"),
    ProviderConfig("openrouter", "openai", "https://openrouter.ai/api/v1", "cloud", "OPENROUTER_API_KEY"),
    # Mammouth AI : un abonnement, plusieurs IA (GPT, Claude, Gemini…), API compatible OpenAI
    ProviderConfig("mammouth", "openai", "https://api.mammouth.ai/v1", "cloud", "MAMMOUTH_API_KEY"),
    # Google Gemini, point d'accès compatible OpenAI (offre gratuite d'AI Studio : 13.8)
    ProviderConfig(
        "gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai", "cloud", "GEMINI_API_KEY"
    ),
]


@dataclass(frozen=True)
class Preset:
    """Fournisseur proposé dans la fenêtre d'ajout : nom, URL et type préremplis."""

    name: str
    label: str
    base_url: str
    hosting: str
    kind: str = "openai"
    key_env: str | None = None  # clé déjà présente dans .env : réutilisée si le champ Clé reste vide
    key_url: str | None = None  # où obtenir une clé


_EXTRA_PRESETS = [
    Preset(
        "deepseek", "DeepSeek", "https://api.deepseek.com/v1", "cloud", key_url="https://platform.deepseek.com/api_keys"
    ),
    Preset("xai", "xAI (Grok)", "https://api.x.ai/v1", "cloud", key_url="https://console.x.ai"),
    Preset(
        "together",
        "Together AI",
        "https://api.together.xyz/v1",
        "cloud",
        key_url="https://api.together.ai/settings/api-keys",
    ),
    Preset(
        "fireworks",
        "Fireworks AI",
        "https://api.fireworks.ai/inference/v1",
        "cloud",
        key_url="https://fireworks.ai/account/api-keys",
    ),
    Preset(
        "perplexity",
        "Perplexity",
        "https://api.perplexity.ai",
        "cloud",
        key_url="https://www.perplexity.ai/settings/api",
    ),
    Preset("cerebras", "Cerebras", "https://api.cerebras.ai/v1", "cloud", key_url="https://cloud.cerebras.ai"),
]
_LABELS = {
    "ollama": "Ollama (ce PC)",
    "lmstudio": "LM Studio (ce PC)",
    "vllm": "vLLM (ce PC)",
    "textgen": "text-generation-webui (ce PC)",
    "localai": "LocalAI (ce PC)",
    "litellm": "LiteLLM (ce PC)",
    "openai": "OpenAI",
    "anthropic": "Anthropic (Claude)",
    "mistral": "Mistral AI",
    "groq": "Groq",
    "openrouter": "OpenRouter",
    "mammouth": "Mammouth AI",
    "gemini": "Google Gemini",
}
_KEY_URLS = {
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "mistral": "https://console.mistral.ai/api-keys",
    "groq": "https://console.groq.com/keys",
    "openrouter": "https://openrouter.ai/keys",
    "mammouth": "https://info.mammouth.ai/docs/api-quick-start",
    "gemini": "https://aistudio.google.com/apikey",
}


def presets() -> list[Preset]:
    """Fournisseurs proposés à l'ajout, cloud d'abord ; les serveurs locaux suivent GATEWAY_LOCAL_HOST et sont
    absents d'une instance hébergée (son « localhost » n'est pas la machine de l'utilisateur)."""
    host = os.getenv("GATEWAY_LOCAL_HOST")
    out = []
    for p in DEFAULT_PROVIDERS:
        if p.hosting == "local" and hosted_instance():
            continue
        url = p.base_url.replace("localhost", host, 1) if p.hosting == "local" and host else p.base_url
        out.append(
            Preset(p.name, _LABELS.get(p.name, p.name), url, p.hosting, p.kind, p.api_key_env, _KEY_URLS.get(p.name))
        )
    out += _EXTRA_PRESETS
    return sorted(out, key=lambda p: p.hosting != "cloud")


def preset(name: str) -> Preset | None:
    return next((p for p in presets() if p.name == name), None)


def auto_detect() -> bool:
    """Ancien comportement : IA courantes déclarées et sondées dès le démarrage (GATEWAY_AUTO_DETECT=true)."""
    return os.getenv("GATEWAY_AUTO_DETECT", "false").lower() in ("1", "true", "yes")


def hosted_instance() -> bool:
    """Instance hébergée managée (7.4) : GATEWAY_HOSTED=true."""
    return os.getenv("GATEWAY_HOSTED", "false").lower() in ("1", "true", "yes")


def bind_address() -> str:
    """Adresse d'écoute annoncée (lancer.ps1 -Lan, ou port publié par Docker) : GATEWAY_BIND."""
    return os.getenv("GATEWAY_BIND", "").strip() or "127.0.0.1"


def lan_mode() -> bool:
    """Mode réseau local (12.1) : la gateway est joignable par les autres appareils du réseau."""
    return bind_address() not in ("127.0.0.1", "localhost", "::1")


def load_providers(path: str | None = None) -> list[ProviderConfig]:
    path = path or os.getenv("GATEWAY_CONFIG", "providers.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return [ProviderConfig(**p) for p in json.load(f)["providers"]]
    if not auto_detect():
        return []  # IA ajoutées depuis la page Modèles (custom_providers), rechargées par main.py
    # Défauts : un provider cloud sans clé est désactivé.
    out = []
    for p in DEFAULT_PROVIDERS:
        if p.hosting == "local" and hosted_instance():
            continue  # dans le conteneur cloud, « localhost » n'est pas la machine de l'utilisateur : ponts
        host = os.getenv("GATEWAY_LOCAL_HOST")
        if p.hosting == "local" and host:
            p = ProviderConfig(**{**p.__dict__, "base_url": p.base_url.replace("localhost", host, 1)})

        if p.hosting == "cloud" and not p.resolved_key():
            p = ProviderConfig(**{**p.__dict__, "enabled": False})
        out.append(p)
    return out
