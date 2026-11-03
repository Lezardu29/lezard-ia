"""Classifieur de requêtes (tâche 2.1).

Heuristique locale, sans appel LLM (gratuit, < 1 ms) : estime la taille du prompt,
la complexité, le type de tâche et les capacités requises (code, JSON, vision, tools).
Le résultat alimente le moteur de routage (2.2 / 2.3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field

IMAGE_TOKENS = 850  # coût approximatif d'une image
CHARS_PER_TOKEN = 4

CODE_FENCE = re.compile(r"```")
CODE_SYNTAX = re.compile(
    r"(\bdef \w+\(|\bfunction\b|\bclass \w+|\bimport \w+|#include|\bpublic static\b|=>|"
    r"\bconst \w+ =|\blet \w+ =|\bSELECT\b.+\bFROM\b|</?\w+>|\w+\(.*\);|Traceback|Exception:)",
    re.IGNORECASE,
)
CODE_INTENT = re.compile(
    r"\b(code|coder|script|fonction|function|bug|debug|débugg?er|refactor\w*|compile\w*|"
    r"python|javascript|typescript|java|rust|golang|sql|regex|api|endpoint|stack ?trace|"
    r"html|css|php|kotlin|swift|bash|powershell|reactjs|react\.js|react native|vue\.?js|angular|node\.?js|"
    r"next\.?js|django|flask|fastapi|dockerfile|docker|kubernetes|frontend|front-end|backend|back-end|"
    r"implémente\w*|implement\w*|programme\w*|algorithme?|algorithm)\b"
    r"|(?<!\w)(c\+\+|c#)(?!\w)",
    re.IGNORECASE,
)
# demande de créer un logiciel, sans nom de langage : « crée-moi un jeu », « fais une application »
CODE_BUILD = re.compile(
    r"\b(cré\w*|développ\w*|fai[st]|fabrique\w*|génère\w*|générer|écri[st]\w*|construi\w*|réalise\w*|"
    r"build\w*|make|create|develop\w*|write)\b[^.?!\n]{0,40}?"
    r"\b(site( web| internet)?|page web|web ?app|application|appli|app|jeu(?! de (mots|rôles?|société))|"
    r"game|bot|extension|plugin|widget|macro|interface graphique|logiciel|software|landing page|calculatrice|"
    r"calculator)\b",
    re.IGNORECASE,
)
JSON_INTENT = re.compile(r"\b(json|yaml|xml|csv|schéma|schema)\b", re.IGNORECASE)
REASONING = re.compile(
    r"\b(pourquoi|why|démontre\w*|prouve\w*|prove|proof|analyse\w*|analy[sz]e\w*|compare\w*|"
    r"raisonne\w*|reason\w*|étape par étape|step[- ]by[- ]step|optimi[sz]\w*|architectures?|"
    r"stratégie|strategy|trade-?offs?|évalue\w*|evaluate|justifie\w*|explain why|explique pourquoi)\b",
    re.IGNORECASE,
)
MATH = re.compile(
    r"(\b(calcule\w*|calculate|équation|equation|intégrale|integral|dérivée|derivative|"
    r"probabilité|probability|théorème|theorem|matrice|matrix)\b|[=<>]\s*\d|\d\s*[\^*/]\s*\d|∑|∫|√)",
    re.IGNORECASE,
)
TASKS = {
    "translation": re.compile(r"\b(tradui\w*|translate|translation|en anglais|in french|in english)\b", re.I),
    "summarization": re.compile(r"\b(résume\w*|resume|summari[sz]e|summary|synthèse|tl;?dr)\b", re.I),
    "extraction": re.compile(r"\b(extrai\w*|extract\w*|liste les|list all|parse\w*)\b", re.I),
    "creative": re.compile(
        r"\b(écri[st]\w* (une?|des) (histoire|poème|chanson|article)|"
        r"write (a|an) (story|poem|song|article)|imagine\w*|créati\w*|creative)\b",
        re.I,
    ),
}
# demande à produire en plusieurs parties (« prépare un plan : marché, budget, communication, planning ») : un verbe
# de production suivi d'éléments séparés par des virgules. Sans ce verbe (« donne le prix, la taille et le poids »),
# une énumération ne rend pas la demande plus complexe.
DELIVER = re.compile(
    r"\b(prépar(?:e|ez|er)|rédig(?:e|ez|er)|élabor(?:e|ez|er)|conçois|concevez|concevoir|construi(?:s|sez|re)|"
    r"établi(?:s|ssez|r)|développ(?:e|ez|er)|planifi(?:e|ez|er)|organis(?:e|ez|er)|cré(?:e|ez|er)|fais un plan|"
    r"prepare|draft|design|write|build|create|plan)\b",
    re.IGNORECASE,
)
LIST_ITEM = re.compile(r"[,;]\s*\S")
MULTI_STEP = re.compile(
    r"(^\s*(\d+[.)]|[-*•])\s+|\b(puis|ensuite|enfin|then|finally|and also)\b)", re.IGNORECASE | re.MULTILINE
)


@dataclass
class Classification:
    prompt_tokens: int  # estimation de la taille de l'entrée
    max_output_tokens: int | None
    min_context: int  # fenêtre requise (entrée + sortie)
    turns: int
    task: str  # code | math | reasoning | translation | summarization | ...
    complexity: str  # low | medium | high
    complexity_score: float  # 0..1
    needs_code: bool
    needs_json: bool
    needs_vision: bool
    needs_tools: bool
    signals: list[str] = field(default_factory=list)  # justification lisible

    def to_dict(self) -> dict:
        return asdict(self)


def _text_and_images(content) -> tuple[str, int]:
    if content is None:
        return "", 0
    if isinstance(content, str):
        return content, 0
    text, images = [], 0
    for p in content:
        t = p.get("type")
        if t == "text":
            text.append(p.get("text", ""))
        elif t in ("image_url", "image", "input_image"):
            images += 1
    return "\n".join(text), images


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN) if text else 0


def classify(payload: dict) -> Classification:
    messages = payload.get("messages", [])
    total_tokens, images, last_user = 0, 0, ""
    for m in messages:
        text, imgs = _text_and_images(m.get("content"))
        total_tokens += estimate_tokens(text) + 4  # surcoût par message
        images += imgs
        if m.get("tool_calls"):
            total_tokens += estimate_tokens(str(m["tool_calls"]))
        if m.get("role") == "user":
            last_user = text
    total_tokens += images * IMAGE_TOKENS
    if payload.get("tools"):
        total_tokens += estimate_tokens(str(payload["tools"]))

    signals: list[str] = []
    rf = (payload.get("response_format") or {}).get("type")
    needs_json = rf in ("json_object", "json_schema") or bool(JSON_INTENT.search(last_user))
    has_code_syntax = bool(CODE_FENCE.search(last_user) or CODE_SYNTAX.search(last_user))
    needs_code = has_code_syntax or bool(CODE_INTENT.search(last_user) or CODE_BUILD.search(last_user))
    needs_vision = images > 0
    needs_tools = bool(payload.get("tools"))
    reasoning_hits = len(REASONING.findall(last_user))
    math = bool(MATH.search(last_user))
    steps = len(MULTI_STEP.findall(last_user))
    questions = last_user.count("?")

    # --- score de complexité (0..1) ---
    score = 0.0
    user_tokens = estimate_tokens(last_user)
    if user_tokens > 1500:
        score += 0.25
        signals.append("prompt long")
    elif user_tokens > 300:
        score += 0.12
        signals.append("prompt moyen")
    if total_tokens > 8000:
        score += 0.1
        signals.append("contexte lourd")
    if needs_code:
        score += 0.2 if has_code_syntax else 0.15
        signals.append("code")
    if math:
        score += 0.3
        signals.append("maths")
    if reasoning_hits:
        score += min(0.4, 0.12 * reasoning_hits)
        signals.append("raisonnement")
    if steps >= 3:
        score += 0.15
        signals.append("multi-étapes")
    parts = len(LIST_ITEM.findall(last_user)) + 1 if DELIVER.search(last_user) else 0
    if parts >= 3:  # plusieurs livrables : chacun peut être confié à un expert
        score += 0.5 if parts >= 4 else 0.3
        signals.append("plusieurs parties")
    if questions >= 3:
        score += 0.1
        signals.append("questions multiples")
    if needs_json:
        score += 0.05
        signals.append("sortie structurée")
    if needs_tools:
        score += 0.1
        signals.append("tools")
    if needs_vision:
        score += 0.05
        signals.append("vision")
    if user_tokens < 15 and not (needs_code or math or reasoning_hits):
        score -= 0.1
        signals.append("message court")
    score = round(min(1.0, max(0.0, score)), 2)
    complexity = "low" if score < 0.3 else "medium" if score < 0.6 else "high"

    # --- type de tâche (priorité décroissante) ---
    if needs_code:
        task = "code"
    elif math:
        task = "math"
    else:
        task = next((name for name, rx in TASKS.items() if rx.search(last_user)), None)
        if task is None:
            task = "reasoning" if reasoning_hits else "extraction" if needs_json else "chat"

    max_out = payload.get("max_completion_tokens") or payload.get("max_tokens")
    return Classification(
        prompt_tokens=total_tokens,
        max_output_tokens=max_out,
        min_context=total_tokens + (max_out or 1024),
        turns=sum(1 for m in messages if m.get("role") == "user"),
        task=task,
        complexity=complexity,
        complexity_score=score,
        needs_code=needs_code,
        needs_json=needs_json,
        needs_vision=needs_vision,
        needs_tools=needs_tools,
        signals=signals,
    )
