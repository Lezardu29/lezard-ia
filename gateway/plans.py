"""Plans d'accès (tâche 2.4) : tout est inclus pour tous, multi-agents compris. Les deux noms, « free » et « pro »,
restent lus pour les clés, comptes et règles de fournisseur déjà enregistrés ; une règle de fournisseur peut encore
réserver un fournisseur au plan « pro ».

Identifiants :
- clé maître GATEWAY_MASTER_KEY (plan GATEWAY_MASTER_PLAN, pro par défaut) et tokens de compte
  GATEWAY_KEYS (JSON {"<token>": "<plan>"}) : peuvent gérer les clés virtuelles ;
- clés virtuelles `sk-app-…` (4.1) : résolues en base par la gateway ;
- sinon plan GATEWAY_DEFAULT_PLAN (ou 401 si GATEWAY_REQUIRE_KEY=true).
L'identité stockée est une empreinte du token, jamais le token lui-même.
Un client peut demander un plan inférieur au sien (en-tête X-Gateway-Plan), jamais supérieur.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass


PLANS = ("free", "pro")
MIN_TOKEN_LENGTH = 24  # en dessous, un identifiant de compte est devinable (ex. « compte-test »)
ACCOUNT_PREFIX = "sk-acct-"  # jeton d'un compte nommé créé depuis l'interface (20.3)
PLACEHOLDERS = ("change-moi", "changez-moi", "changeme", "change-me", "change_me")  # valeurs d'exemple (9.2)


def is_placeholder(value: str | None) -> bool:
    """Valeur recopiée d'un fichier d'exemple (deploy/.env.example) : publique, donc connue de tous."""
    v = (value or "").lower()
    return any(p in v for p in PLACEHOLDERS)


RANK = {p: i for i, p in enumerate(PLANS)}
# ancien plan d'essai (retiré) et anciens noms : lus comme free, pour qu'une configuration existante démarre
ALIASES = {"sandbox": "free", "test": "free", "demo": "free", "gratuit": "free", "standard": "free"}
FEATURES = {
    "free": {"multi_agent": True, "billed": True, "models": "tous"},
    "pro": {"multi_agent": True, "billed": True, "models": "tous"},
}


class PlanError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class InvalidCredentials(PlanError):
    """Identifiant présenté mais inconnu : compté contre la force brute (12.3)."""

    def __init__(self, message: str = "Clé API invalide."):
        super().__init__(401, message)


def normalize_plan(value: str) -> str:
    v = ALIASES.get(value.strip().lower(), value.strip().lower())
    if v not in PLANS:
        raise PlanError(400, f"Plan inconnu '{value}'. Valeurs : free, pro.")
    return v


@dataclass
class PlanContext:
    plan: str
    identity: str  # propriétaire des données (compte)
    can_manage: bool = False  # peut gérer les clés virtuelles (identifiant de compte)
    key: dict | None = None  # clé virtuelle utilisée : {id, name, project_id, …}
    base_plan: str | None = None  # plan maximal de l'identifiant (avant rétrogradation demandée)
    permissions: dict | None = None  # restrictions de la clé virtuelle (4.2)
    limits: dict | None = None  # plafonds & rate-limits de la clé virtuelle (4.3)
    is_admin: bool = False  # clé maître : administration de l'instance (4.4)


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def account_identity(token: str) -> str:
    return "acct:" + hashlib.sha256(token.encode()).hexdigest()[:24]


def apply_requested(ctx: PlanContext, requested: str | None) -> PlanContext:
    ctx.base_plan = ctx.base_plan or ctx.plan
    if requested:
        plan = normalize_plan(requested)
        if RANK[plan] > RANK[ctx.base_plan]:
            raise PlanError(403, f"Le plan '{plan}' n'est pas souscrit (plan actuel : {ctx.base_plan}).")
        ctx.plan = plan
    return ctx


class PlanResolver:
    def __init__(
        self,
        keys: dict[str, str] | None = None,
        default: str | None = None,
        master_key: str | None = None,
        require_key: bool | None = None,
    ):
        if keys is None:
            keys = json.loads(os.getenv("GATEWAY_KEYS", "{}"))
        self.keys = {k: normalize_plan(v) for k, v in keys.items()}
        master_key = master_key if master_key is not None else os.getenv("GATEWAY_MASTER_KEY")
        self.master_key = master_key or None
        if master_key:
            self.keys[master_key] = normalize_plan(os.getenv("GATEWAY_MASTER_PLAN", "pro"))
        self.default = normalize_plan(default or os.getenv("GATEWAY_DEFAULT_PLAN", "free"))
        self.require_key = (
            require_key
            if require_key is not None
            else os.getenv("GATEWAY_REQUIRE_KEY", "false").lower() in ("1", "true", "yes")
        )
        self.identity_plans = {account_identity(t): p for t, p in self.keys.items()}
        # recherche par empreinte puis comparaison en temps constant (9.6) : le temps de réponse ne dépend pas
        # des caractères du jeton secret
        self._by_digest = {_digest(t): t for t in self.keys}
        self.accounts = None  # comptes nommés en base (20.3), branchés au démarrage : gateway.accounts.Accounts

    def account_token(self, token: str | None) -> str | None:
        """Identifiant de compte configuré correspondant à `token`, sinon None."""
        if not token:
            return None
        known = self._by_digest.get(_digest(token))
        return known if known is not None and hmac.compare_digest(known.encode(), token.encode()) else None

    def weak_tokens(self) -> list[str]:
        """Identifiants de compte trop courts ou d'exemple, pour une instance exposée (décrits sans les révéler)."""
        return sorted(
            "GATEWAY_MASTER_KEY" if t == self.master_key else f"GATEWAY_KEYS (compte {p})"
            for t, p in self.keys.items()
            if len(t) < MIN_TOKEN_LENGTH or is_placeholder(t)
        )

    def configured_plan(self, identity: str) -> str | None:
        """Plan donné par l'administrateur : GATEWAY_KEYS, ou compte nommé en base (20.3)."""
        found = self.identity_plans.get(identity)
        if found is None and self.accounts is not None:
            found = self.accounts.plan_of(identity)
        return found

    def account_plan(self, identity: str) -> str | None:
        """Plan effectif d'un compte : celui donné par l'administrateur."""
        return self.configured_plan(identity)

    def resolve(
        self, token: str | None, client_ip: str, requested: str | None, allow_anonymous: bool = True
    ) -> PlanContext:
        """Identifiants de compte et anonymes (les clés `sk-app-` sont résolues par la gateway).
        `allow_anonymous=False` : accès sans clé refusé même sans GATEWAY_REQUIRE_KEY (instance exposée, 12.2)."""
        if (known := self.account_token(token)) is not None:
            ident = account_identity(known)
            ctx = PlanContext(
                self.account_plan(ident) or self.keys[known],
                ident,
                can_manage=True,
                is_admin=bool(self.master_key) and hmac.compare_digest(known.encode(), self.master_key.encode()),
            )
        elif token and token.startswith(ACCOUNT_PREFIX) and self.accounts is not None:
            account = self.accounts.by_token(token)  # compte nommé (20.3) ; inconnu : tentative comptée
            if account is None:
                raise InvalidCredentials()
            if account["status"] != "active":
                raise PlanError(403, "Compte désactivé par l'administrateur de la gateway.")
            ctx = PlanContext(
                self.account_plan(account["identity"]) or normalize_plan(account["plan"]),
                account["identity"],
                can_manage=True,
            )
        elif token and (self.require_key or not allow_anonymous):
            raise InvalidCredentials()
        elif self.require_key:
            raise PlanError(401, "Clé API requise (Authorization: Bearer sk-app-…).")
        elif not allow_anonymous:
            raise PlanError(
                401,
                "Clé API requise : l'accès sans clé n'est permis que depuis cette machine, et pas "
                "pendant un accès distant (Authorization: Bearer sk-app-…).",
            )
        else:
            ctx = PlanContext(self.default, account_identity(token) if token else f"ip:{client_ip}")
        return apply_requested(ctx, requested)
