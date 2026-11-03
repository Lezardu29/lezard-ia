"""Accès distant inactif pour les tests : même interface que gateway.tunnel.RemoteAccess, sans lancer de tunnel."""

import os

from gateway.remote import PROVIDERS, TunnelError, TunnelState


class StubRemote:
    def __init__(self, port: int | None = None, binaries: dict[str, list[str]] | None = None):
        self.port = port or int(os.getenv("GATEWAY_PORT", "8000"))
        self.status = TunnelState()
        self.on_url_change = None

    @property
    def active(self) -> bool:
        return False

    def available(self) -> dict[str, bool]:
        return dict.fromkeys(PROVIDERS, False)

    async def start(self, provider: str, mode: str | None = None, managed: dict | None = None) -> dict:
        raise TunnelError(403, "Accès distant indisponible dans ce test.")

    async def stop(self) -> dict:
        return self.status.to_dict()
