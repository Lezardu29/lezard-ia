"""Mode économie (18.7) : chat envoyé à l'API NATIVE d'Ollama (/api/chat), seule à régler le chargement du modèle.

L'API compatible OpenAI d'Ollama ignore les options de chargement : impossible d'y limiter la part du modèle sur la
carte graphique (num_gpu), les cœurs du processeur (num_thread) ou la mémoire du contexte (num_ctx). Ici, la requête
OpenAI est traduite vers /api/chat avec ces options, et la réponse (complète ou en flux NDJSON) retraduite au format
OpenAI : le client ne voit aucune différence.

Une requête qui utilise une fonction sans équivalent natif (plusieurs réponses, logprobs, image par URL, outil
imposé…) n'est pas traduite : `to_native` rend None et elle part par l'API compatible, sans bridage, plutôt qu'à
moitié comprise.
"""

from __future__ import annotations

import json
import time
import uuid

# champs OpenAI sans effet ici, ignorés sans risque
IGNORED = {"stream_options", "user", "store", "metadata", "parallel_tool_calls", "service_tier", "prompt_cache_key"}
OPTIONS = {  # champ OpenAI -> option native
    "max_tokens": "num_predict",
    "max_completion_tokens": "num_predict",
    "temperature": "temperature",
    "top_p": "top_p",
    "seed": "seed",
    "frequency_penalty": "frequency_penalty",
    "presence_penalty": "presence_penalty",
}
HANDLED = {"model", "messages", "stream", "tools", "tool_choice", "response_format", "reasoning_effort", "stop", "n"}
HANDLED |= set(OPTIONS) | IGNORED | {"logprobs", "top_logprobs", "logit_bias"}


def _arguments(raw):
    """Arguments d'un appel d'outil : objet attendu par Ollama (texte JSON côté OpenAI)."""
    if isinstance(raw, str):
        try:
            value = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    return raw if isinstance(raw, dict) else None


def _message(m: dict, names: dict[str, str]) -> dict | None:
    if not isinstance(m, dict) or not isinstance(m.get("role"), str):
        return None
    role = "system" if m["role"] == "developer" else m["role"]
    out: dict = {"role": role}
    content = m.get("content")
    if isinstance(content, list):  # parties : texte et images (data: seulement, Ollama ne télécharge rien)
        texts, images = [], []
        for part in content:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif kind == "image_url":
                url = part.get("image_url")
                url = url.get("url") if isinstance(url, dict) else url
                if not isinstance(url, str) or not url.startswith("data:") or ";base64," not in url:
                    return None
                images.append(url.split(";base64,", 1)[1])
            else:
                return None
        out["content"] = "\n".join(texts)
        if images:
            out["images"] = images
    elif content is None or isinstance(content, str):
        out["content"] = content or ""
    else:
        return None
    calls = m.get("tool_calls")
    if calls:
        native = []
        for tc in calls:
            fn = tc.get("function") if isinstance(tc, dict) else None
            args = _arguments(fn.get("arguments")) if isinstance(fn, dict) else None
            if args is None or not isinstance(fn.get("name"), str):
                return None
            native.append({"function": {"name": fn["name"], "arguments": args}})
            if tc.get("id"):
                names[tc["id"]] = fn["name"]
        out["tool_calls"] = native
    if role == "tool" and names.get(m.get("tool_call_id")):
        out["tool_name"] = names[m["tool_call_id"]]
    return out


def to_native(payload: dict, options: dict, keep_alive: int | None) -> dict | None:
    """Corps de /api/chat équivalent à `payload` (format OpenAI), avec les `options` de chargement ; None si une
    partie de la requête n'a pas d'équivalent natif."""
    if set(payload) - HANDLED:
        return None
    if payload.get("n", 1) != 1 or payload.get("logprobs") or payload.get("top_logprobs") or payload.get("logit_bias"):
        return None
    names: dict[str, str] = {}
    messages = [_message(m, names) for m in payload.get("messages") or []]
    if not messages or any(m is None for m in messages):
        return None
    body: dict = {"model": payload["model"], "messages": messages, "stream": bool(payload.get("stream"))}
    tools, choice = payload.get("tools"), payload.get("tool_choice")
    if tools and choice not in (None, "auto", "none"):
        return None  # outil imposé : pas d'équivalent natif
    if tools and choice != "none":
        body["tools"] = tools
    rf = payload.get("response_format")
    if isinstance(rf, dict) and rf.get("type") == "json_object":
        body["format"] = "json"
    elif isinstance(rf, dict) and rf.get("type") == "json_schema":
        schema = (rf.get("json_schema") or {}).get("schema")
        if not isinstance(schema, dict):
            return None
        body["format"] = schema
    elif rf not in (None, {"type": "text"}):
        return None
    effort = payload.get("reasoning_effort")
    if effort is not None:
        body["think"] = effort not in ("none", "minimal")
    opts = {native: payload[k] for k, native in OPTIONS.items() if payload.get(k) is not None}
    stop = payload.get("stop")
    if stop:
        opts["stop"] = [stop] if isinstance(stop, str) else list(stop)
    body["options"] = {**opts, **options}
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    return body


def _calls(msg: dict) -> list[dict]:
    return [
        {
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": (tc.get("function") or {}).get("name", ""),
                "arguments": json.dumps((tc.get("function") or {}).get("arguments") or {}, ensure_ascii=False),
            },
        }
        for tc in msg.get("tool_calls") or []
        if isinstance(tc, dict)
    ]


def _finish(data: dict, calls: bool) -> str:
    if calls:
        return "tool_calls"
    return "length" if data.get("done_reason") == "length" else "stop"


def _usage(data: dict) -> dict:
    prompt, completion = int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def from_native(data: dict, model: str) -> dict:
    """Réponse complète de /api/chat au format OpenAI (chat.completion)."""
    msg = data.get("message") or {}
    calls = _calls(msg)
    message: dict = {"role": "assistant", "content": msg.get("content") or (None if calls else "")}
    if calls:
        message["tool_calls"] = calls
    if msg.get("thinking"):
        message["reasoning"] = msg["thinking"]  # comme l'API compatible d'Ollama
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": _finish(data, bool(calls))}],
        "usage": _usage(data),
    }


class StreamTranslator:
    """Lignes NDJSON de /api/chat en fragments OpenAI (chat.completion.chunk), usage à la fin."""

    def __init__(self, model: str):
        self.model = model
        self.id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self.created = int(time.time())
        self.started = False
        self.calls = 0

    def _chunk(self, delta: dict, finish: str | None = None) -> dict:
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def feed(self, data: dict) -> list[dict]:
        msg = data.get("message") or {}
        out, delta = [], {}
        if not self.started:
            delta["role"] = "assistant"
            self.started = True
        if msg.get("content"):
            delta["content"] = msg["content"]
        if msg.get("thinking"):
            delta["reasoning"] = msg["thinking"]
        calls = _calls(msg)
        if calls:
            delta["tool_calls"] = [{**c, "index": self.calls + i} for i, c in enumerate(calls)]
            self.calls += len(calls)
        if delta.keys() - {"role"} or (delta and not data.get("done")):
            out.append(self._chunk(delta))
        if data.get("done"):
            out.append(self._chunk({}, _finish(data, self.calls > 0)))
            out.append({**self._chunk({}), "choices": [], "usage": _usage(data)})
        return out
