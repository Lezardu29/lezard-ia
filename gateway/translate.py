"""Conversions OpenAI <-> Anthropic (texte, images, tools)."""

from __future__ import annotations

import json
import time
import uuid

from .validation import tool_arguments

EPHEMERAL = {"type": "ephemeral"}
STOP_TO_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls"}
FINISH_TO_STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "content_filter": "end_turn"}


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if p.get("type") == "text")


# ---------- OpenAI request -> Anthropic request ----------


def _img_to_anthropic(url: str) -> dict:
    if url.startswith("data:"):
        head, b64 = url.split(",", 1)
        media = head[5:].split(";")[0]
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _parts_to_anthropic(content) -> list[dict] | str:
    if isinstance(content, str):
        return content
    out = []
    for p in content or []:
        if p.get("type") == "text":
            out.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "image_url":
            u = p["image_url"]
            out.append(_img_to_anthropic(u["url"] if isinstance(u, dict) else u))
    return out


def openai_to_anthropic(req: dict, model: str) -> dict:
    system, msgs = [], []
    for m in req.get("messages", []):
        role = m["role"]
        if role in ("system", "developer"):
            system.append(_text_of(m["content"]))
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": _text_of(m["content"])}
            if msgs and msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], list):
                msgs[-1]["content"].append(block)
            else:
                msgs.append({"role": "user", "content": [block]})
        elif role == "assistant" and m.get("tool_calls"):
            blocks = []
            text = _text_of(m.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in m["tool_calls"]:
                fn = tc["function"]
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": fn["name"],
                        "input": tool_arguments(fn.get("arguments")),
                    }
                )
            msgs.append({"role": "assistant", "content": blocks})
        else:
            msgs.append({"role": role, "content": _parts_to_anthropic(m["content"])})

    out: dict = {
        "model": model,
        "messages": msgs,
        "max_tokens": req.get("max_completion_tokens") or req.get("max_tokens") or 4096,
    }
    pc = req.get("_prompt_cache") or {}  # points de cache posés par la gateway (3.3)
    if system:
        if pc.get("system"):
            out["system"] = [{"type": "text", "text": s} for s in system]
            out["system"][-1]["cache_control"] = dict(EPHEMERAL)
        else:
            out["system"] = "\n\n".join(system)
    if pc.get("messages") and msgs:
        last = msgs[-1]
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        if last["content"]:
            last["content"][-1] = {**last["content"][-1], "cache_control": dict(EPHEMERAL)}
    for k in ("temperature", "top_p"):
        if req.get(k) is not None:
            out[k] = req[k]
    stop = req.get("stop")
    if stop:
        out["stop_sequences"] = [stop] if isinstance(stop, str) else stop
    if req.get("tools"):
        out["tools"] = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters", {"type": "object"}),
            }
            for t in req["tools"]
        ]
        if pc.get("tools"):
            out["tools"][-1]["cache_control"] = dict(EPHEMERAL)
    th = req.get("thinking")
    if th and th.get("type") == "enabled":
        # extended thinking : max_tokens doit dépasser le budget, temperature/top_p interdits
        out["thinking"] = th
        out["max_tokens"] = max(out["max_tokens"], th["budget_tokens"] + 4096)
        out.pop("temperature", None)
        out.pop("top_p", None)
    tc = req.get("tool_choice")
    if tc == "required":
        out["tool_choice"] = {"type": "any"}
    elif tc == "auto":
        out["tool_choice"] = {"type": "auto"}
    elif isinstance(tc, dict):
        out["tool_choice"] = {"type": "tool", "name": tc["function"]["name"]}
    return out


# ---------- Anthropic response -> OpenAI response ----------


def _usage(inp: int, out: int, cache_read: int = 0, cache_write: int = 0) -> dict:
    """Usage OpenAI normalisé. Côté Anthropic, input_tokens EXCLUT les tokens de cache :
    prompt_tokens = input + lus + écrits, détail dans prompt_tokens_details."""
    prompt = inp + cache_read + cache_write
    u = {"prompt_tokens": prompt, "completion_tokens": out, "total_tokens": prompt + out}
    if cache_read or cache_write:
        u["prompt_tokens_details"] = {"cached_tokens": cache_read, "cache_creation_tokens": cache_write}
    return u


def _anthropic_usage(u: dict, out: int | None = None) -> dict:
    return _usage(
        u.get("input_tokens", 0),
        u.get("output_tokens", 0) if out is None else out,
        u.get("cache_read_input_tokens", 0) or 0,
        u.get("cache_creation_input_tokens", 0) or 0,
    )


def anthropic_to_openai(resp: dict, model: str) -> dict:
    text, thinking, calls = "", "", []
    for b in resp.get("content", []):
        if b["type"] == "text":
            text += b["text"]
        elif b["type"] == "thinking":
            thinking += b.get("thinking", "")
        elif b["type"] == "tool_use":
            calls.append(
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
                }
            )
    msg: dict = {"role": "assistant", "content": text or None}
    if thinking:
        msg["reasoning_content"] = thinking
    if calls:
        msg["tool_calls"] = calls
    u = resp.get("usage", {})
    return {
        "id": resp.get("id", f"chatcmpl-{uuid.uuid4().hex}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": STOP_TO_FINISH.get(resp.get("stop_reason"), "stop")}],
        "usage": _anthropic_usage(u),
    }


class AnthropicStreamConverter:
    """Convertit les events SSE Anthropic en chunks OpenAI (état par flux)."""

    def __init__(self, model: str):
        self.model = model
        self.id = f"chatcmpl-{uuid.uuid4().hex}"
        self.start_usage: dict = {}
        self.out = 0
        self.tool_idx = -1
        self.finish = "stop"

    def _chunk(self, delta: dict, finish=None, usage=None) -> dict:
        c = {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}] if usage is None else [],
        }
        if usage is not None:
            c["usage"] = usage
        return c

    def feed(self, ev: dict) -> list[dict]:
        t = ev.get("type")
        if t == "message_start":
            self.start_usage = ev["message"].get("usage", {})  # inclut les tokens de cache
            return [self._chunk({"role": "assistant", "content": ""})]
        if t == "content_block_start" and ev["content_block"]["type"] == "tool_use":
            self.tool_idx += 1
            b = ev["content_block"]
            return [
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": self.tool_idx,
                                "id": b["id"],
                                "type": "function",
                                "function": {"name": b["name"], "arguments": ""},
                            }
                        ]
                    }
                )
            ]
        if t == "content_block_delta":
            d = ev["delta"]
            if d["type"] == "text_delta":
                return [self._chunk({"content": d["text"]})]
            if d["type"] == "thinking_delta":
                return [self._chunk({"reasoning_content": d["thinking"]})]
            if d["type"] == "input_json_delta":
                return [
                    self._chunk(
                        {"tool_calls": [{"index": self.tool_idx, "function": {"arguments": d["partial_json"]}}]}
                    )
                ]
        if t == "message_delta":
            self.finish = STOP_TO_FINISH.get(ev["delta"].get("stop_reason"), "stop")
            self.out = ev.get("usage", {}).get("output_tokens", self.out)
        if t == "message_stop":
            return [
                self._chunk({}, finish=self.finish),
                self._chunk({}, usage=_anthropic_usage(self.start_usage, self.out)),
            ]
        return []


# ---------- Anthropic request -> OpenAI request (endpoint /v1/messages) ----------


def anthropic_to_openai_request(req: dict) -> dict:
    msgs = []
    sys = req.get("system")
    if sys:
        msgs.append({"role": "system", "content": sys if isinstance(sys, str) else _text_of(sys)})
    for m in req.get("messages", []):
        c = m["content"]
        if isinstance(c, str):
            msgs.append({"role": m["role"], "content": c})
            continue
        parts, calls = [], []
        for b in c:
            if b["type"] == "text":
                parts.append({"type": "text", "text": b["text"]})
            elif b["type"] == "image":
                s = b["source"]
                url = s["url"] if s["type"] == "url" else f"data:{s['media_type']};base64,{s['data']}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
            elif b["type"] == "tool_use":
                calls.append(
                    {
                        "id": b["id"],
                        "type": "function",
                        "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
                    }
                )
            elif b["type"] == "tool_result":
                msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": b["tool_use_id"],
                        "content": _text_of(b["content"]) if not isinstance(b["content"], str) else b["content"],
                    }
                )
        if parts or calls:
            msg = {"role": m["role"], "content": parts or None}
            if calls:
                msg["tool_calls"] = calls
            msgs.append(msg)
    out: dict = {"model": req.get("model"), "messages": msgs, "max_tokens": req.get("max_tokens")}
    for k in ("temperature", "top_p"):
        if req.get(k) is not None:
            out[k] = req[k]
    if req.get("stop_sequences"):
        out["stop"] = req["stop_sequences"]
    if req.get("tools"):
        out["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object"}),
                },
            }
            for t in req["tools"]
        ]
    tc = req.get("tool_choice")  # 16.2 : auto / any / tool / none
    if isinstance(tc, dict):
        kind = tc.get("type")
        if kind in ("auto", "none"):
            out["tool_choice"] = kind
        elif kind == "any":
            out["tool_choice"] = "required"
        elif kind == "tool" and tc.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        if tc.get("disable_parallel_tool_use") and out.get("tools"):
            out["parallel_tool_calls"] = False
    return out


def openai_to_anthropic_response(resp: dict, model: str) -> dict:
    ch = resp["choices"][0]
    msg = ch["message"]
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        content.append(
            {
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["function"]["name"],
                "input": tool_arguments(tc["function"].get("arguments")),
            }
        )
    u = resp.get("usage", {})
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": FINISH_TO_STOP.get(ch.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": openai_usage_to_anthropic(u),
    }


def openai_usage_to_anthropic(u: dict) -> dict:
    d = u.get("prompt_tokens_details") or {}
    read, write = d.get("cached_tokens", 0) or 0, d.get("cache_creation_tokens", 0) or 0
    out = {
        "input_tokens": max(0, u.get("prompt_tokens", 0) - read - write),
        "output_tokens": u.get("completion_tokens", 0),
    }
    if read or write:
        out["cache_read_input_tokens"] = read
        out["cache_creation_input_tokens"] = write
    return out


def sse(event: str | None, data: dict) -> str:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data)}\n\n"


async def openai_stream_to_anthropic_sse(chunks, model: str):
    """chunks: async iterator de chunks OpenAI -> lignes SSE au format Anthropic."""
    yield sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    open_block = None  # ("text"|"tool", index)
    n_blocks = 0
    finish, usage = "stop", {}
    tool_map: dict[int, int] = {}

    async for c in chunks:
        if c.get("usage"):
            usage = c["usage"]
        for ch in c.get("choices", []):
            d = ch.get("delta", {})
            if d.get("content"):
                if open_block != ("text", n_blocks):
                    if open_block:
                        yield sse("content_block_stop", {"type": "content_block_stop", "index": n_blocks})
                        n_blocks += 1
                    yield sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": n_blocks,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                    open_block = ("text", n_blocks)
                yield sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": n_blocks,
                        "delta": {"type": "text_delta", "text": d["content"]},
                    },
                )
            for tc in d.get("tool_calls") or []:
                i = tc["index"]
                if tc.get("id"):
                    if open_block:
                        yield sse("content_block_stop", {"type": "content_block_stop", "index": n_blocks})
                        n_blocks += 1
                    tool_map[i] = n_blocks
                    open_block = ("tool", n_blocks)
                    yield sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": n_blocks,
                            "content_block": {
                                "type": "tool_use",
                                "id": tc["id"],
                                "name": tc["function"]["name"],
                                "input": {},
                            },
                        },
                    )
                args = tc.get("function", {}).get("arguments")
                if args:
                    yield sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": tool_map[i],
                            "delta": {"type": "input_json_delta", "partial_json": args},
                        },
                    )
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    if open_block:
        yield sse("content_block_stop", {"type": "content_block_stop", "index": n_blocks})
    yield sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": FINISH_TO_STOP.get(finish, "end_turn"), "stop_sequence": None},
            "usage": {"output_tokens": usage.get("completion_tokens", 0)},
        },
    )
    yield sse("message_stop", {"type": "message_stop"})
