"""Définitions d'outils envoyées au modèle, dans un budget de tokens.

Un serveur MCP peut apporter des dizaines d'outils, chacun avec une longue description et un schéma détaillé : 73 outils
pèsent plus de 30 000 tokens, soit bien au-delà de la fenêtre des modèles locaux (8 192 tokens ici). On n'envoie donc
que les outils MCP les plus liés à la demande, dans un budget, avec des descriptions raccourcies. Les outils internes
(calcul, date, recherche web, PC, documents) sont toujours envoyés : ils sont peu nombreux.
"""

from __future__ import annotations

import json
import os
import re

MCP_SPEC_TOKENS = int(os.getenv("MCP_SPEC_TOKENS", "3000") or 3000)  # ≈ 12 000 caractères de définitions
WORD = re.compile(r"[a-zà-ÿ0-9][a-zà-ÿ0-9_-]{2,}")
DROP_KEYS = {"examples", "default", "title", "$schema"}


def _short(node):
    """Schéma raccourci : descriptions de propriétés coupées, exemples et valeurs par défaut retirés."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in DROP_KEYS:
                continue
            out[k] = " ".join(v.split())[:120] if k == "description" and isinstance(v, str) else _short(v)
        return out
    if isinstance(node, list):
        return [_short(x) for x in node]
    return node


def compact_spec(tool) -> dict:
    spec = tool.spec()
    fn = spec["function"]
    fn["description"] = " ".join(fn["description"].split())[:200]
    fn["parameters"] = _short(fn["parameters"])
    return spec


def tokens(specs: list[dict]) -> int:
    return len(json.dumps(specs, ensure_ascii=False)) // 4


def select_specs(tools: list, query: str, budget: int = MCP_SPEC_TOKENS) -> list[dict]:
    """Définitions à envoyer : tous les outils internes, puis les outils MCP les plus liés à la demande (mots communs
    avec leur nom, nom du serveur compris, ou leur description), dans le budget. À égalité, l'ordre des serveurs."""
    fixed = [t.spec() for t in tools if t.level != "mcp"]
    words = set(WORD.findall((query or "").lower()))

    def score(t) -> int:
        text = f"{t.name} {t.description}".lower()
        return sum(1 for w in words if w in text)

    out = fixed
    used = tokens(out)
    for t in sorted((t for t in tools if t.level == "mcp"), key=lambda t: -score(t)):
        spec = compact_spec(t)
        cost = tokens([spec])
        if used + cost <= budget:
            out.append(spec)
            used += cost
    return out
