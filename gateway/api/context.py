"""Contexte partagé des routes : identité de l'appelant (plan, clé, IP réelle), droits (gestion, admin),
accès sans clé et accès distant.

Fonctions communes à plusieurs groupes de routes, créées une fois par application (10.8).
"""

from __future__ import annotations

import ipaddress
import os
import time
from datetime import datetime, UTC
from types import SimpleNamespace

from fastapi import Request

from ..config import lan_mode
from ..keys import WEB_2FA, hash_secret, is_virtual as is_virtual_key
from ..local_gpu import CALLER
from ..permissions import check_network, private_permissions
from ..plans import (
    InvalidCredentials,
    MIN_TOKEN_LENGTH,
    PlanContext,
    PlanError,
    PlanResolver,
    account_identity,
    apply_requested,
    normalize_plan,
)
from ..remote import RemoteAccess, TunnelError
from ..totp import COOKIE as ADMIN_COOKIE, SESSION_HEADER, AdminSessionRequired, TotpError
from ..core import log
from ..local_net import local_address

# posés par un tunnel ou un proxy, jamais par un navigateur qui joint directement la gateway
PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-real-ip", "forwarded", "true-client-ip")


def build(holder: dict) -> SimpleNamespace:
    def check_exposure(public: bool) -> None:
        """Refuse d'exposer publiquement une instance dont un identifiant de compte est devinable."""
        weak = holder["gw"].plans.weak_tokens()
        if public and weak:
            raise TunnelError(
                409,
                f"Exposition publique refusée : identifiant(s) devinable(s) — moins de "
                f"{MIN_TOKEN_LENGTH} caractères ou valeur d'exemple — ({', '.join(weak)}). "
                "Remplacez-les par des jetons aléatoires (relancez lancer.ps1 pour régénérer le "
                "compte de test).",
            )

    async def start_remote(provider: str, mode: str | None, allow_anonymous: bool, managed: dict | None = None) -> dict:
        """Tunnel actif = clé requise (12.2), sauf dérogation explicite `allow_anonymous`."""
        check_exposure(not (provider == "tailscale" and mode != "funnel"))
        holder["remote_anonymous"] = False
        status = await holder["remote"].start(provider, mode, managed)
        holder["remote_anonymous"] = allow_anonymous
        if allow_anonymous and not holder["gw"].plans.require_key:
            log.warning(
                "Accès distant SANS clé autorisé (allow_anonymous) : les requêtes anonymes consomment vos crédits."
            )
        return status

    def anonymous_networks() -> list | None:
        """GATEWAY_ANONYMOUS_FROM : réseaux d'où l'accès sans clé est permis ("*" = partout ; défaut :
        cette machine seulement). None = partout."""
        raw = os.getenv("GATEWAY_ANONYMOUS_FROM")
        loopback = "127.0.0.1/32,::1/128"
        if lan_mode() and os.getenv("GATEWAY_LAN_ANONYMOUS", "false").lower() not in ("1", "true", "yes"):
            # mode réseau local (12.1) : les autres appareils doivent présenter une clé. Les réseaux élargis
            # (pont Docker…) sont ignorés : Docker Desktop y fait apparaître les appareils du réseau.
            raw = loopback
        raw = loopback if raw is None else raw
        if raw.strip() == "*":
            return None
        nets = []
        for part in raw.split(","):
            try:
                nets.append(ipaddress.ip_network(part.strip(), strict=False))
            except ValueError:
                if part.strip():
                    log.error("GATEWAY_ANONYMOUS_FROM : réseau invalide ignoré « %s ».", part.strip())
        return nets

    def anonymous_allowed(request: Request) -> bool:
        """Accès sans clé (si GATEWAY_REQUIRE_KEY est désactivé) : jamais à travers un tunnel actif, sauf
        dérogation ; sinon seulement depuis GATEWAY_ANONYMOUS_FROM. On lit l'IP du pair TCP, pas les en-têtes
        (falsifiables)."""
        if holder["remote"].active:
            return holder["remote_anonymous"]
        nets = anonymous_networks()
        if nets is None:
            return True
        if unmanaged_proxy(request):  # relayée par un tunnel inconnu de la gateway : pas « cette machine »
            return False
        try:
            peer = ipaddress.ip_address(request.client.host if request.client else "")
        except ValueError:
            return False
        return any(peer in n for n in nets)

    async def auto_connect() -> None:
        provider = os.getenv("REMOTE_ACCESS", "").strip().lower()
        if not provider:
            return
        mode = os.getenv("REMOTE_ACCESS_MODE")
        try:
            status = await start_remote(
                provider, mode, os.getenv("REMOTE_ALLOW_ANONYMOUS", "false").lower() in ("1", "true", "yes")
            )
            log.warning("Instance accessible à distance : %s", status["public_url"])
        except TunnelError as e:
            log.error("Accès distant (%s) non démarré : %s", provider, e.message)

    def plan_ctx(body: dict, request: Request, web_session: bool = True) -> PlanContext:
        """`web_session=False` : seulement pour ouvrir la session elle-même (POST /v1/admin/session)."""
        ip = client_ip(request)
        guarded = not is_local_console(ip)
        if guarded:
            holder["auth"].check(ip)  # IP bloquée pour force brute (12.3)
        try:
            ctx = _plan_ctx(body, request, web_session)
        except InvalidCredentials:
            if guarded:
                holder["auth"].fail(ip)
            raise
        private = body.pop("private", None) if "private" in body else request.headers.get("x-gateway-private")
        if private is True or str(private).lower() in ("1", "true", "yes", "on"):
            ctx.permissions = private_permissions(ctx.permissions)  # mode privé : IA locales seulement
        request.state.gw_ctx = ctx  # pour la capture des requêtes en erreur (6.1)
        CALLER.set((ctx.identity, ctx.is_admin))  # files d'attente des serveurs locaux, PC occupé (18.4)
        return ctx

    def _plan_ctx(body: dict, request: Request, web_session: bool = True) -> PlanContext:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key")
        requested = body.pop("plan", None) or request.headers.get("x-gateway-plan")
        ip = client_ip(request)
        if is_virtual_key(token):  # clé virtuelle sk-app-… (4.1)
            found = holder["store"].key_by_hash(hash_secret(token))
            if not found:
                raise InvalidCredentials()
            owner, key = found
            # statut vérifié en base à CHAQUE requête : révocation/suspension instantanées (4.4)
            if key["status"] == "revoked":
                raise PlanError(401, "Clé API révoquée.")
            if key["status"] == "suspended":
                if key["suspended_until"] and key["suspended_until"] <= time.time():
                    holder["store"].set_key_status(owner, key["id"], "active", "fin de suspension", actor="auto")
                else:
                    until = (
                        f" jusqu'au {datetime.fromtimestamp(key['suspended_until'], tz=UTC):%Y-%m-%d %H:%M} UTC"
                        if key["suspended_until"]
                        else ""
                    )
                    reason = f" ({key['status_reason']})" if key["status_reason"] else ""
                    raise PlanError(403, f"Clé API suspendue{until}{reason}.")
            if key["expires_at"] and key["expires_at"] <= time.time():
                raise PlanError(401, "Clé API expirée.")
            if owner.startswith("sandbox:"):  # session d'essai anonyme de l'ancienne offre (retirée) : finie
                raise PlanError(401, "Clé API expirée.")
            if web_session and is_web_key(key) and from_internet(request):
                require_web_session(request)
            resolver: PlanResolver = holder["gw"].plans
            if is_web_key(key) and resolver.master_key and owner == account_identity(resolver.master_key):
                # compte lezard-ia.fr du propriétaire : sa clé web, créée par la clé maître (« Créer mon compte web »),
                # donne les mêmes droits qu'elle ; depuis Internet, seulement avec la session ouverte par le code de
                # double authentification de ce PC (ci-dessus). Révoquée ou expirée : refusée plus haut.
                return resolver.resolve(resolver.master_key, ip, requested)
            perms = key["permissions"] or None
            if perms:  # restrictions réseau (4.2)
                check_network(perms, ip, request.headers.get("origin") or request.headers.get("referer"))
            plan = normalize_plan(key["plan"])  # ancienne offre d'essai : lue comme free
            if (perms or {}).get("tools") == "pc" and not (
                resolver.master_key and owner == account_identity(resolver.master_key)
            ):  # contrôle du PC : délégué par la clé maître seulement (clé créée avant cette règle, base restaurée…)
                perms = {**perms, "tools": "web"}
            if resolver.accounts is not None and resolver.accounts.blocked(owner):
                raise PlanError(403, "Clé API d'un compte désactivé par l'administrateur.")  # 20.3
            ctx = PlanContext(
                plan,
                owner,
                can_manage=False,
                permissions=perms,
                limits=key["limits"] or None,
                key={k: key[k] for k in ("id", "name", "project_id", "metadata")},
            )
            return apply_requested(ctx, requested)
        return holder["gw"].plans.resolve(token, ip, requested, allow_anonymous=anonymous_allowed(request))

    def is_local_console(ip: str) -> bool:
        """Le propriétaire sur sa machine, sans tunnel : jamais bloqué (il doit pouvoir se débloquer)."""
        return ip in ("127.0.0.1", "::1") and not holder["remote"].active

    def _valid_ip(value: str | None) -> str | None:
        try:
            return str(ipaddress.ip_address((value or "").strip()))
        except ValueError:
            return None

    def _forwarded_for(request: Request, hops: int) -> str | None:
        """Adresse ajoutée par le `hops`-ième proxy de confiance, en partant de la DROITE de
        X-Forwarded-For : la partie gauche est écrite par le client, donc falsifiable (8.2)."""
        chain = [h.strip() for v in request.headers.getlist("x-forwarded-for") for h in v.split(",") if h.strip()]
        return _valid_ip(chain[-hops]) if len(chain) >= hops else None

    def trust_proxy() -> bool:
        return os.getenv("GATEWAY_TRUST_PROXY", "false").lower() in ("1", "true", "yes")

    def unmanaged_proxy(request: Request) -> bool:
        """Requête relayée depuis cette machine par un tunnel ou un proxy que la gateway n'a pas lancé (cloudflared
        installé en service Windows, tunnel resté ouvert après un arrêt brutal, ngrok lancé à la main…) : elle vient
        d'ailleurs. Un navigateur qui joint directement la gateway n'envoie aucun de ces en-têtes."""
        peer = request.client.host if request.client else ""
        return (
            peer in ("127.0.0.1", "::1")
            and not holder["remote"].active
            and not trust_proxy()
            and any(request.headers.get(h) for h in PROXY_HEADERS)
        )

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        remote: RemoteAccess = holder["remote"]
        # derrière un tunnel (5.6), le démon local relaie toutes les requêtes depuis 127.0.0.1 : on lit l'IP
        # réelle qu'il transmet — uniquement si la connexion vient bien de la machine, et selon le tunnel
        if remote.active and peer in ("127.0.0.1", "::1"):
            if remote.status.provider == "cloudflare":
                # Cloudflare écrase CF-Connecting-IP en bordure : seul en-tête fiable avec ce tunnel
                return _valid_ip(request.headers.get("cf-connecting-ip")) or peer
            # ngrok / Tailscale ajoutent l'IP du visiteur à la fin de X-Forwarded-For (CF-Connecting-IP ignoré)
            return _forwarded_for(request, 1) or peer
        if trust_proxy():
            try:
                hops = max(1, int(os.getenv("GATEWAY_TRUSTED_HOPS", "1")))
            except ValueError:
                hops = 1
            return _forwarded_for(request, hops) or peer
        if unmanaged_proxy(request):
            # tunnel inconnu : l'IP qu'il transmet (Cloudflare, sinon le dernier proxy), jamais celle de ce PC
            ip = (
                _valid_ip(request.headers.get("cf-connecting-ip")) if request.headers.get("cf-ray") else None
            ) or _forwarded_for(request, 1)
            return ip if ip and not ipaddress.ip_address(ip).is_loopback else "unknown"
        return peer

    def manager(request: Request) -> PlanContext:
        ctx = plan_ctx({}, request)
        if not ctx.can_manage:
            raise PlanError(
                403,
                "Gestion des clés réservée aux identifiants de compte "
                "(GATEWAY_MASTER_KEY ou GATEWAY_KEYS), pas aux clés virtuelles.",
            )
        return ctx

    # ----- secrets des providers, chiffrés au repos (4.4) -----
    def admin(request: Request) -> PlanContext:
        ctx = plan_ctx({}, request)
        if not ctx.is_admin:
            raise PlanError(403, "Réservé à la clé maître (GATEWAY_MASTER_KEY).")
        admin_session(request)
        return ctx

    # ----- double authentification de l'administration (20.1) -----
    def session_token(request: Request) -> str | None:
        return request.cookies.get(ADMIN_COOKIE) or request.headers.get(SESSION_HEADER)

    def admin_session_required(request: Request) -> bool:
        """Hors de ce PC, avec la double authentification active : la clé maître seule ne suffit plus."""
        totp = holder.get("totp")
        return bool(totp and not is_local_console(client_ip(request)) and totp.enabled())

    def from_internet(request: Request) -> bool:
        """Requête venue d'Internet : par un tunnel public (l'IP du visiteur transmise par le tunnel) ou d'une
        adresse publique. Ce PC, le réseau local, Tailscale privé : non."""
        peer = request.client.host if request.client else ""
        ip = client_ip(request)
        if unmanaged_proxy(request):
            return True
        if holder["remote"].active and peer in ("127.0.0.1", "::1"):
            # sans IP transmise, la requête ne vient pas du tunnel (qui l'ajoute toujours) : navigateur de ce PC
            return ip != peer and holder["remote"].status.mode != "private"
        try:
            return ipaddress.ip_address(ip).is_global
        except ValueError:
            return False

    def relayed(request: Request) -> bool:
        """Offre Gratuite (29.7) : requête passée par un relais ou une page web d'ailleurs, même depuis la maison.
        Un navigateur de la maison qui joint directement la gateway n'envoie aucun en-tête de proxy, et l'adresse
        qu'il a tapée (Host) ou la page d'où il appelle (Origin) est celle de ce PC ou d'un appareil de la maison."""
        if any(request.headers.get(h) for h in PROXY_HEADERS):
            return True
        origin = request.headers.get("origin")
        for url in (f"http://{request.headers.get('host', '')}", origin if origin and origin != "null" else None):
            if url and not local_address(url):
                return True
        return False

    def remote_admin_blocked(request: Request) -> bool:
        """Administration depuis Internet SANS double authentification : refusée (la clé maître seule, si elle
        fuit, donnerait tout). ADMIN_REMOTE_WITHOUT_TOTP=true lève ce garde-fou en connaissance de cause."""
        totp = holder.get("totp")
        if not totp or os.getenv("ADMIN_REMOTE_WITHOUT_TOTP", "false").lower() in ("1", "true", "yes"):
            return False
        return from_internet(request) and not totp.enabled()

    def is_web_key(key: dict) -> bool:
        return bool((key.get("metadata") or {}).get(WEB_2FA))

    def require_web_session(request: Request) -> dict:
        """Clé web (compte lezard-ia.fr) depuis Internet : la clé seule ne suffit jamais. Il faut une session ouverte
        par un code de double authentification, vérifié ici avec le secret de CE PC (qui n'en sort pas). Sans
        double authentification active sur ce PC : refusée."""
        totp = holder.get("totp")
        if not totp or not totp.enabled():
            raise TotpError(
                403,
                "Clé web refusée depuis Internet : la double authentification de ce PC est désactivée. "
                "Réactivez-la sur le PC (Accès par le site, « Créer mon compte web »).",
            )
        s = totp.session(session_token(request), holder["gw"].plans.master_key or "")
        if s is None:
            raise AdminSessionRequired(
                "Connexion depuis Internet : saisissez le code de votre application d'authentification pour ce PC."
            )
        return s

    def admin_session(request: Request) -> dict | None:
        """Session d'administration valide (ou None sur ce PC / sans double authentification) ; sinon
        AdminSessionRequired. À appeler après avoir vérifié la clé maître."""
        if remote_admin_blocked(request):
            raise TotpError(
                403,
                "Administration depuis Internet refusée : activez d'abord la double authentification "
                "(Administration → Double authentification), depuis ce PC ou votre réseau local. "
                "Le chat et l'API restent accessibles à distance.",
            )
        if not admin_session_required(request):
            return None
        s = holder["totp"].session(session_token(request), holder["gw"].plans.master_key)
        if s is None:  # clé maître ou compte du propriétaire (clé web) : le même code de double authentification
            raise AdminSessionRequired()
        request.state.admin_session_id = s["id"]  # journal d'audit (20.2)
        return s

    return SimpleNamespace(
        holder=holder,
        _forwarded_for=_forwarded_for,
        _plan_ctx=_plan_ctx,
        _valid_ip=_valid_ip,
        admin=admin,
        admin_session=admin_session,
        admin_session_required=admin_session_required,
        anonymous_allowed=anonymous_allowed,
        anonymous_networks=anonymous_networks,
        auto_connect=auto_connect,
        check_exposure=check_exposure,
        client_ip=client_ip,
        from_internet=from_internet,
        is_local_console=is_local_console,
        is_web_key=is_web_key,
        manager=manager,
        plan_ctx=plan_ctx,
        remote_admin_blocked=remote_admin_blocked,
        session_token=session_token,
        start_remote=start_remote,
        unmanaged_proxy=unmanaged_proxy,
    )
