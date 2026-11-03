"""Outils sans risque : date et heure, calculatrice (évaluation sûre, sans eval)."""

from __future__ import annotations

import ast
import math
import operator
from datetime import datetime

from . import Tool, ToolError, ToolResult, register

DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}
_NAMES = {"pi": math.pi, "e": math.e, "tau": math.tau}
_FUNCS = {
    n: getattr(math, n)
    for n in (
        "sqrt",
        "sin",
        "cos",
        "tan",
        "asin",
        "acos",
        "atan",
        "log",
        "log10",
        "log2",
        "exp",
        "floor",
        "ceil",
        "factorial",
        "radians",
        "degrees",
    )
}
_FUNCS.update(abs=abs, round=round, min=min, max=max)
MAX_BITS = 14_000  # entiers d'environ 4 200 chiffres : au-delà, ((9**999)**999)**999 bloquerait le serveur


async def now(args: dict) -> ToolResult:
    d = datetime.now().astimezone()
    return ToolResult(f"{DAYS[d.weekday()]} {d:%d/%m/%Y %H:%M:%S} (UTC{d:%z})")


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id in _NAMES:
        return _NAMES[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:  # 9**9**9 bloquerait le serveur
            raise ToolError("Exposant trop grand (1000 max).")
        if isinstance(left, int) and isinstance(right, int):  # taille du résultat entier, avant de le calculer
            bits = (
                left.bit_length() * right
                if isinstance(node.op, ast.Pow) and right > 0 and abs(left) > 1
                else left.bit_length() + right.bit_length()
                if isinstance(node.op, ast.Mult)
                else 0
            )
            if bits > MAX_BITS:
                raise ToolError("Résultat trop grand (environ 4 000 chiffres max).")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        args = [_eval(a) for a in node.args]
        if node.func.id == "factorial" and args and args[0] > 1000:
            raise ToolError("Factorielle trop grande (1000 max).")
        return _FUNCS[node.func.id](*args)
    raise ToolError("Expression non autorisée : nombres, + - * / // % **, pi, e et fonctions math usuelles.")


async def calculate(args: dict) -> ToolResult:
    expr = str(args.get("expression") or "")
    if not expr or len(expr) > 300:
        raise ToolError("'expression' requise (300 caractères max).")
    try:
        value = _eval(ast.parse(expr.replace("^", "**"), mode="eval"))
        return ToolResult(f"{expr} = {value}")  # conversion en texte comprise : limitée à 4 300 chiffres
    except ToolError:
        raise
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError, RecursionError) as e:
        raise ToolError(f"Calcul impossible : {e}") from None


register(
    Tool(
        "get_datetime",
        "Date, jour de la semaine et heure actuels (heure locale du serveur).",
        {"type": "object", "properties": {}},
        now,
    ),
    Tool(
        "calculator",
        "Calcule une expression mathématique exacte, ex. « (12.5*3)**2 / sqrt(7) ».",
        {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
        calculate,
    ),
)
