"""Préférences utilisateur persistées en base — thème graphique (5.3).

Un thème : {"mode": "auto|light|dark", "preset": "<nom>|custom",
            "custom": {"accent", "background", "bubble", "border": "#rrggbb"}}  (custom optionnel)
Les couleurs acceptent #rgb, #rrggbb ou rgb(r, g, b) et sont normalisées en #rrggbb.
Portée : par clé virtuelle (chaque appareil/appli peut avoir son thème) ou par compte.
"""

from __future__ import annotations

import re

from . import skills, ui_packs

MODES = ("auto", "light", "dark")
UIS = ("classic", "claude")
PRESETS = ("default", "indigo", "carbon", "cyber", "slate", "crimson", "emerald", "midnight", "claude", "custom")
COLOR_FIELDS = ("accent", "background", "bubble", "border")
_HEX = re.compile(r"^#?([0-9a-f]{3}|[0-9a-f]{6})$", re.IGNORECASE)
_RGB = re.compile(r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*[\d.]+\s*)?\)$", re.IGNORECASE)


class PreferenceError(ValueError):
    pass


def normalize_color(value) -> str:
    if not isinstance(value, str):
        raise PreferenceError("Couleur invalide : utiliser #rrggbb, #rgb ou rgb(r, g, b).")
    v = value.strip()
    m = _HEX.match(v)
    if m:
        h = m.group(1).lower()
        return "#" + ("".join(c * 2 for c in h) if len(h) == 3 else h)
    m = _RGB.match(v)
    if m and all(0 <= int(x) <= 255 for x in m.groups()):
        return "#" + "".join(f"{int(x):02x}" for x in m.groups())
    raise PreferenceError(f"Couleur invalide '{value}' : utiliser #rrggbb, #rgb ou rgb(r, g, b).")


CHAT_MODES = ("speed", "economy", "balanced")
CHAT_EFFORTS = ("auto", "low", "medium", "high")
CHAT_ORCHESTRATION = ("auto", "single", "multi")
BADGE_DISPLAY = ("model", "provider", "both")
CHAT_FIELDS = {"mode", "effort", "orchestration", "badge", "model", "tools", "skills"}
_PROVIDER = re.compile(r"^[a-z0-9_.-]{1,40}$")
_MODEL = re.compile(r"^[A-Za-z0-9_.-]{1,40}/[A-Za-z0-9_.:/@+-]{1,160}$")  # fournisseur/modèle


def validate_chat(data) -> dict:
    """Réglages rapides du chat (5.4) : mode, effort, orchestration, modèle imposé et apparence du badge."""
    if not isinstance(data, dict) or set(data) - CHAT_FIELDS:
        raise PreferenceError(f"Réglages chat : champs {', '.join(sorted(CHAT_FIELDS))} uniquement.")
    out = {
        "mode": data.get("mode", "balanced"),
        "effort": data.get("effort", "auto"),
        "orchestration": data.get("orchestration", "auto"),
    }
    for field, allowed in (("mode", CHAT_MODES), ("effort", CHAT_EFFORTS), ("orchestration", CHAT_ORCHESTRATION)):
        if out[field] not in allowed:
            raise PreferenceError(f"'{field}' doit valoir {', '.join(allowed)}.")
    model = data.get("model") or ""
    if not isinstance(model, str) or (model and not _MODEL.match(model)):
        raise PreferenceError("'model' : « fournisseur/modèle », ou vide pour le routage automatique.")
    if model:
        out["model"] = model
    if not isinstance(data.get("tools", False), bool):
        raise PreferenceError("'tools' : true ou false.")
    if data.get("tools"):
        out["tools"] = True
    try:  # compétences actives (sélecteur de modèle, /competence)
        chosen = skills.normalize(data.get("skills"))
    except skills.SkillError as e:
        raise PreferenceError(str(e))
    if chosen:
        out["skills"] = chosen
    badge = data.get("badge") or {}
    if not isinstance(badge, dict) or set(badge) - {"display", "provider_colors", "colors"}:
        raise PreferenceError("'badge' accepte display, provider_colors et colors.")
    display = badge.get("display", "model")
    if display not in BADGE_DISPLAY:
        raise PreferenceError(f"'badge.display' doit valoir {', '.join(BADGE_DISPLAY)}.")
    colors = badge.get("colors") or {}
    if (
        not isinstance(colors, dict)
        or len(colors) > 20
        or not all(isinstance(k, str) and _PROVIDER.match(k) for k in colors)
    ):
        raise PreferenceError("'badge.colors' : {fournisseur: couleur}, 20 entrées max.")
    out["badge"] = {
        "display": display,
        "provider_colors": bool(badge.get("provider_colors", True)),
        "colors": {k: normalize_color(v) for k, v in colors.items()},
    }
    return out


# ---------- profil du chat : préférences personnelles et styles de réponse (comme Claude) ----------
BUILTIN_STYLES = ("normal", "concis", "explicatif", "formel")  # leurs consignes : static/profile.js
MAX_STYLES = 10
_STYLE_ID = re.compile(r"^[a-z0-9-]{1,40}$")
PROFILE_FIELDS = {"name", "about", "style", "styles"}


def _text(data: dict, key: str, limit: int) -> str:
    v = data.get(key) or ""
    if not isinstance(v, str) or len(v) > limit:
        raise PreferenceError(f"'{key}' : texte de {limit} caractères au plus.")
    return v.strip()


def validate_profile(data) -> dict:
    """Comment appeler la personne, ce que l'IA doit savoir d'elle, style de réponse choisi et styles à elle.
    Envoyés à l'IA par la page avec chaque message (message système), jamais ajoutés par la gateway aux appels d'API."""
    if not isinstance(data, dict) or set(data) - PROFILE_FIELDS:
        raise PreferenceError(f"Profil : champs {', '.join(sorted(PROFILE_FIELDS))} uniquement.")
    out = {"name": _text(data, "name", 60), "about": _text(data, "about", 2000)}
    raw = data.get("styles") or []
    if not isinstance(raw, list) or len(raw) > MAX_STYLES:
        raise PreferenceError(f"'styles' : {MAX_STYLES} styles personnalisés au plus.")
    styles = []
    for st in raw:
        if not isinstance(st, dict) or set(st) - {"id", "name", "instructions"}:
            raise PreferenceError("Style : champs id, name et instructions.")
        sid = st.get("id")
        if not isinstance(sid, str) or not _STYLE_ID.match(sid) or sid in BUILTIN_STYLES:
            raise PreferenceError("Style : identifiant de 1 à 40 caractères (a-z, 0-9, -), autre que ceux fournis.")
        name, instructions = _text(st, "name", 40), _text(st, "instructions", 2000)
        if not name or not instructions:
            raise PreferenceError("Style : un nom et des consignes.")
        styles.append({"id": sid, "name": name, "instructions": instructions})
    if len({st["id"] for st in styles}) != len(styles):
        raise PreferenceError("Deux styles ont le même identifiant.")
    names = [st["name"].casefold() for st in styles]
    if len(set(names)) != len(names) or set(names) & set(BUILTIN_STYLES):  # le sélecteur les montrerait en double
        raise PreferenceError("Deux styles portent le même nom (ou celui d'un style fourni) : renommez-en un.")
    style = data.get("style") or "normal"
    if style not in BUILTIN_STYLES and style not in {st["id"] for st in styles}:
        raise PreferenceError("'style' : un style fourni ou un de vos styles.")
    out["style"] = style
    if styles:
        out["styles"] = styles
    return out


VALIDATORS = {"theme": lambda d: validate_theme(d), "chat": validate_chat, "profile": validate_profile}


def validate_theme(data) -> dict:
    if not isinstance(data, dict) or set(data) - {"mode", "preset", "custom", "ui", "pack"}:
        raise PreferenceError("Thème : champs mode, preset, custom, ui et pack uniquement.")
    mode = data.get("mode", "auto")
    if mode not in MODES:
        raise PreferenceError("'mode' doit valoir auto, light ou dark.")
    preset = data.get("preset", "default")
    if preset not in PRESETS:
        raise PreferenceError(f"'preset' inconnu. Valeurs : {', '.join(PRESETS)}.")
    out: dict = {"mode": mode, "preset": preset}
    ui = data.get("ui")  # interface : Claude (par défaut) ou Lezard IA, suivie sur tous les appareils
    if ui is not None:
        if ui not in UIS:
            raise PreferenceError(f"'ui' doit valoir {' ou '.join(UIS)}.")
        out["ui"] = ui  # gardée même « classic » : sans elle, c'est l'interface Claude
    pack = data.get("pack")  # interface personnalisée importée (ui_packs.py), appliquée par-dessus
    if pack is not None:
        if not ui_packs.valid_id(pack):
            raise PreferenceError("'pack' : identifiant d'une interface importée (pk_…).")
        out["pack"] = pack
    custom = data.get("custom") or {}
    if not isinstance(custom, dict) or set(custom) - set(COLOR_FIELDS):
        raise PreferenceError(f"'custom' accepte : {', '.join(COLOR_FIELDS)}.")
    clean = {k: normalize_color(v) for k, v in custom.items() if v not in (None, "")}
    if clean:
        out["custom"] = clean
    return out
