"""Conversations enregistrées sur la gateway (tâche 19.1).

Une conversation appartient à un compte ET à une portée : « account » pour le compte lui-même, « key:<id> » pour
chaque clé virtuelle (comme les préférences 5.3). Une clé ne voit jamais les conversations d'une autre, ni celles du
compte : 404, comme une conversation qui n'existe pas. Visiteurs anonymes : pas d'enregistrement
(la conversation reste dans le navigateur, comme avant).

Titre et messages sont chiffrés au repos (AES-GCM, 4.4), avec pour données associées le propriétaire, la portée et
l'identifiant : une ligne recopiée ailleurs ne se déchiffre pas. Ils sont donc aussi illisibles dans les sauvegardes
(18.2) sans la clé de chiffrement.

Deux appareils : la liste des messages est remplacée en entier à chaque enregistrement, avec la révision lue ;
si un autre appareil a écrit entre-temps, 409 et le client recharge (pas d'écrasement silencieux).

Variantes (19.3) : un message modifié, ou une réponse régénérée, garde ses versions — « versions » (liste) et
« active » (index). Chaque version inactive conserve la suite de la conversation qui la suivait (« tail ») : on
navigue entre elles (‹ 1/2 ›) sans rien perdre. La version active est la suite visible de la liste.

Recherche plein texte (19.3) : titres ET messages, déchiffrés en mémoire seulement — aucun index en clair sur le
disque (le chiffrement au repos l'interdit). Textes déchiffrés gardés en cache par révision, borné.

Titre : généré par un modèle LOCAL après le premier échange (gratuit, rien ne quitte le réseau), sinon le début du
premier message. Conservation : GATEWAY_CONVERSATION_DAYS (0 ou vide = illimitée), sans activité depuis N jours.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from .classifier import classify
from .crypto import Cipher, CryptoError
from .providers.base import ProviderError, strip_reasoning
from .router import route
from .validation import RequestError, check_image_url

if TYPE_CHECKING:
    from .core import Gateway
    from .plans import PlanContext
    from .store import DocumentStore

MAX_CONVERSATIONS = 1000  # par compte ou par clé virtuelle
MAX_MESSAGES = 500
MAX_CHARS = 8_000_000  # messages sérialisés (≈ 8 Mo, images comprises) : sous la limite des corps (9.1)
MAX_ATTACHMENTS = 20  # documents joints à un message (19.2)
MAX_VERSIONS = 20  # variantes d'un message (19.3)
MAX_DEPTH = 8  # variantes dans des variantes
MAX_NODES = 5000  # messages au total, variantes comprises
SEARCH_CACHE = 300  # conversations dont le texte déchiffré reste en mémoire
SNIPPET = 70  # caractères de part et d'autre du terme trouvé
PART_TYPES = ("text", "image_url")
MAX_TITLE = 120
FALLBACK_TITLE_CHARS = 60
ROLES = ("user", "assistant", "system")
UNREADABLE = "(illisible : clé de chiffrement différente)"
TITLE_PROMPT = (
    "Donne un titre très court (3 à 6 mots) à cette conversation, dans sa langue. Réponds uniquement "
    "par le titre, sans guillemets ni ponctuation finale."
)


class ConversationError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def storable(ctx: PlanContext) -> bool:
    return not ctx.identity.startswith("ip:")


def scope_of(ctx: PlanContext) -> tuple[str, str]:
    if not storable(ctx):
        raise ConversationError(
            403,
            "Conversations enregistrées : connectez-vous avec une clé. "
            "En attendant, la conversation reste dans ce navigateur.",
        )
    return ctx.identity, f"key:{ctx.key['id']}" if ctx.key else "account"


def retention_days() -> float:
    try:
        return max(0.0, float(os.getenv("GATEWAY_CONVERSATION_DAYS", "0") or 0))
    except ValueError:
        return 0.0


def text_of(content) -> str:
    """Texte d'un message : la chaîne elle-même, ou ses parties « text » (message avec images, 19.2)."""
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text")


def _content(c, i: int):
    """Texte, ou liste de parties text / image_url (images jointes, vérifiées comme pour le chat)."""
    if isinstance(c, str):
        return c
    if not isinstance(c, list) or not c:
        raise ConversationError(400, f"Message {i} invalide : « content » texte ou liste de parties requis.")
    out = []
    for p in c:
        if not isinstance(p, dict) or p.get("type") not in PART_TYPES:
            raise ConversationError(400, f"Message {i} : parties « text » ou « image_url » seulement.")
        if p["type"] == "text":
            if not isinstance(p.get("text"), str):
                raise ConversationError(400, f"Message {i} : partie « text » sans texte.")
            out.append({"type": "text", "text": p["text"]})
            continue
        u = p.get("image_url")
        url = u.get("url") if isinstance(u, dict) else u
        if not isinstance(url, str):
            raise ConversationError(400, f"Message {i} : « image_url » sans adresse.")
        try:
            check_image_url(url, f"Message {i}")
        except RequestError as e:
            raise ConversationError(e.status, e.message) from e
        out.append({"type": "image_url", "image_url": {"url": url}})
    return out


def _attachments(raw, i: int) -> list[dict]:
    """Documents joints (19.2) : seulement leur fiche, le contenu reste dans /v1/documents."""
    if not isinstance(raw, list) or len(raw) > MAX_ATTACHMENTS:
        raise ConversationError(400, f"Message {i} : « attachments » : liste de {MAX_ATTACHMENTS} documents au plus.")
    out = []
    for a in raw:
        if not isinstance(a, dict) or not isinstance(a.get("id"), str) or not 0 < len(a["id"]) <= 64:
            raise ConversationError(400, f"Message {i} : pièce jointe sans identifiant de document.")
        out.append(
            {
                "id": a["id"],
                "filename": str(a.get("filename") or "document")[:255],
                "kind": str(a.get("kind") or "")[:16],
                "size_bytes": a["size_bytes"] if isinstance(a.get("size_bytes"), int) else None,
            }
        )
    return out


def _version(v, i: int, depth: int, count: list[int]) -> dict:
    if not isinstance(v, dict):
        raise ConversationError(400, f"Message {i} : chaque version est un objet (content, meta, tail).")
    out = {"content": _content(v.get("content"), i)}
    if isinstance(v.get("meta"), dict):
        out["meta"] = v["meta"]
    if v.get("attachments"):
        out["attachments"] = _attachments(v["attachments"], i)
    if v.get("tail"):
        out["tail"] = _messages(v["tail"], depth + 1, count)
    return out


def walk(messages: list[dict]):
    """Tous les messages, versions inactives et leurs suites comprises."""
    for m in messages:
        yield m
        for v in m.get("versions") or []:
            yield v
            yield from walk(v.get("tail") or [])


def validate_messages(raw) -> list[dict]:
    out = _messages(raw, 0, [0])
    if len(json.dumps(out, ensure_ascii=False)) > MAX_CHARS:
        raise ConversationError(
            413, f"Conversation trop volumineuse ({MAX_CHARS // 1_000_000} Mo au plus, images comprises)."
        )
    return out


def _messages(raw, depth: int, count: list[int]) -> list[dict]:
    if not isinstance(raw, list):
        raise ConversationError(400, "« messages » doit être une liste.")
    if len(raw) > MAX_MESSAGES:
        raise ConversationError(413, f"Conversation trop longue : {MAX_MESSAGES} messages au plus.")
    if depth > MAX_DEPTH:
        raise ConversationError(400, f"Variantes imbriquées sur plus de {MAX_DEPTH} niveaux.")
    out = []
    for i, m in enumerate(raw):
        count[0] += 1
        if count[0] > MAX_NODES:
            raise ConversationError(
                413, f"Conversation trop longue : {MAX_NODES} messages au plus, variantes comprises."
            )
        if not isinstance(m, dict) or m.get("role") not in ROLES:
            raise ConversationError(
                400, f"Message {i} invalide : « role » ({', '.join(ROLES)}) et « content » (texte) requis."
            )
        msg = {"role": m["role"], "content": _content(m.get("content"), i)}
        if isinstance(m.get("meta"), dict):  # modèle, coût, latence affichés sous la réponse (5.5)
            msg["meta"] = m["meta"]
        if m.get("attachments"):
            msg["attachments"] = _attachments(m["attachments"], i)
        if m.get("versions") is not None:  # message modifié / réponse régénérée (19.3)
            versions, active = m["versions"], m.get("active")
            if not isinstance(versions, list) or not 2 <= len(versions) <= MAX_VERSIONS:
                raise ConversationError(400, f"Message {i} : « versions » : de 2 à {MAX_VERSIONS} versions.")
            if not isinstance(active, int) or isinstance(active, bool) or not 0 <= active < len(versions):
                raise ConversationError(400, f"Message {i} : « active » : index de la version affichée.")
            msg["versions"] = [_version(v, i, depth, count) for v in versions]
            msg["versions"][active].pop("tail", None)  # sa suite est celle de la liste
            msg["active"] = active
        out.append(msg)
    return out


def clean_title(title) -> str:
    if not isinstance(title, str):
        return ""
    title = re.sub(r"\s+", " ", title).strip().strip("\"'«»“”.:").strip()
    return title[:MAX_TITLE]


def fallback_title(messages: list[dict]) -> str:
    first = next((text_of(m["content"]) for m in messages if m["role"] == "user" and text_of(m["content"]).strip()), "")
    text = re.sub(r"\s+", " ", first).strip()
    if not text:
        return "Nouvelle conversation"
    return text if len(text) <= FALLBACK_TITLE_CHARS else text[:FALLBACK_TITLE_CHARS].rstrip() + "…"


class Conversations:
    def __init__(self, store: DocumentStore, cipher: Cipher, clock=time.time):
        self.store, self.cipher, self.clock = store, cipher, clock
        # recherche (19.3) : (propriétaire, portée, id) -> (révision, textes des messages visibles), en mémoire
        self._texts: OrderedDict[tuple[str, str, str], tuple[int, list[str]]] = OrderedDict()

    @staticmethod
    def _aad(owner: str, scope: str, conv_id: str) -> str:
        return f"conversation:{owner}:{scope}:{conv_id}"

    def _open(self, owner: str, scope: str, row: dict, with_messages: bool) -> dict:
        aad = self._aad(owner, scope, row["id"])
        try:
            title = self.cipher.decrypt(row["title"], aad + ":title")
        except CryptoError:
            title = UNREADABLE
        out = {
            "id": row["id"],
            "title": title,
            "private": bool(row["private"]),
            "message_count": row["message_count"],
            "revision": row["revision"],
            "project_id": row.get("project_id"),  # projet du chat : instructions et fichiers communs
            "starred": bool(row.get("starred")),  # favorite : en tête de la barre latérale
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if with_messages:
            try:
                out["messages"] = json.loads(self.cipher.decrypt(row["body"], aad))
            except CryptoError as e:
                raise ConversationError(
                    500,
                    "Conversation illisible : la clé de chiffrement de la gateway a changé "
                    "(restaurez-la : python -m gateway.backup import-key).",
                ) from e
        return out

    def _row(self, owner: str, scope: str, conv_id: str) -> dict:
        row = self.store.get_conversation(owner, scope, conv_id or "")
        if not row:
            raise ConversationError(404, "Conversation introuvable.")
        return row

    def _project(self, owner: str, project_id) -> str | None:
        """Projet du compte (les projets appartiennent au compte, comme leurs documents) ; None : aucun."""
        if project_id is None:
            return None
        if not isinstance(project_id, str) or not self.store.get_project(owner, project_id):
            raise ConversationError(404, "Projet introuvable.")
        return project_id

    def create(
        self, owner: str, scope: str, messages: list[dict], title: str = "", private: bool = False, project_id=None
    ) -> dict:
        project_id = self._project(owner, project_id)
        if self.store.count_conversations(owner, scope) >= MAX_CONVERSATIONS:
            raise ConversationError(409, f"{MAX_CONVERSATIONS} conversations au plus : supprimez-en d'anciennes.")
        conv_id = f"conv_{secrets.token_hex(12)}"
        aad, now = self._aad(owner, scope, conv_id), self.clock()
        self.store.add_conversation(
            owner,
            scope,
            {
                "id": conv_id,
                "title": self.cipher.encrypt(clean_title(title), aad + ":title"),
                "body": self.cipher.encrypt(json.dumps(messages, ensure_ascii=False), aad),
                "private": bool(private),
                "message_count": len(messages),
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "project_id": project_id,
            },
        )
        return self.get(owner, scope, conv_id)

    def get(self, owner: str, scope: str, conv_id: str) -> dict:
        return self._open(owner, scope, self._row(owner, scope, conv_id), with_messages=True)

    def list(self, owner: str, scope: str, query: str = "", limit: int = 50, offset: int = 0, project=None) -> dict:
        rows = self.store.list_conversations(owner, scope)
        if project:  # conversations d'un projet
            rows = [r for r in rows if r.get("project_id") == project]
        found = [self._open(owner, scope, r, with_messages=False) for r in rows]
        if query.strip():  # titres chiffrés : filtrés après déchiffrement
            q = query.strip().casefold()
            found = [c for c in found if q in c["title"].casefold()]
        return {
            "object": "list",
            "data": found[offset : offset + limit],
            "total": len(found),
            "has_more": offset + limit < len(found),
        }

    def _message_texts(self, owner: str, scope: str, row: dict) -> list[str]:
        key = (owner, scope, row["id"])
        hit = self._texts.get(key)
        if hit and hit[0] == row["revision"]:
            self._texts.move_to_end(key)
            return hit[1]
        full = self.store.get_conversation(owner, scope, row["id"]) or {}  # la liste ne porte pas les messages
        try:
            messages = json.loads(self.cipher.decrypt(full["body"], self._aad(owner, scope, row["id"])))
        except (CryptoError, KeyError):
            messages = []
        texts = [text_of(m.get("content")) for m in messages]
        self._texts[key] = (row["revision"], texts)
        while len(self._texts) > SEARCH_CACHE:
            self._texts.popitem(last=False)
        return texts

    def search(self, owner: str, scope: str, query: str, limit: int = 50, offset: int = 0) -> dict:
        """Conversations dont le titre ou les messages visibles contiennent tous les mots cherchés (sans tenir
        compte de la casse), avec un extrait et l'index du premier message trouvé."""
        terms = [t for t in query.casefold().split() if t][:10]
        found = []
        for row in self.store.list_conversations(owner, scope):
            conv = self._open(owner, scope, row, with_messages=False)
            texts = self._message_texts(owner, scope, row)
            title = conv["title"].casefold()
            folded = [t.casefold() for t in texts]
            if not all(term in title or any(term in f for f in folded) for term in terms):
                continue
            hit = next((i for i, f in enumerate(folded) if any(term in f for term in terms)), None)
            if hit is not None:
                text, low = texts[hit], folded[hit]
                pos = min(low.find(t) for t in terms if t in low)
                start = max(0, pos - SNIPPET)
                end = pos + SNIPPET * 2
                snippet = re.sub(r"\s+", " ", text[start:end]).strip()
                conv["match"] = {
                    "message": hit,
                    "snippet": ("…" if start else "") + snippet + ("…" if end < len(text) else ""),
                }
            found.append(conv)
        return {
            "object": "list",
            "data": found[offset : offset + limit],
            "total": len(found),
            "has_more": offset + limit < len(found),
        }

    def save_messages(self, owner: str, scope: str, conv_id: str, messages: list[dict], revision) -> dict:
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise ConversationError(400, "« revision » (entier, celle de la conversation lue) requis.")
        self._row(owner, scope, conv_id)
        ok = self.store.update_conversation(
            owner,
            scope,
            conv_id,
            {
                "body": self.cipher.encrypt(json.dumps(messages, ensure_ascii=False), self._aad(owner, scope, conv_id)),
                "message_count": len(messages),
                "updated_at": self.clock(),
            },
            expected_revision=revision,
        )
        if not ok:
            raise ConversationError(409, "Conversation modifiée depuis un autre appareil : rechargez-la.")
        return self._open(owner, scope, self._row(owner, scope, conv_id), with_messages=False)

    def update(self, owner: str, scope: str, conv_id: str, title=None, private=None, project=..., starred=None) -> dict:
        """Titre, mode privé et projet (None : retirée du projet) : sans changer la révision (ne fait pas échouer
        l'enregistrement d'un autre appareil en cours)."""
        self._row(owner, scope, conv_id)
        fields = {}
        if title is not None:
            if not clean_title(title):
                raise ConversationError(400, "Titre vide.")
            fields["title"] = self.cipher.encrypt(clean_title(title), self._aad(owner, scope, conv_id) + ":title")
        if private is not None:
            if not isinstance(private, bool):
                raise ConversationError(400, "« private » doit valoir true ou false.")
            fields["private"] = private
        if project is not ...:
            fields["project_id"] = self._project(owner, project)
        if starred is not None:
            if not isinstance(starred, bool):
                raise ConversationError(400, "« starred » doit valoir true ou false.")
            fields["starred"] = starred
        if fields:
            self.store.update_conversation(owner, scope, conv_id, fields, bump=False)
        return self._open(owner, scope, self._row(owner, scope, conv_id), with_messages=False)

    def delete(self, owner: str, scope: str, conv_id: str) -> None:
        try:
            messages = self.get(owner, scope, conv_id)["messages"]
        except ConversationError as e:
            if e.status == 404:
                raise
            messages = []  # illisible : la conversation part quand même
        if not self.store.delete_conversation(owner, scope, conv_id):
            raise ConversationError(404, "Conversation introuvable.")
        # documents joints dans le chat (19.2) : supprimés avec elle, sauf s'ils servent aussi à un projet
        for doc_id in {a["id"] for m in walk(messages) for a in m.get("attachments") or []}:
            if not self.store.document_in_project(doc_id):
                self.store.delete(owner, doc_id)

    # ---------- partage par lien, en lecture seule : copie figée du fil affiché ----------
    @staticmethod
    def _share_id(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _share_out(self, row: dict) -> dict:
        token = self.cipher.decrypt(row["token"], f"share:{row['id']}:token")
        return {"token": token, "path": f"/partage/{token}", "created_at": row["created_at"]}

    def share(self, owner: str, scope: str, conv_id: str) -> dict:
        """Nouveau lien (l'ancien cesse de fonctionner) vers la conversation telle qu'elle est maintenant : questions
        et réponses affichées, sans les variantes ni les fiches des documents joints."""
        conv = self.get(owner, scope, conv_id)
        thread = [
            {"role": m["role"], "content": m["content"], "model": (m.get("meta") or {}).get("model")}
            for m in conv["messages"]
            if m["role"] in ("user", "assistant")
        ]
        if not thread:
            raise ConversationError(400, "Conversation vide : rien à partager.")
        token = secrets.token_urlsafe(32)
        share_id = self._share_id(token)
        aad = f"share:{share_id}"
        self.store.set_share(
            {
                "id": share_id,
                "owner": owner,
                "scope": scope,
                "conv_id": conv_id,
                "token": self.cipher.encrypt(token, aad + ":token"),
                "title": self.cipher.encrypt(conv["title"] or fallback_title(conv["messages"]), aad + ":title"),
                "body": self.cipher.encrypt(json.dumps(thread, ensure_ascii=False), aad),
                "created_at": self.clock(),
            }
        )
        return self._share_out(self.store.get_share(owner, scope, conv_id))

    def share_status(self, owner: str, scope: str, conv_id: str) -> dict:
        self._row(owner, scope, conv_id)
        row = self.store.get_share(owner, scope, conv_id)
        return {"shared": bool(row), **(self._share_out(row) if row else {})}

    def unshare(self, owner: str, scope: str, conv_id: str) -> bool:
        self._row(owner, scope, conv_id)
        return self.store.delete_share(owner, scope, conv_id)

    def shared(self, token: str) -> dict:
        """Lecture publique, par le jeton seul : titre, fil et date du partage."""
        row = self.store.find_share(self._share_id(token)) if isinstance(token, str) and len(token) <= 100 else None
        if not row:
            raise ConversationError(404, "Ce lien de partage n'existe pas ou a été retiré.")
        aad = f"share:{row['id']}"
        try:
            title = self.cipher.decrypt(row["title"], aad + ":title")
            messages = json.loads(self.cipher.decrypt(row["body"], aad))
        except CryptoError as e:
            raise ConversationError(410, "Ce partage n'est plus lisible.") from e
        return {"title": title, "messages": messages, "created_at": row["created_at"]}

    async def generate_title(self, gw: Gateway, ctx: PlanContext, owner: str, scope: str, conv_id: str) -> dict:
        """Titre d'une conversation qui n'en a pas : modèle LOCAL autorisé pour la clé, sinon début du message."""
        conv = await asyncio.to_thread(self.get, owner, scope, conv_id)
        if conv["title"]:
            return conv
        messages = conv["messages"]
        title = ""
        local = [m for m in gw._routable(ctx) if m.hosting == "local"]
        first_user = next((text_of(m["content"]) for m in messages if m["role"] == "user"), "")
        first_reply = strip_reasoning(next((text_of(m["content"]) for m in messages if m["role"] == "assistant"), ""))
        if local and first_user:
            sample = f"Utilisateur : {first_user[:2000]}\n\nAssistant : {first_reply[:1000]}"
            body = {
                "messages": [{"role": "system", "content": TITLE_PROMPT}, {"role": "user", "content": sample}],
                "max_tokens": 24,
                "temperature": 0.2,
            }
            decision = route(classify(body), "economy", local, gw.latency, effort="low")
            by_ref = {m.ref: m for m in local}
            for ref in [decision.ref] + decision.alternatives[:1]:
                info = by_ref.get(ref)
                if not info:
                    continue
                try:
                    resp = await asyncio.wait_for(
                        gw.providers[info.provider].chat(gw.upstream_payload(body, info, "low", "off", None)), 30
                    )
                    title = clean_title(strip_reasoning(resp["choices"][0]["message"].get("content")))
                except (TimeoutError, ProviderError, KeyError, IndexError, TypeError):
                    continue
                if title:
                    break
        title = title or fallback_title(messages)
        return await asyncio.to_thread(self.update, owner, scope, conv_id, title)
