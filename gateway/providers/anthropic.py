"""Connecteur Anthropic : traduit le format OpenAI canonique <-> API Messages."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from gateway import translate
from gateway.providers.base import Provider, ProviderError


class AnthropicProvider(Provider):
    def _headers(self) -> dict:
        return {
            "x-api-key": self.cfg.resolved_key() or "",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return self.cfg.base_url.rstrip("/") + path

    async def list_models(self) -> list[str]:
        r = await self.client.get(self._url("/models"), headers=self._headers(), timeout=10)
        self._raise(r)
        return [m["id"] for m in self._json(r).get("data", []) if isinstance(m, dict) and "id" in m]

    async def chat(self, payload: dict) -> dict:
        body = translate.openai_to_anthropic(payload, payload["model"])
        try:
            r = await self.client.post(self._url("/messages"), headers=self._headers(), json=body, timeout=600)
        except httpx.HTTPError as e:
            raise ProviderError(503, f"{type(e).__name__}: {e}", self.name)
        self._raise(r)
        return translate.anthropic_to_openai(self._json(r), payload["model"])

    async def stream(self, payload: dict) -> AsyncIterator[dict]:
        body = {**translate.openai_to_anthropic(payload, payload["model"]), "stream": True}
        conv = translate.AnthropicStreamConverter(payload["model"])
        try:
            async with self.client.stream(
                "POST", self._url("/messages"), headers=self._headers(), json=body, timeout=600
            ) as r:
                if r.status_code >= 400:
                    await r.aread()
                    self._raise(r)
                async for line in r.aiter_lines():
                    if line.startswith("data:"):
                        for chunk in conv.feed(self._event(line[5:])):
                            yield chunk
        except httpx.HTTPError as e:
            raise ProviderError(503, f"{type(e).__name__}: {e}", self.name)
