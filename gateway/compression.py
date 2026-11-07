"""Compression dynamique de l'historique de discussion (tâche 3.4).

Quand la conversation dépasse la fenêtre de contexte autorisée (projet ou `max_context_tokens`),
les échanges les plus anciens sont remplacés par un résumé ; les plus récents restent intacts.

- Résumeur : SUMMARY_MODEL (ex. "ollama/llama3.1:8b") sinon routage économique / effort bas
  (modèle local gratuit en priorité), avec repli sur les alternatives en cas d'échec.
- Incrémental : les résumés sont mis en cache par préfixe de conversation ; au tour suivant,
  seul le nouveau segment est résumé (résumé précédent + nouveaux messages).
- Gros historiques : résumé par lots successifs adaptés à la fenêtre du résumeur.
- Modes : summary (défaut) | trim (suppression des plus anciens, comportement 3.2) | off.
- Si le résumé échoue, repli automatique sur `trim`.
- Compactage à la demande (commande /compacte du chat) : toute la conversation devient un résumé.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .classifier import _text_and_images, classify
from .health import is_retryable
from .permissions import model_allowed
from .providers.base import ProviderError, strip_reasoning
from .registry import ModelInfo, usage_tokens
from .router import RoutingError, route
from .workspaces import message_tokens, trim_to_window

if TYPE_CHECKING:
    from .main import Gateway
    from .plans import PlanContext

MODES = ("summary", "trim", "off")
SUMMARY_TAG = "Résumé des échanges précédents"
MAX_MSG_CHARS = 16_000  # un message très long est tronqué dans la transcription
SUMMARIZER_PROMPT = (
    "Tu résumes une conversation entre un utilisateur et un assistant IA, pour que l'assistant "
    "puisse la poursuivre sans l'historique complet. Rédige un résumé factuel et dense, dans la "
    "langue de la conversation, qui conserve : les objectifs et préférences de l'utilisateur, "
    "les décisions prises, les faits, chiffres, noms et références, le code ou les commandes "
    "importants (verbatim s'ils sont courts), et les questions encore en suspens. "
    "Pas de préambule ni de commentaire : uniquement le résumé, en {max_words} mots maximum."
)


class CompressionError(ValueError):
    pass


def normalize_mode(value) -> str:
    v = str(value or os.getenv("HISTORY_COMPRESSION", "summary")).strip().lower()
    v = {
        "auto": "summary",
        "resume": "summary",
        "résumé": "summary",
        "summarize": "summary",
        "truncate": "trim",
        "tronquer": "trim",
        "none": "off",
        "false": "off",
    }.get(v, v)
    if v not in MODES:
        raise CompressionError(f"compression inconnue '{value}'. Valeurs : summary, trim, off.")
    return v


# ---------- cache de résumés (par préfixe) ----------


def _prefix_hashes(owner: str, messages: list[dict]) -> list[str]:
    """h[i] identifie messages[:i+1] (hash chaîné, calcul en O(n))."""
    out, h = [], hashlib.sha256(owner.encode()).hexdigest()
    for m in messages:
        h = hashlib.sha256((h + json.dumps(m, sort_keys=True, ensure_ascii=False, default=str)).encode()).hexdigest()
        out.append(h)
    return out


class SummaryCache:
    def __init__(self, max_entries: int | None = None):
        self.max_entries = max_entries or int(os.getenv("SUMMARY_CACHE_MAX", "2000"))
        self._data: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str) -> str | None:
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        return None

    def put(self, key: str, summary: str) -> None:
        self._data[key] = summary
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)


# ---------- transcription & appel au résumeur ----------


def transcript(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = m.get("role")
        text, images = _text_and_images(m.get("content"))
        if len(text) > MAX_MSG_CHARS:
            text = text[:MAX_MSG_CHARS] + " […]"
        if images:
            text += " " + " ".join(["[image]"] * images)
        if role == "assistant" and m.get("tool_calls"):
            calls = ", ".join(
                f"{tc['function']['name']}({tc['function'].get('arguments', '')[:500]})" for tc in m["tool_calls"]
            )
            text = f"{text}\n[appel d'outil : {calls}]".strip()
        if role == "system" and text.startswith(SUMMARY_TAG):
            label = "Résumé antérieur"
        else:
            label = {
                "user": "Utilisateur",
                "assistant": "Assistant",
                "tool": "Résultat d'outil",
                "system": "Consigne",
            }.get(role, role)
        lines.append(f"{label} : {text}")
    return "\n\n".join(lines)


@dataclass
class Summarizer:
    gw: Gateway
    ctx: PlanContext
    max_tokens: int
    focus: str | None = None  # consigne de l'utilisateur (/compacte garde le code)
    calls: list[dict] = field(default_factory=list)  # {ref, prompt_tokens, completion_tokens, cost_usd}

    def _chain(self, sample: str) -> list[ModelInfo]:
        forced = os.getenv("SUMMARY_MODEL")
        models = self.gw._routable(self.ctx)
        if forced:
            prov, mid = forced.split("/", 1)
            # pont d'un autre compte : jamais l'historique de celui-ci (comme le routage normal)
            p = self.gw.providers.get(prov) if self.gw.visible(prov, self.ctx.identity) else None
            info = self.gw.registry.get(prov, mid, p.hosting) if p else None
            # le résumeur imposé doit aussi respecter les permissions de la clé (ex. local only),
            # et avoir un tarif connu si la clé a un budget (8.3)
            if (
                info
                and model_allowed(self.ctx.permissions, info)
                and (info.priced or not (self.ctx.limits or {}).get("budget"))
            ):
                return [info]
        cls = classify({"messages": [{"role": "user", "content": sample}], "max_tokens": self.max_tokens})
        d = route(cls, "economy", models, self.gw.latency, effort="low")
        by_ref = {m.ref: m for m in models}
        return [by_ref[r] for r in [d.ref] + d.alternatives[:2] if r in by_ref]

    async def summarize(self, previous: str | None, new_messages: list[dict]) -> str:
        """Résume `new_messages` en intégrant le résumé `previous` ; par lots si nécessaire."""
        text = transcript(new_messages)
        chain = self._chain(text[:20_000])
        if not chain:
            raise RoutingError("Aucun modèle disponible pour résumer.", 503)
        window = min(m.context_window for m in chain)
        batch_chars = max(4000, int(window * 0.6 - self.max_tokens) * 4)
        summary = previous
        for start in range(0, len(text), batch_chars):
            summary = await self._call(chain, summary, text[start : start + batch_chars])
        return summary or ""

    async def _call(self, chain: list[ModelInfo], previous: str | None, segment: str) -> str:
        content = (
            f"Résumé existant à compléter :\n{previous}\n\nSuite de la conversation :\n{segment}"
            if previous
            else f"Conversation :\n{segment}"
        )
        prompt = SUMMARIZER_PROMPT.format(max_words=int(self.max_tokens * 0.7))
        if self.focus:
            prompt += f" Consigne de l'utilisateur pour ce résumé : {self.focus}"
        body = {
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": content},
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0.2,
        }
        last: ProviderError | None = None
        for info in chain:
            p = self.gw.providers[info.provider]
            try:
                resp = await p.chat(self.gw.upstream_payload(body, info, "low", "off", None))
            except ProviderError as e:
                self.gw.health.mark_failure(info.ref, e)
                last = e
                if is_retryable(e):
                    continue
                raise
            self.gw.health.mark_success(info.ref)
            usage = resp.get("usage") or {}
            i, o, _, _ = usage_tokens(usage)
            self.calls.append(
                {"ref": info.ref, "prompt_tokens": i, "completion_tokens": o, "cost_usd": info.usage_cost(usage)}
            )
            text = strip_reasoning(resp["choices"][0]["message"].get("content"))  # sans « <think>… »
            if text:
                return text
        raise last or ProviderError(502, "Résumé vide.", "compression")


# ---------- compression ----------


def _cut_index(convo: list[dict], recent_budget: int) -> int:
    """Premier index conservé tel quel : les plus récents qui tiennent dans le budget,
    au moins le dernier message, sans commencer par un résultat d'outil ni par l'assistant."""
    k, used = len(convo), 0
    while k > 0 and used + message_tokens(convo[k - 1]) <= recent_budget:
        k -= 1
        used += message_tokens(convo[k])
    k = min(k, len(convo) - 1)
    while k < len(convo) - 1 and convo[k].get("role") in ("tool", "assistant"):
        k += 1
    return k


async def compress_history(
    gw: Gateway,
    ctx: PlanContext,
    cache: SummaryCache,
    messages: list[dict],
    limit: int,
    reserve: int = 0,
    mode: str = "summary",
) -> tuple[list[dict], dict | None]:
    """Fait tenir `messages` dans `limit - reserve` tokens. Retourne (messages, rapport | None)."""
    budget = limit - reserve
    total = sum(map(message_tokens, messages))
    if mode == "off" or total <= budget:
        return messages, None
    if mode == "trim":
        out, dropped = trim_to_window(messages, limit, reserve)
        return out, {"mode": "trim", "trimmed_messages": dropped}

    system = [m for m in messages if m.get("role") in ("system", "developer")]
    convo = [m for m in messages if m.get("role") not in ("system", "developer")]
    sys_tokens = sum(map(message_tokens, system))
    summary_tokens = max(128, min(int(os.getenv("SUMMARY_MAX_TOKENS", "800")), budget // 4))
    k = _cut_index(convo, budget - sys_tokens - summary_tokens - 40)
    old, recent = convo[:k], convo[k:]
    if not old:
        out, dropped = trim_to_window(messages, limit, reserve)  # 413 si impossible
        return out, {"mode": "trim", "trimmed_messages": dropped}

    hashes = _prefix_hashes(ctx.identity, old)
    report = {
        "mode": "summary",
        "summarized_messages": len(old),
        "kept_messages": len(recent),
        "original_tokens": total,
    }
    summary = cache.get(hashes[-1])
    summarizer = Summarizer(gw, ctx, summary_tokens)
    if summary is not None:
        report["cache"] = "hit"
    else:
        start, previous = 0, None
        for i in range(len(old) - 1, -1, -1):  # plus long préfixe déjà résumé
            if (cached := cache.get(hashes[i])) is not None:
                start, previous = i + 1, cached
                break
        report["cache"] = "partial" if previous else "miss"
        try:
            summary = await summarizer.summarize(previous, old[start:])
        except (ProviderError, RoutingError) as e:
            out, dropped = trim_to_window(messages, limit, reserve)
            return out, {
                "mode": "trim",
                "trimmed_messages": dropped,
                "fallback_from": "summary",
                "error": getattr(e, "message", str(e))[:200],
            }
        cache.put(hashes[-1], summary)

    note = {"role": "system", "content": f"{SUMMARY_TAG} ({len(old)} messages) :\n{summary}"}
    out = system + [note] + recent
    if sum(map(message_tokens, out)) > budget:  # résumé plus long que prévu -> on rogne
        out, dropped = trim_to_window(out, limit, reserve)
        report["trimmed_messages"] = dropped
    report.update(
        final_tokens=sum(map(message_tokens, out)),
        summary_tokens=message_tokens(note),
        summarizer=[c["ref"] for c in summarizer.calls],
        cost_usd=round(sum(c["cost_usd"] for c in summarizer.calls), 8),
        tokens_used=sum(c["prompt_tokens"] + c["completion_tokens"] for c in summarizer.calls),
    )
    return out, report


async def compact(gw: Gateway, ctx: PlanContext, messages: list[dict], focus: str | None = None) -> dict:
    """Résume toute la conversation (consignes système exclues) : le résumé remplace l'historique.
    Un résumé antérieur (message système SUMMARY_TAG) est repris dans le nouveau."""
    convo = [
        m
        for m in messages
        if m.get("role") not in ("system", "developer") or _text_and_images(m.get("content"))[0].startswith(SUMMARY_TAG)
    ]
    if not any(m.get("role") in ("user", "assistant") for m in convo):
        raise CompressionError("Rien à compacter : la conversation est vide.")
    summary_tokens = max(800, int(os.getenv("SUMMARY_MAX_TOKENS", "800")))
    summarizer = Summarizer(gw, ctx, summary_tokens, focus=(focus or "").strip()[:500] or None)
    summary = await summarizer.summarize(None, convo)
    content = f"{SUMMARY_TAG} ({len(convo)} messages) :\n{summary}"
    return {
        "summary": content,
        "summarized_messages": len(convo),
        "original_tokens": sum(map(message_tokens, convo)),
        "summary_tokens": message_tokens({"role": "system", "content": content}),
        "summarizer": [c["ref"] for c in summarizer.calls],
        "cost_usd": round(sum(c["cost_usd"] for c in summarizer.calls), 8),
        "tokens_used": sum(c["prompt_tokens"] + c["completion_tokens"] for c in summarizer.calls),
    }
