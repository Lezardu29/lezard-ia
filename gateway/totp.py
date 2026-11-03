"""Double authentification de l'administration (20.1) : codes TOTP (RFC 6238) et codes de secours.

Hors de ce PC (http://127.0.0.1 sans tunnel), la clé maître seule ne suffit plus pour /v1/admin/* : elle s'échange,
avec un code de l'application d'authentification, contre une session courte (cookie HttpOnly, 12 h par défaut),
révocable. Sur le PC, la clé maître seule suffit toujours : on ne s'enferme jamais dehors.

Le secret est chiffré au repos (AES-GCM, comme les clés des fournisseurs) ; les codes de secours ne sont gardés que
hachés. Un code TOTP déjà accepté ne l'est plus jamais (on retient le dernier pas de 30 s utilisé) ; un code de
secours ne sert qu'une fois.

Réinitialisation (téléphone perdu, sans code de secours), sur le PC : .\\lancer.ps1 -ResetTotp
(ou python -m gateway.totp reset).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import sys
import time
from urllib.parse import quote

from .crypto import Cipher, CryptoError

STEP = 30  # secondes par code
DIGITS = 6
DRIFT = 1  # pas acceptés de part et d'autre (horloge du téléphone décalée)
SETTING = "admin_totp"
CONTEXT = "admin_totp"  # AAD du secret chiffré
SETUP_TTL = 900  # un QR code affiché mais jamais validé expire
RECOVERY_COUNT = 10
COOKIE = "gw_admin_session"
SESSION_HEADER = "x-gateway-admin-session"  # scripts : même jeton qu'en cookie
ISSUER = "Smart AI Gateway"
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # sans 0/o, 1/l/i : se recopie sans erreur


def session_seconds() -> int:
    try:
        hours = float(os.getenv("ADMIN_SESSION_HOURS", "12"))
    except ValueError:
        hours = 12
    return int(min(max(hours, 0.25), 24 * 7) * 3600)


class TotpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class AdminSessionRequired(TotpError):
    """Clé maître valide, mais pas de session d'administration (code TOTP) depuis une adresse distante."""

    def __init__(self, message: str | None = None):
        super().__init__(
            401,
            message
            or "Administration à distance : la double authentification est activée. Saisissez le "
            "code de votre application d'authentification (POST /v1/admin/session).",
        )


# ---------- RFC 4226 / 6238 (stdlib seulement) ----------
def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")  # 160 bits, comme Google Authenticator


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def provisioning_uri(secret: str, label: str) -> str:
    return (
        f"otpauth://totp/{quote(ISSUER)}:{quote(label)}?secret={secret}&issuer={quote(ISSUER)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP}"
    )


def qr_svg(text: str) -> str | None:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None
    return qrcode.make(text, image_factory=qrcode.image.svg.SvgPathFillImage, border=2).to_string(encoding="unicode")


def _normalize(code: str) -> str:
    return re.sub(r"[\s-]", "", code or "").lower()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_recovery_codes() -> list[str]:
    codes = []
    for _ in range(RECOVERY_COUNT):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))  # ~49 bits chacun
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def master_fingerprint(master_key: str) -> str:
    """Une session n'est valable que pour la clé maître qui l'a ouverte : changer la clé maître les ferme toutes."""
    return hashlib.sha256(b"admin-session:" + master_key.encode()).hexdigest()[:24]


class AdminTotp:
    def __init__(self, store, cipher: Cipher, clock=time.time):
        self.store, self.cipher, self.clock = store, cipher, clock

    # ----- état -----
    def _state(self) -> dict:
        return self.store.get_setting(SETTING) or {}

    def enabled(self) -> bool:
        return bool(self._state().get("enabled"))

    def status(self) -> dict:
        st = self._state()
        return {
            "enabled": bool(st.get("enabled")),
            "enabled_at": st.get("enabled_at"),
            "recovery_left": len(st.get("recovery") or []) if st.get("enabled") else None,
            "session_hours": session_seconds() / 3600,
        }

    def _secret(self, st: dict, field: str = "secret") -> str:
        try:
            return self.cipher.decrypt(st[field], CONTEXT)
        except CryptoError as e:
            raise TotpError(
                500,
                "Secret TOTP illisible (clé de chiffrement changée ?) : réinitialisez la double "
                "authentification sur le PC (.\\lancer.ps1 -ResetTotp).",
            ) from e

    def _replace(self, before: dict, after: dict) -> bool:
        if not before:
            self.store.set_setting(SETTING, after)
            return True
        return self.store.replace_setting(SETTING, before, after)

    # ----- activation -----
    def begin_setup(self, label: str) -> dict:
        st = self._state()
        if st.get("enabled"):
            raise TotpError(409, "Double authentification déjà active : désactivez-la avant d'en créer une autre.")
        secret = new_secret()
        self.store.set_setting(
            SETTING, {"enabled": False, "pending": self.cipher.encrypt(secret, CONTEXT), "pending_at": self.clock()}
        )
        uri = provisioning_uri(secret, label)
        return {"secret": secret, "uri": uri, "svg": qr_svg(uri), "digits": DIGITS, "period": STEP}

    def enable(self, code: str) -> list[str]:
        st = self._state()
        if st.get("enabled"):
            raise TotpError(409, "Double authentification déjà active.")
        if not st.get("pending") or self.clock() - st.get("pending_at", 0) > SETUP_TTL:
            raise TotpError(409, "Aucune activation en cours (ou QR code expiré) : recommencez.")
        secret = self._secret(st, "pending")
        step = self._match(secret, _normalize(code), -1)
        if step is None:
            raise TotpError(401, "Code incorrect : vérifiez l'heure du téléphone et saisissez le code affiché.")
        codes = new_recovery_codes()
        after = {
            "enabled": True,
            "secret": st["pending"],
            "last_step": step,
            "enabled_at": self.clock(),
            "recovery": [_hash(_normalize(c)) for c in codes],
        }
        if not self._replace(st, after):
            raise TotpError(409, "Activation concurrente : rechargez la page.")
        return codes

    def disable(self) -> None:
        self.store.set_setting(SETTING, {"enabled": False})
        self.store.delete_admin_sessions()

    def regenerate_recovery(self, code: str) -> list[str]:
        self.verify(code, recovery=False)
        for _ in range(3):
            st = self._state()
            codes = new_recovery_codes()
            if self._replace(st, {**st, "recovery": [_hash(_normalize(c)) for c in codes]}):
                return codes
        raise TotpError(409, "Modification concurrente : réessayez.")

    # ----- vérification -----
    def _match(self, secret: str, code: str, last_step: int) -> int | None:
        if not re.fullmatch(rf"\d{{{DIGITS}}}", code):
            return None
        now = int(self.clock() // STEP)
        for step in range(now - DRIFT, now + DRIFT + 1):
            if step > last_step and hmac.compare_digest(code_at(secret, step), code):
                return step
        return None

    def verify(self, code: str, recovery: bool = True) -> str:
        """Consomme un code (TOTP, ou de secours si `recovery`) : 'totp' ou 'recovery' ; TotpError(401) sinon.
        Atomique : un même code présenté deux fois en même temps n'est accepté qu'une fois."""
        code = _normalize(code)
        for _ in range(3):
            st = self._state()
            if not st.get("enabled"):
                raise TotpError(409, "La double authentification n'est pas activée.")
            step = self._match(self._secret(st), code, st.get("last_step", -1))
            if step is not None:
                if self._replace(st, {**st, "last_step": step}):
                    return "totp"
                continue  # un autre worker a écrit entre-temps : on relit
            hashed = _hash(code)
            if recovery and hashed in (st.get("recovery") or []):
                if self._replace(st, {**st, "recovery": [h for h in st["recovery"] if h != hashed]}):
                    return "recovery"
                continue
            break
        raise TotpError(401, "Code de double authentification invalide ou déjà utilisé.")

    # ----- sessions -----
    def open_session(self, master_key: str, ip: str, user_agent: str | None, via: str) -> tuple[str, dict]:
        token = "gwa_" + secrets.token_urlsafe(32)
        now = self.clock()
        session = {
            "id": _hash(token),
            "master": master_fingerprint(master_key),
            "ip": ip,
            "user_agent": (user_agent or "")[:200] or None,
            "via": via,
            "created_at": now,
            "expires_at": now + session_seconds(),
        }
        self.store.add_admin_session(session)
        return token, public_session(session)

    def session(self, token: str | None, master_key: str) -> dict | None:
        if not token or not token.startswith("gwa_") or len(token) > 100:
            return None
        s = self.store.get_admin_session(_hash(token))
        if not s or s["expires_at"] <= self.clock():
            return None
        if not hmac.compare_digest(s["master"], master_fingerprint(master_key)):
            return None
        return s

    def sessions(self, current: str | None = None) -> list[dict]:
        cur = _hash(current) if current else None
        return [{**public_session(s), "current": s["id"] == cur} for s in self.store.list_admin_sessions(self.clock())]

    def revoke(self, session_id: str | None = None) -> int:
        return self.store.delete_admin_sessions(session_id)

    def close(self, token: str | None) -> bool:
        return bool(token) and self.store.delete_admin_sessions(_hash(token)) > 0


def public_session(s: dict) -> dict:
    return {k: s[k] for k in ("id", "ip", "user_agent", "via", "created_at", "expires_at")}


def main(argv: list[str] | None = None) -> None:
    from .store import DocumentStore, default_db_path

    parser = argparse.ArgumentParser(prog="python -m gateway.totp", description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="état de la double authentification")
    sub.add_parser("reset", help="désactive la double authentification et ferme les sessions (sur le PC)")
    args = parser.parse_args(argv)
    store = DocumentStore(default_db_path())
    try:
        st = store.get_setting(SETTING) or {}
        if args.cmd == "status":
            print("Double authentification : " + ("ACTIVE" if st.get("enabled") else "désactivée"))
            return
        store.set_setting(SETTING, {"enabled": False})
        n = store.delete_admin_sessions()
        print(
            "Double authentification désactivée"
            + (f", {n} session(s) fermée(s)" if n else "")
            + ". Réactivez-la depuis l'administration (section Double authentification)."
        )
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
