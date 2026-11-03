"""Comptes nommés gérés depuis l'interface (20.3), par la clé maître.

Un compte peut créer ses clés virtuelles, ses conversations, ses alertes… comme ceux de GATEWAY_KEYS, toujours lus
(compatibilité, affichés en lecture seule). Différences :
- jeton GÉNÉRÉ par la gateway (sk-acct- + 43 caractères aléatoires : le garde-fou de 8.1 est tenu d'office), montré
  une seule fois, conservé haché comme les clés virtuelles (HMAC-SHA-256 avec GATEWAY_KEY_PEPPER, 4.1) ;
- identité aléatoire (acct:…), indépendante du jeton : renouveler un jeton perdu ou divulgué ne fait rien perdre
  (clés, conversations, abonnement, historique) ;
- désactivation immédiate : le jeton ET les clés virtuelles du compte sont refusés (403) ; réactivation possible ;
- suppression : clés du compte révoquées, jeton supprimé (les données restent rattachées à l'identité, purgées
  avec leurs propres règles de conservation).
"""

from __future__ import annotations

import re
import secrets
import time

from .keys import hash_secret
from .plans import ACCOUNT_PREFIX, PLANS, normalize_plan

MAX_ACCOUNTS = 500
MAX_NAME = 60
STATUSES = ("active", "disabled")
TOUCH_EVERY = 60  # secondes : « dernière utilisation » écrite au plus une fois par minute


class AccountError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def generate_token() -> str:
    return ACCOUNT_PREFIX + secrets.token_urlsafe(32)


def display_prefix(token: str) -> str:
    return f"{token[:12]}…{token[-4:]}"


def _name(value) -> str:
    name = re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""
    if not name:
        raise AccountError(400, "Nom du compte requis.")
    if len(name) > MAX_NAME:
        raise AccountError(400, f"Nom du compte : {MAX_NAME} caractères au plus.")
    return name


def _plan(value) -> str:
    if value is None:
        return "free"  # offres identiques : tout est inclus
    if value not in PLANS:
        raise AccountError(400, f"Plan : {', '.join(PLANS)}.")
    return normalize_plan(value)


def public(account: dict) -> dict:
    """Fiche affichable : jamais l'empreinte du jeton."""
    return {
        k: account[k]
        for k in (
            "id",
            "identity",
            "name",
            "plan",
            "token_prefix",
            "status",
            "created_at",
            "updated_at",
            "last_used_at",
        )
    } | {"source": "interface"}


class Accounts:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    # -- résolution, à chaque requête --
    def by_token(self, token: str) -> dict | None:
        account = self.store.account_by_hash(hash_secret(token))
        if account and account["status"] == "active":
            now = self.clock()
            if not account["last_used_at"] or now - account["last_used_at"] >= TOUCH_EVERY:
                self.store.update_account(account["id"], {"last_used_at": now})
        return account

    def plan_of(self, identity: str) -> str | None:
        if not identity.startswith("acct:"):
            return None
        account = self.store.account_by_identity(identity)
        return normalize_plan(account["plan"]) if account and account["status"] == "active" else None

    def blocked(self, identity: str) -> bool:
        """Compte nommé désactivé : ses clés virtuelles sont refusées aussi."""
        if not identity.startswith("acct:"):
            return False
        account = self.store.account_by_identity(identity)
        return bool(account) and account["status"] != "active"

    # -- gestion (clé maître) --
    def _get(self, account_id: str) -> dict:
        account = self.store.get_account(account_id or "")
        if not account:
            raise AccountError(404, "Compte introuvable.")
        return account

    def _unique(self, name: str, except_id: str | None = None) -> None:
        if any(a["name"].casefold() == name.casefold() and a["id"] != except_id for a in self.store.list_accounts()):
            raise AccountError(409, f"Un compte s'appelle déjà « {name} ».")

    def list(self) -> list[dict]:
        return [public(a) for a in self.store.list_accounts()]

    def create(self, name, plan) -> tuple[dict, str]:
        name, plan = _name(name), _plan(plan)
        if len(self.store.list_accounts()) >= MAX_ACCOUNTS:
            raise AccountError(409, f"{MAX_ACCOUNTS} comptes au plus.")
        self._unique(name)
        token, now = generate_token(), self.clock()
        account = {
            "id": f"acc_{secrets.token_hex(8)}",
            "identity": f"acct:{secrets.token_hex(12)}",  # même forme que les comptes de GATEWAY_KEYS
            "name": name,
            "plan": plan,
            "token_hash": hash_secret(token),
            "token_prefix": display_prefix(token),
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "last_used_at": None,
        }
        self.store.add_account(account)
        return public(account), token

    def update(self, account_id: str, body: dict) -> dict:
        account = self._get(account_id)
        unknown = set(body) - {"name", "plan", "status"}
        if unknown:
            raise AccountError(400, f"Champs inconnus : {', '.join(sorted(unknown))}.")
        fields = {}
        if "name" in body:
            fields["name"] = _name(body["name"])
            self._unique(fields["name"], account_id)
        if "plan" in body:
            fields["plan"] = _plan(body["plan"])
        if "status" in body:
            if body["status"] not in STATUSES:
                raise AccountError(400, "Statut : active ou disabled.")
            fields["status"] = body["status"]
        if fields:
            self.store.update_account(account_id, {**fields, "updated_at": self.clock()})
        return public(self._get(account["id"]))

    def rotate(self, account_id: str) -> tuple[dict, str]:
        self._get(account_id)
        token = generate_token()
        self.store.update_account(
            account_id,
            {"token_hash": hash_secret(token), "token_prefix": display_prefix(token), "updated_at": self.clock()},
        )
        return public(self._get(account_id)), token

    def delete(self, account_id: str) -> int:
        """Supprime le compte et révoque ses clés virtuelles ; renvoie le nombre de clés révoquées."""
        account = self._get(account_id)
        revoked = 0
        for key in self.store.list_keys(account["identity"]):
            if key["status"] != "revoked":
                self.store.set_key_status(account["identity"], key["id"], "revoked", "compte supprimé", actor="admin")
                revoked += 1
        self.store.delete_account(account_id)
        return revoked
