"""Connecteur générique OpenAI-compatible : Ollama, vLLM, LM Studio, TGWUI, LocalAI,
LiteLLM, OpenAI, Mistral, Groq, OpenRouter."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import httpx

from ..local_context import item_context, looks_ollama, server_root
from ..pricing import item_prices
from .base import GATEWAY_HEADER, Provider, ProviderError
from .ollama_native import StreamTranslator, from_native, to_native


GEMINI_HOST = "generativelanguage.googleapis.com"
SKIP_SIGNATURE = "skip_thought_signature_validator"  # valeur documentée par Google pour ce cas


def with_thought_signatures(payload: dict) -> dict:
    """Gemini 3 exige, pour chaque appel d'outil déjà dans la conversation, la « thought_signature » qu'il a produite.
    Un appel fait par un autre modèle (secours en cours de tâche : Groq saturé, puis Gemini) n'en a pas, et Gemini
    refusait toute la requête (400). Ces appels reçoivent la valeur de contournement prévue par Google ; les
    signatures réelles (renvoyées par Gemini dans extra_content) sont gardées telles quelles."""
    msgs, changed = [], False
    for m in payload.get("messages") or []:
        calls = m.get("tool_calls") if isinstance(m, dict) else None
        if calls:
            fixed = []
            for tc in calls:
                extra = tc.get("extra_content") or {}
                google = extra.get("google") or {}
                if isinstance(tc, dict) and not google.get("thought_signature"):
                    tc = {**tc, "extra_content": {**extra, "google": {**google, "thought_signature": SKIP_SIGNATURE}}}
                    changed = True
                fixed.append(tc)
            m = {**m, "tool_calls": fixed}
        msgs.append(m)
    return {**payload, "messages": msgs} if changed else payload


def _fix_call(tc: dict, first: bool = True) -> None:
    if first:
        tc.setdefault("type", "function")
    fn = tc.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("arguments"), (dict, list)):
        fn["arguments"] = json.dumps(fn["arguments"], ensure_ascii=False)  # objet au lieu de texte JSON


def normalize_tool_calls(resp: dict) -> dict:
    """Réponse complète : appels d'outils au format OpenAI strict (16.2). Ollama et d'autres serveurs locaux
    répondent parfois finish_reason « stop » malgré un appel d'outil, sans identifiant, ou avec des arguments en
    objet : les clients (SDK OpenAI, agents) attendent « tool_calls », un id et du texte JSON."""
    for ch in resp.get("choices") or []:
        calls = (ch.get("message") or {}).get("tool_calls")
        if not calls:
            continue
        for tc in calls:
            _fix_call(tc)
            tc["id"] = tc.get("id") or f"call_{uuid.uuid4().hex[:24]}"
        if ch.get("finish_reason") in (None, "stop"):
            ch["finish_reason"] = "tool_calls"
    return resp


class ToolCallStream:
    """Même chose en flux : index manquant (un appel par fragment), id absent, fin « stop »."""

    def __init__(self):
        self.next_index = 0
        self.last: int | None = None
        self.seen = False

    def fix(self, chunk: dict) -> dict:
        for ch in chunk.get("choices") or []:
            delta = ch.get("delta") or {}
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                starts = bool(tc.get("id")) or ((tc.get("function") or {}).get("name") and "index" not in tc)
                if "index" not in tc:
                    tc["index"] = self.next_index if starts or self.last is None else self.last
                first = tc["index"] != self.last
                if first and not tc.get("id"):
                    tc["id"] = f"call_{uuid.uuid4().hex[:24]}"  # premier fragment d'un appel sans id
                self.last = tc["index"]
                self.next_index = max(self.next_index, tc["index"] + 1)
                self.seen = True
                _fix_call(tc, first)
            if self.seen and ch.get("finish_reason") == "stop":
                ch["finish_reason"] = "tool_calls"
        return chunk


class OpenAICompatProvider(Provider):
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        key = self.cfg.resolved_key()
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def _patience(self) -> float:
        """Attente maximale sans rien recevoir. Un gros modèle local qui déborde de la carte graphique relit une
        longue demande (Claude Code : ~20 000 tokens) à quelques dizaines de tokens par seconde avant son premier
        mot : 10 minutes le coupaient en plein calcul (essai réel, Qwen 3.8 27B sur 12 Go)."""
        return 14400 if self.hosting == "local" else 600

    def _url(self, path: str) -> str:
        return self.cfg.base_url.rstrip("/") + path

    async def list_models(self) -> list[str]:
        timeout = 2.0 if self.hosting == "local" else 10.0
        self.served_by = None
        r = await self.client.get(self._url("/models"), headers=self._headers(), timeout=timeout)
        self.served_by = r.headers.get(GATEWAY_HEADER)  # une gateway Smart AI (peut-être celle-ci)
        self._raise(r)
        items = [m for m in self._json(r).get("data", []) if isinstance(m, dict) and "id" in m]
        for m in items:  # Gemini (compatible OpenAI) annonce « models/gemini-… » : nom court
            if isinstance(m["id"], str) and m["id"].startswith("models/"):
                m["id"] = m["id"][len("models/") :]
        # fenêtre de contexte quand le serveur l'annonce (vLLM : max_model_len…) — 10.6
        self.context_lengths = {m["id"]: c for m in items if (c := item_context(m))}
        # tarif quand le fournisseur le publie (OpenRouter, Together…) — 10.7
        self.prices = {m["id"]: p for m in items if (p := item_prices(m))}
        return [m["id"] for m in items]

    def _clean(self, payload: dict) -> dict:
        """Retire les directives internes de la gateway (clés préfixées par '_') ; Gemini : signatures des appels."""
        clean = {k: v for k, v in payload.items() if not k.startswith("_")}
        return with_thought_signatures(clean) if GEMINI_HOST in self.cfg.base_url else clean

    async def chat(self, payload: dict) -> dict:
        num_ctx = payload.get("_num_ctx")
        payload = self._eco({**self._clean(payload), "stream": False})
        async with self._slot(payload.get("model")):
            native = await self._native(payload, num_ctx)
            try:
                r = await self.client.post(
                    self._native_url() if native else self._url("/chat/completions"),
                    headers=self._headers(),
                    json=native or payload,
                    timeout=self._patience(),
                )
            except httpx.HTTPError as e:
                raise ProviderError(503, f"{type(e).__name__}: {e}", self.name)
        self._raise(r)
        resp = from_native(self._json(r), payload["model"]) if native else self._json(r)
        if not resp.get("choices"):
            raise ProviderError(502, f"Réponse du fournisseur sans 'choices' : {r.text[:200]}", self.name)
        return normalize_tool_calls(resp)

    def _eco(self, payload: dict) -> dict:
        """Serveur local en mode économie : réponse plafonnée (18.4)."""
        return self.gate.adjust(payload) if self.gate else payload

    async def _native(self, payload: dict, num_ctx: int | None = None) -> dict | None:
        """Mode économie, Ollama de ce PC : corps /api/chat avec le chargement bridé (18.7) ; sinon None.
        `num_ctx` (contrôle du PC) : fenêtre de contexte demandée à Ollama pour cette requête seulement."""
        body = await self.gate.manager.eco_request(self, payload) if self.gate else None
        if num_ctx and looks_ollama(self.name, self.cfg.base_url):
            body = body or to_native(payload, {}, None)
            if body:
                body["options"]["num_ctx"] = max(num_ctx, body["options"].get("num_ctx") or 0)
        return body

    def _native_url(self) -> str:
        return f"{server_root(self.cfg.base_url)}/api/chat"

    async def transcribe(self, model: str, audio: bytes, filename: str, kind: str, fields: dict) -> dict | bytes:
        """POST /audio/transcriptions (13.9) : dict pour json / verbose_json, octets pour text / srt / vtt."""
        headers = {k: v for k, v in self._headers().items() if k != "Content-Type"}  # multipart : httpx le pose
        mime = {
            "webm": "audio/webm",
            "ogg": "audio/ogg",
            "wav": "audio/wav",
            "flac": "audio/flac",
            "mp4": "audio/mp4",
            "mp3": "audio/mpeg",
        }[kind]
        try:
            r = await self.client.post(
                self._url("/audio/transcriptions"),
                headers=headers,
                timeout=300,
                data={**fields, "model": model},
                files={"file": (filename, audio, mime)},
            )
        except httpx.HTTPError as e:
            raise ProviderError(503, f"{type(e).__name__}: {e}", self.name)
        self._raise(r)
        if fields.get("response_format", "json") in ("json", "verbose_json"):
            data = self._json(r)
            if not isinstance(data.get("text"), str):
                raise ProviderError(502, f"Réponse de transcription sans 'text' : {r.text[:200]}", self.name)
            return data
        return r.content

    async def speech(self, payload: dict, limit: int) -> bytes:
        """POST /audio/speech (19.4) : l'audio produit, vérifié (pas de JSON ni de HTML relayé), plafonné à `limit`."""
        buf = bytearray()
        async with self._slot(payload.get("model"), "speech"):
            try:
                async with self.client.stream(
                    "POST", self._url("/audio/speech"), headers=self._headers(), json=payload, timeout=300
                ) as r:
                    if r.status_code >= 400:
                        await r.aread()
                        self._raise(r)
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype.startswith("text/") or ctype.endswith("json") or ctype.endswith("xml"):
                        await r.aread()
                        raise ProviderError(
                            502,
                            f"Réponse de synthèse vocale qui n'est pas de l'audio ({ctype}) : {r.text[:200]}",
                            self.name,
                        )
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > limit:
                            raise ProviderError(502, f"Audio trop volumineux (plus de {limit} octets).", self.name)
            except httpx.HTTPError as e:
                raise ProviderError(503, f"{type(e).__name__}: {e}", self.name)
        if not buf:
            raise ProviderError(502, "Réponse de synthèse vocale vide.", self.name)
        return bytes(buf)

    async def embed(self, payload: dict) -> dict:
        """POST /embeddings (16.1) : Ollama, LM Studio, vLLM, LocalAI, OpenAI, Mistral, Gemini (compatible OpenAI)."""
        async with self._slot(payload.get("model"), "embed"):
            try:
                r = await self.client.post(self._url("/embeddings"), headers=self._headers(), json=payload, timeout=300)
            except httpx.HTTPError as e:
                raise ProviderError(503, f"{type(e).__name__}: {e}", self.name)
        self._raise(r)
        data = self._json(r)
        if not isinstance(data.get("data"), list):
            raise ProviderError(502, f"Réponse d'embeddings sans 'data' : {r.text[:200]}", self.name)
        return data

    async def stream(self, payload: dict) -> AsyncIterator[dict]:
        num_ctx = payload.get("_num_ctx")
        payload = self._eco({**self._clean(payload), "stream": True, "stream_options": {"include_usage": True}})
        async with self._slot(payload.get("model")):
            native = await self._native(payload, num_ctx)
            events = self._native_stream(native, payload["model"]) if native else self._stream(payload)
            async for event in events:
                yield event

    async def _native_stream(self, body: dict, model: str) -> AsyncIterator[dict]:
        """Flux NDJSON de /api/chat retraduit en fragments OpenAI."""
        try:
            async with self.client.stream(
                "POST", self._native_url(), headers=self._headers(), json=body, timeout=self._patience()
            ) as r:
                if r.status_code >= 400:
                    await r.aread()
                    self._raise(r)
                tools, out = ToolCallStream(), StreamTranslator(model)
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    data = self._event(line)
                    if not isinstance(data, dict):
                        raise ProviderError(502, f"Flux illisible du fournisseur : {line[:200]}", self.name)
                    if data.get("error"):
                        raise ProviderError(502, str(data["error"])[:500], self.name)
                    for chunk in out.feed(data):
                        yield tools.fix(chunk)
                    if data.get("done"):
                        return
        except httpx.HTTPError as e:
            raise ProviderError(503, f"{type(e).__name__}: {e}", self.name)

    async def _stream(self, payload: dict) -> AsyncIterator[dict]:
        try:
            async with self.client.stream(
                "POST", self._url("/chat/completions"), headers=self._headers(), json=payload, timeout=self._patience()
            ) as r:
                if r.status_code >= 400:
                    await r.aread()
                    self._raise(r)
                tools = ToolCallStream()
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    event = self._event(data)
                    yield tools.fix(event) if isinstance(event, dict) else event
        except httpx.HTTPError as e:
            raise ProviderError(503, f"{type(e).__name__}: {e}", self.name)
