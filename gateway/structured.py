"""Sorties structurées (16.3) : response_format json_object / json_schema, quel que soit le fournisseur.

- Natif : les connecteurs compatibles OpenAI transmettent response_format tel quel — OpenAI, Mistral, Groq, Gemini,
  OpenRouter, vLLM (structured outputs), Ollama (l'API compatible le traduit en « format » : JSON ou schéma),
  LM Studio (json_schema). Un serveur qui refuse le paramètre (400 qui le cite : ancien Ollama, LM Studio avec
  json_object…) est relancé aussitôt en mode consigne.
- Consigne : Anthropic (et les refus ci-dessus) reçoivent l'exigence dans le prompt système, schéma compris.
- Dans tous les cas la réponse est VÉRIFIÉE : JSON extrait (bloc ```json, texte autour retiré), contrôlé contre
  le schéma (sous-ensemble de JSON Schema : type, properties, required, additionalProperties, items, enum, const,
  anyOf / oneOf, bornes), puis remis au propre dans `content`. Invalide : une nouvelle tentative au plus, avec
  l'erreur ; encore invalide : réponse rendue telle quelle, signalée (x_gateway.structured.valid = false).
- Une réponse qui appelle des outils n'est pas contrôlée (le modèle a choisi d'appeler une fonction).
"""

from __future__ import annotations

import json
import re

from .providers.base import Provider, ProviderError
from .validation import RequestError

TYPES = ("json_object", "json_schema")
_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)
MAX_SCHEMA_CHARS = 20_000
MAX_ERRORS = 5


def requested(body: dict) -> dict | None:
    """response_format JSON de la requête (None : texte libre)."""
    rf = body.get("response_format")
    return rf if isinstance(rf, dict) and rf.get("type") in TYPES else None


def check_format(rf) -> None:
    """Validation de la requête : type connu, schéma présent et raisonnable pour json_schema."""
    if rf is None:
        return
    t = rf.get("type")
    if t not in ("text", *TYPES):
        raise RequestError("'response_format.type' : text, json_object ou json_schema.")
    if t == "json_schema":
        js = rf.get("json_schema")
        if not isinstance(js, dict) or not isinstance(js.get("schema"), dict):
            raise RequestError('\'response_format.json_schema\' : {"name": …, "schema": {…}} attendu.')
        if js.get("name") is not None and not (
            isinstance(js["name"], str) and re.fullmatch(r"[\w-]{1,64}", js["name"])
        ):
            raise RequestError("'response_format.json_schema.name' : 64 caractères au plus (lettres, chiffres, _ -).")
        if len(json.dumps(js["schema"])) > MAX_SCHEMA_CHARS:
            raise RequestError(f"Schéma JSON trop volumineux ({MAX_SCHEMA_CHARS} caractères au plus).")


def schema_of(rf: dict) -> dict | None:
    return (rf.get("json_schema") or {}).get("schema") if rf.get("type") == "json_schema" else None


def native(provider: Provider) -> bool:
    return provider.cfg.kind != "anthropic"


# ---------- consigne ----------


def instruction(rf: dict) -> str:
    text = (
        "Réponds UNIQUEMENT avec un document JSON valide : pas de texte avant ou après, pas de bloc de code "
        "Markdown, pas de commentaire."
    )
    schema = schema_of(rf)
    if schema is not None:
        text += " Il doit respecter exactement ce schéma JSON :\n" + json.dumps(schema, ensure_ascii=False)
    else:
        text += " La racine est un objet JSON ({…})."
    return text


def with_instruction(payload: dict, rf: dict) -> dict:
    """Requête sans response_format, l'exigence étant ajoutée au message système."""
    out = {k: v for k, v in payload.items() if k != "response_format"}
    msgs = list(out.get("messages") or [])
    note = instruction(rf)
    if msgs and msgs[0].get("role") in ("system", "developer") and isinstance(msgs[0].get("content"), str):
        msgs[0] = {**msgs[0], "content": f"{msgs[0]['content']}\n\n{note}"}
    else:
        msgs.insert(0, {"role": "system", "content": note})
    out["messages"] = msgs
    return out


def refuses_format(e: ProviderError) -> bool:
    """Le serveur refuse le paramètre lui-même (et non la requête) : on passe en mode consigne."""
    if e.status not in (400, 422, 501):
        return False
    text = e.message.lower()
    return any(
        k in text
        for k in ("response_format", "json_schema", "json_object", "structured output", "response format", "guided")
    )


# ---------- extraction et contrôle ----------


def extract(text: str):
    """Document JSON contenu dans la réponse : texte brut, bloc ```json, ou premier objet / tableau du texte."""
    s = (text or "").strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    for block in _FENCE.findall(s):
        try:
            return json.loads(block.strip())
        except ValueError:
            continue
    dec = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch in "{[":
            try:
                return dec.raw_decode(s[i:])[0]
            except ValueError:
                continue
    raise ValueError("aucun JSON lisible dans la réponse")


_PY = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is(value, t: str) -> bool:
    if t == "integer":
        return (
            isinstance(value, int) and not isinstance(value, bool) or (isinstance(value, float) and value.is_integer())
        )
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _PY.get(t, object))


def errors(value, schema: dict, path: str = "$", root: dict | None = None, out: list | None = None) -> list[str]:
    """Écarts entre `value` et `schema` (sous-ensemble courant de JSON Schema ; mots-clés inconnus ignorés)."""
    out = [] if out is None else out
    root = root or schema
    if len(out) >= MAX_ERRORS or not isinstance(schema, dict):
        return out
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        target = root
        for part in ref[2:].split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        return errors(value, target, path, root, out)
    for key in ("anyOf", "oneOf"):
        if isinstance(schema.get(key), list):
            if not any(not errors(value, s, path, root, []) for s in schema[key]):
                out.append(f"{path} : ne correspond à aucune des formes permises")
            return out
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    if types and not any(_is(value, x) for x in types):
        out.append(f"{path} : {' ou '.join(types)} attendu")
        return out
    if "const" in schema and value != schema["const"]:
        out.append(f"{path} : {json.dumps(schema['const'], ensure_ascii=False)} attendu")
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        out.append(f"{path} : valeur parmi {json.dumps(schema['enum'], ensure_ascii=False)[:200]}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                out.append(f"{path} : champ « {name} » manquant")
        extra = schema.get("additionalProperties")
        for k, v in value.items():
            if k in props:
                errors(v, props[k], f"{path}.{k}", root, out)
            elif extra is False:
                out.append(f"{path} : champ « {k} » non prévu par le schéma")
            elif isinstance(extra, dict):
                errors(v, extra, f"{path}.{k}", root, out)
    if isinstance(value, list):
        if isinstance(schema.get("items"), dict):
            for i, v in enumerate(value):
                errors(v, schema["items"], f"{path}[{i}]", root, out)
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            out.append(f"{path} : au moins {schema['minItems']} élément(s)")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            out.append(f"{path} : au plus {schema['maxItems']} élément(s)")
    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            out.append(f"{path} : au moins {schema['minLength']} caractère(s)")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            out.append(f"{path} : au plus {schema['maxLength']} caractère(s)")
    if _is(value, "number"):
        for key, ok in (
            ("minimum", lambda b: value >= b),
            ("maximum", lambda b: value <= b),
            ("exclusiveMinimum", lambda b: value > b),
            ("exclusiveMaximum", lambda b: value < b),
        ):
            b = schema.get(key)
            if isinstance(b, (int, float)) and not isinstance(b, bool) and not ok(b):
                out.append(f"{path} : {key} {b}")
    return out[:MAX_ERRORS]


def verify(resp: dict, rf: dict) -> str | None:
    """Contrôle la réponse et remet le JSON au propre dans `content` ; renvoie l'erreur (None si valide)."""
    msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
    if msg.get("tool_calls"):
        return None
    try:
        doc = extract(msg.get("content") or "")
    except ValueError as e:
        return str(e)
    schema = schema_of(rf)
    if schema is None and not isinstance(doc, dict):
        return "objet JSON attendu à la racine ({…})"
    if schema is not None:
        found = errors(doc, schema)
        if found:
            return " ; ".join(found)
    msg["content"] = json.dumps(doc, ensure_ascii=False)
    return None


def stream_report(text: str, rf: dict) -> dict:
    """Flux natif : le JSON est contrôlé une fois reçu en entier ; sans nouvelle tentative possible (les tokens sont
    déjà chez le client), le rapport dit seulement s'il est valide."""
    problem = verify({"choices": [{"message": {"content": text}}]}, rf)
    report = {"mode": "native", "valid": problem is None, "retried": False, "streamed": True}
    if problem:
        report["error"] = problem[:300]
    return report


def _add_usage(a: dict | None, b: dict | None) -> dict:
    a, b = a or {}, b or {}
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    return {**b, **{k: (a.get(k) or 0) + (b.get(k) or 0) for k in keys}}


async def chat(provider: Provider, payload: dict, rf: dict) -> tuple[dict, dict]:
    """Appel avec sortie structurée : (réponse, rapport {mode, valid, retried, error?})."""
    mode = "native" if native(provider) else "prompt"
    send = payload if mode == "native" else with_instruction(payload, rf)
    try:
        resp = await provider.chat(send)
    except ProviderError as e:
        if mode != "native" or not refuses_format(e):
            raise
        mode, send = "prompt", with_instruction(payload, rf)
        resp = await provider.chat(send)
    report = {"mode": mode, "valid": True, "retried": False}
    problem = verify(resp, rf)
    if problem:
        content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        again = {
            **send,
            "messages": [
                *send["messages"],
                {"role": "assistant", "content": content[:8000]},
                {
                    "role": "user",
                    "content": f"Ta réponse n'est pas un JSON valide ({problem[:500]}). "
                    "Renvoie uniquement le document JSON corrigé, sans aucun texte autour.",
                },
            ],
        }
        first_usage = resp.get("usage")
        try:
            resp = await provider.chat(again)
        except ProviderError:
            return resp, {**report, "valid": False, "error": problem[:300]}
        resp["usage"] = _add_usage(first_usage, resp.get("usage"))  # les deux appels sont facturés
        report["retried"] = True
        problem = verify(resp, rf)
        if problem:
            report.update(valid=False, error=problem[:300])
    return resp, report
