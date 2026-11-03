"""Prompt caching & cache de réponses (tâche 3.3).

1. Prompt caching côté provider (réutilisation du KV cache des contextes lourds) :
   - Anthropic : la gateway pose les points de cache `cache_control` (tools, système =
     prompt projet + documents, puis fin de conversation pour le multi-tours).
     Lecture facturée ~10 % du prix input, écriture ~125 %.
   - OpenAI : cache automatique des préfixes >= 1024 tokens ; la gateway envoie un
     `prompt_cache_key` stable (par client/projet) pour maximiser les hits (~50 % de remise).
   - Local (Ollama, vLLM…) : réutilisation de préfixe native ; la gateway garantit un préfixe
     stable (prompt projet -> documents -> conversation).
   Politique : `prompt_cache` = auto (défaut, seulement si le préfixe est assez long) | on | off.

2. Cache de réponses (exact-match, côté gateway, opt-in via `cache`) : une requête identique
   (même client, mêmes messages et paramètres) est servie sans appel au modèle, à 0 €.
   Mémoire LRU + TTL, bornée (RESPONSE_CACHE_MAX entrées, RESPONSE_CACHE_MAX_OWNERS statistiques), propre à
   chaque processus. Volontairement non partagé (contrairement aux limites et quotas, tâche 10.3 : SQLite ou Redis) :
   un cache vide après redémarrage ne coûte qu'un appel au modèle, jamais un dépassement de limite.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from .registry import ModelInfo
from .workspaces import message_tokens

POLICIES = ("auto", "on", "off")
MIN_CACHE_TOKENS = 1024  # en dessous, les providers ne cachent pas
GEN_PARAMS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "seed",
    "tools",
    "tool_choice",
    "response_format",
    "n",
    "presence_penalty",
    "frequency_penalty",
    "logit_bias",
    "reasoning_effort",
)


class CacheError(ValueError):
    pass


def normalize_policy(value) -> str:
    v = str(value if value is not None else os.getenv("PROMPT_CACHE", "auto")).strip().lower()
    v = {"true": "on", "1": "on", "yes": "on", "false": "off", "0": "off", "no": "off"}.get(v, v)
    if v not in POLICIES:
        raise CacheError(f"prompt_cache inconnu '{value}'. Valeurs : auto, on, off.")
    return v


def cache_key(identity: str, project_id: str | None) -> str:
    """Clé de routage de cache stable, sans exposer l'identité du client."""
    return "gw-" + hashlib.sha256(f"{identity}|{project_id or ''}".encode()).hexdigest()[:32]


def apply_prompt_cache(payload: dict, info: ModelInfo, provider: str, kind: str, policy: str, key: str | None) -> dict:
    """Ajoute les directives de prompt caching propres au provider."""
    if policy == "off" or info.hosting == "local":
        return payload
    if kind == "anthropic":
        msgs = payload.get("messages", [])
        system = [m for m in msgs if m.get("role") in ("system", "developer")]
        convo = [m for m in msgs if m.get("role") not in ("system", "developer")]
        prefix = sum(map(message_tokens, system)) + len(json.dumps(payload.get("tools") or [])) // 4
        total = prefix + sum(map(message_tokens, convo))
        force = policy == "on"
        marks = {
            "tools": bool(payload.get("tools")) and (force or prefix >= MIN_CACHE_TOKENS),
            "system": bool(system) and (force or prefix >= MIN_CACHE_TOKENS),
            # multi-tours : l'historique est réutilisé au tour suivant
            "messages": force or (len(convo) > 1 and total >= MIN_CACHE_TOKENS),
        }
        if any(marks.values()):
            return {**payload, "_prompt_cache": marks}
        return payload
    if provider == "openai" and key:
        return {**payload, "prompt_cache_key": key}
    return payload


# ---------- cache de réponses ----------


def response_ttl(value) -> int | None:
    """`cache` de la requête : true/false, un TTL en secondes, ou {"ttl": n}. Défaut : RESPONSE_CACHE_TTL
    si RESPONSE_CACHE=on, sinon désactivé."""
    default_ttl = int(os.getenv("RESPONSE_CACHE_TTL", "3600"))
    if value is None:
        value = os.getenv("RESPONSE_CACHE", "off")
    if isinstance(value, dict):
        value = value.get("ttl", True)
    if isinstance(value, bool):
        return default_ttl if value else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < 0:
            raise CacheError("Le TTL de cache doit être >= 0.")
        return int(value) or None
    v = str(value).strip().lower()
    if v in ("on", "true", "1", "yes"):
        return default_ttl
    if v in ("off", "false", "0", "no", ""):
        return None
    try:
        return response_ttl(int(v))
    except ValueError:
        raise CacheError(f"Valeur de cache invalide '{value}'. Utiliser true/false ou un TTL en secondes.")


def response_key(identity: str, requested_model: str, body: dict, extra: dict) -> str:
    material = {
        "owner": identity,
        "model": requested_model,
        "messages": body.get("messages"),
        **{k: body[k] for k in GEN_PARAMS if body.get(k) is not None},
        **extra,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


@dataclass
class CacheEntry:
    owner: str
    response: dict
    meta: dict  # provider, model, hosting, cost_usd d'origine
    created: float
    expires: float


@dataclass
class OwnerStats:
    hits: int = 0
    misses: int = 0
    saved_usd: float = 0.0


class ResponseCache:
    def __init__(self, max_entries: int | None = None, clock=time.time):
        self.max_entries = max_entries or int(os.getenv("RESPONSE_CACHE_MAX", "1000"))
        self.clock = clock
        self._data: OrderedDict[str, CacheEntry] = OrderedDict()
        # statistiques par propriétaire, bornées (10.3) : chaque compte est un propriétaire distinct
        self.max_owners = int(os.getenv("RESPONSE_CACHE_MAX_OWNERS", "10000"))
        self._stats: OrderedDict[str, OwnerStats] = OrderedDict()
        self._next_purge = 0.0

    def _st(self, owner: str) -> OwnerStats:
        st = self._stats.get(owner)
        if st is None:
            st = self._stats[owner] = OwnerStats()
            while len(self._stats) > self.max_owners:
                self._stats.popitem(last=False)
        else:
            self._stats.move_to_end(owner)
        return st

    def get(self, key: str, owner: str) -> CacheEntry | None:
        e = self._data.get(key)
        if e and e.expires <= self.clock():
            del self._data[key]
            e = None
        if not e:
            self._st(owner).misses += 1
            return None
        self._data.move_to_end(key)
        st = self._st(owner)
        st.hits += 1
        st.saved_usd += e.meta.get("cost_usd", 0.0)
        return CacheEntry(e.owner, copy.deepcopy(e.response), e.meta, e.created, e.expires)

    def put(self, key: str, owner: str, response: dict, meta: dict, ttl: int) -> None:
        now = self.clock()
        if now >= self._next_purge:  # entrées expirées libérées sans attendre l'éviction LRU
            self._next_purge = now + 60
            for k in [k for k, e in self._data.items() if e.expires <= now]:
                del self._data[k]
        clean = {k: v for k, v in response.items() if k != "x_gateway"}
        self._data[key] = CacheEntry(owner, copy.deepcopy(clean), meta, now, now + ttl)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def purge(self, owner: str) -> int:
        keys = [k for k, e in self._data.items() if e.owner == owner]
        for k in keys:
            del self._data[k]
        return len(keys)

    def stats(self, owner: str) -> dict:
        st = self._st(owner)
        now = self.clock()
        entries = sum(1 for e in self._data.values() if e.owner == owner and e.expires > now)
        total = st.hits + st.misses
        return {
            "entries": entries,
            "hits": st.hits,
            "misses": st.misses,
            "hit_rate": round(st.hits / total, 3) if total else 0.0,
            "saved_usd": round(st.saved_usd, 8),
        }


@dataclass
class StreamRecorder:
    """Reconstitue une réponse complète à partir des chunks streamés (texte seulement)."""

    content: list[str] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict | None = None
    model: str | None = None
    cacheable: bool = True

    def feed(self, chunk: dict) -> None:
        self.model = chunk.get("model") or self.model
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for ch in chunk.get("choices", []):
            d = ch.get("delta", {})
            if d.get("tool_calls"):
                self.cacheable = False
            if d.get("content"):
                self.content.append(d["content"])
            if ch.get("finish_reason"):
                self.finish_reason = ch["finish_reason"]

    def response(self) -> dict | None:
        if not self.cacheable or not self.finish_reason:
            return None
        return {
            "object": "chat.completion",
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": self.finish_reason,
                    "message": {"role": "assistant", "content": "".join(self.content)},
                }
            ],
            "usage": self.usage or {},
        }


def replay_chunks(resp: dict) -> list[dict]:
    """Rejoue une réponse en cache sous forme de chunks de streaming."""
    msg = resp["choices"][0]["message"]
    base = {"id": resp.get("id", "cached"), "object": "chat.completion.chunk", "model": resp.get("model")}
    return [
        {
            **base,
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": msg.get("content") or ""}, "finish_reason": None}
            ],
        },
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": resp["choices"][0].get("finish_reason")}]},
        {**base, "choices": [], "usage": resp.get("usage") or {}},
    ]
