from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from collections.abc import AsyncIterator

import httpx

from ..config import ProviderConfig

GATEWAY_HEADER = "X-Smart-Gateway"  # empreinte de l'installation, ajoutée à chaque réponse de la gateway


def instance_tag(instance: str) -> str:
    """Ce que l'en-tête GATEWAY_HEADER montre de l'installation : une empreinte, pas l'identifiant. Envoyé à tout
    visiteur (tunnel public compris), l'identifiant suffisait à réserver le tunnel de ce PC sur lezard-ia.fr avant lui
    (son nom en dépend), qui recevait ensuite un refus définitif. L'empreinte suffit à se reconnaître soi-même."""
    return hashlib.sha256(f"lezard-instance:{instance}".encode()).hexdigest()[:24]


def is_self(served_by: str | None, instance: str | None) -> bool:
    """Ce serveur est-il cette gateway ? (empreinte, ou identifiant envoyé par une version précédente)"""
    return bool(served_by and instance) and served_by in (instance_tag(instance), instance)


# réflexion des modèles qui raisonnent (DeepSeek-R1, Qwen3… servis par LM Studio, llama.cpp, vLLM) : « <think>…</think> »
# en tête du contenu, parfois sans balise fermante quand la réponse est coupée par max_tokens
_THINK = re.compile(r"^\s*<think>.*?(?:</think>|$)", re.S | re.I)


def strip_reasoning(text: str | None) -> str:
    """Contenu d'une réponse sans le bloc de réflexion du début (titres, résumés, plans : la réponse seule)."""
    return _THINK.sub("", text or "", count=1).strip()


class ProviderError(Exception):
    def __init__(self, status: int, message: str, provider: str, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.provider = provider
        self.retry_after = retry_after


class LocalUnavailable(ProviderError):
    """Serveur local indisponible pour CET appelant (PC occupé, file d'attente pleine, 18.4) : pas une panne — la
    santé du serveur n'est pas touchée et le secours passe à l'IA suivante."""


class Provider:
    """Interface commune. Format canonique = OpenAI chat/completions."""

    def __init__(self, cfg: ProviderConfig, client: httpx.AsyncClient):
        self.cfg = cfg
        self.client = client
        self.context_lengths: dict[str, int] = {}  # fenêtres annoncées par /models (10.6)
        self.prices: dict[str, dict] = {}  # tarifs annoncés par /models, $/M tokens (10.7)
        self.served_by: str | None = None  # en-tête GATEWAY_HEADER de la dernière détection
        self.gate = None  # file d'attente d'un serveur local (18.4)

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def hosting(self) -> str:
        return self.cfg.hosting

    def _slot(self, model: str | None = None, kind: str = "chat"):
        """Place dans la file d'attente du serveur local (18.4) pendant l'appel ; rien pour le cloud."""
        return self.gate.slot(model, kind) if self.gate else nullcontext()

    def _headers(self) -> dict:
        raise NotImplementedError

    async def list_models(self) -> list[str]:
        raise NotImplementedError

    async def chat(self, payload: dict) -> dict:
        raise NotImplementedError

    def stream(self, payload: dict) -> AsyncIterator[dict]:
        """Itère sur des chunks au format OpenAI `chat.completion.chunk`."""
        raise NotImplementedError

    def _json(self, r: httpx.Response) -> dict:
        """Corps JSON d'une réponse du fournisseur ; une réponse illisible devient une erreur 502 (10.1)."""
        try:
            data = r.json()
        except ValueError:
            raise ProviderError(502, f"Réponse illisible du fournisseur (pas du JSON) : {r.text[:200]}", self.name)
        if not isinstance(data, dict):
            raise ProviderError(502, "Réponse inattendue du fournisseur (objet JSON attendu).", self.name)
        return data

    def _event(self, data: str):
        """Une ligne SSE `data:` du fournisseur ; illisible -> erreur 502 (et non une exception en plein flux)."""
        try:
            return json.loads(data)
        except ValueError:
            raise ProviderError(502, f"Flux illisible du fournisseur : {data[:200]}", self.name)

    def _raise(self, r: httpx.Response) -> None:
        if r.status_code >= 400:
            try:
                retry_after = float(r.headers.get("retry-after", ""))
            except ValueError:
                retry_after = None
            raise ProviderError(r.status_code, r.text[:500], self.name, retry_after)
