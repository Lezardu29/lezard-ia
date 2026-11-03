"""Compétences : consignes que l'IA suit pendant une conversation (sélecteur de modèle, commande /competence).

- Une compétence = un fichier skill_packs/<id>.md : en-tête (nom, description, groupe, source, licence) puis le texte
  envoyé à l'IA comme message système. Activées par la requête : champ `skills` (liste d'identifiants) ou en-tête
  `x-gateway-skills` (identifiants séparés par des virgules).
- Adaptations françaises condensées de projets open source (Superpowers, Caveman, Humanizer, UI UX Pro Max) : licences dans
  skill_packs/licenses, modifications décrites dans skill_packs/NOTICE.md. Condensées : chaque compétence active coûte
  ses tokens à chaque message, d'où aussi MAX_ACTIVE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .classifier import estimate_tokens

PACKS_DIR = Path(__file__).parent / "skill_packs"
MAX_ACTIVE = 4
CATALOG_URL = "https://github.com/ComposioHQ/awesome-claude-skills"  # d'autres idées (liste sans licence : lien seul)
_ID = re.compile(r"^[a-z0-9-]{1,40}$")
_FRONT = re.compile(r"^---\n(.*?)\n---\n+(.*)$", re.S)
FIELDS = ("id", "name", "description", "group", "source", "url", "license", "license_file")
# ordre d'affichage : le style des réponses, puis la méthode de développement dans l'ordre du travail, puis le design
ORDER = ("concis", "naturel", "cadrer", "plan", "tdd", "debogage", "verifier", "interface")


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    group: str
    source: str
    url: str
    license: str
    license_file: str
    content: str

    def to_dict(self) -> dict:
        return {f: getattr(self, f) for f in FIELDS} | {"tokens": estimate_tokens(self.content)}


def parse(text: str) -> Skill:
    m = _FRONT.match(text.replace("\r\n", "\n"))
    if not m:
        raise SkillError("En-tête « --- » manquant.")
    meta = dict(line.split(": ", 1) for line in m.group(1).splitlines() if ": " in line)
    missing = [f for f in FIELDS if not meta.get(f)]
    if missing or not _ID.match(meta.get("id", "")):
        raise SkillError(f"Champs manquants ou invalides : {', '.join(missing) or 'id'}.")
    return Skill(**{f: meta[f].strip() for f in FIELDS}, content=m.group(2).strip())


@lru_cache(maxsize=1)
def load() -> dict[str, Skill]:
    skills = [parse(p.read_text(encoding="utf-8")) for p in sorted(PACKS_DIR.glob("*.md")) if p.name != "NOTICE.md"]
    rank = lambda s: ORDER.index(s.id) if s.id in ORDER else len(ORDER)
    return {s.id: s for s in sorted(skills, key=rank)}


def catalog() -> list[dict]:
    return [s.to_dict() for s in load().values()]


def normalize(value) -> list[str]:
    """Identifiants demandés (liste, ou texte séparé par des virgules), dédoublonnés et vérifiés."""
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SkillError("'skills' : une liste d'identifiants de compétences.")
    ids = list(dict.fromkeys(value))
    unknown = [v for v in ids if v not in load()]
    if unknown:
        raise SkillError(f"Compétence inconnue : {', '.join(unknown[:3])} (voir /v1/skills).")
    if len(ids) > MAX_ACTIVE:
        raise SkillError(f"{MAX_ACTIVE} compétences actives au plus.")
    return ids


def system_message(ids: list[str]) -> dict:
    skills = load()
    parts = "\n\n".join(skills[i].content for i in ids)
    return {
        "role": "system",
        "content": "Compétences actives : applique les consignes suivantes pendant toute la conversation, sans les "
        "citer ni les annoncer.\n\n" + parts,
    }
