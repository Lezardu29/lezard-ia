"""Accès depuis Internet (5.6, 31.1) : tunnels Cloudflare, ngrok et Tailscale lancés et surveillés par la gateway."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from gateway.remote import BINARIES, PROVIDERS, TRYCLOUDFLARE, TunnelError, TunnelState, log  # noqa: F401


def _pid_file() -> Path:
    return Path(os.getenv("GATEWAY_DATA_DIR", "data")) / "cloudflared.pid"


def _is_cloudflared(pid: int) -> bool:
    """Le numéro désigne-t-il encore un cloudflared ? (un autre programme peut l'avoir repris)"""
    if sys.platform == "win32":
        # tasklist écrit dans la page de code de la console (cp850 en français) : en UTF-8 strict (PYTHONUTF8=1),
        # un accent la rendait illisible, stdout valait None et l'accès à distance ne démarrait plus
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        ).stdout
        return "cloudflared" in (out or "").lower()
    try:
        return "cloudflared" in Path(f"/proc/{pid}/comm").read_text()
    except OSError:
        return False


def _stop_orphan() -> None:
    """cloudflared laissé par une gateway arrêtée brutalement (il survit à son parent, tunnel toujours ouvert) :
    arrêté avant d'en lancer un autre, sinon ils s'accumulent à chaque redémarrage."""
    f = _pid_file()
    try:
        pid = int(f.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return
    if pid > 0 and _is_cloudflared(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            log.warning("cloudflared orphelin arrêté (processus %d).", pid)
        except OSError:
            pass
    f.unlink(missing_ok=True)


class RemoteAccess:
    def __init__(self, port: int | None = None, binaries: dict[str, list[str]] | None = None):
        self.port = port or int(os.getenv("GATEWAY_PORT", "8000"))
        self.timeout = float(os.getenv("REMOTE_URL_TIMEOUT", "30"))  # attente de l'URL publique
        self._bins = binaries or {}
        self.status = TunnelState()
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._stopping = False  # arrêt demandé : pas de redémarrage automatique
        self._managed: dict | None = None  # tunnel fourni par le serveur central (31.1)
        self._reconnect_task: asyncio.Task | None = None
        self.retry_min = float(os.getenv("REMOTE_RETRY_MIN", "2"))
        self.retry_max = float(os.getenv("REMOTE_RETRY_MAX", "60"))
        self.on_url_change = None  # rappel (ancienne URL, nouvelle URL) après un redémarrage

    @property
    def active(self) -> bool:
        """Tunnel voulu par l'admin (y compris pendant une reconnexion) : la clé reste exigée."""
        return self.status.state in ("starting", "running", "reconnecting")

    def command(self, provider: str) -> list[str] | None:
        """Commande de base du binaire (surchargeable pour les tests), None s'il est introuvable."""
        if provider in self._bins:
            return list(self._bins[provider])
        if provider == "cloudflare":  # aussi la copie téléchargée par l'accès à distance automatique
            from gateway import cloudflared

            path = cloudflared.find()
            return [path] if path else None
        env = os.getenv(f"{BINARIES[provider].upper()}_BIN")
        path = env or shutil.which(BINARIES[provider])
        return [path] if path else None

    def available(self) -> dict[str, bool]:
        return {p: self.command(p) is not None for p in PROVIDERS}

    # ---------- démarrage ----------
    async def start(self, provider: str, mode: str | None = None, managed: dict | None = None) -> dict:
        """`managed` (31.1) : {token, hostname} d'un tunnel Cloudflare fourni par le serveur central de Lezard IA,
        à la place de CLOUDFLARE_TUNNEL_TOKEN / CLOUDFLARE_TUNNEL_HOSTNAME (gardé pour les reconnexions)."""
        if provider not in PROVIDERS:
            raise TunnelError(400, f"Fournisseur inconnu '{provider}'. Valeurs : {', '.join(PROVIDERS)}.")
        base = self.command(provider)
        if not base:
            raise TunnelError(
                424,
                f"Binaire '{BINARIES[provider]}' introuvable : installez-le ou "
                f"indiquez son chemin dans {BINARIES[provider].upper()}_BIN.",
            )
        async with self._lock:
            if self.active:
                raise TunnelError(409, f"Un tunnel {self.status.provider} est déjà actif ({self.status.public_url}).")
            self._stopping = False
            self._managed = managed
            self.status = TunnelState(
                provider=provider,
                state="starting",
                started_at=time.time(),
                mode="private" if provider == "tailscale" and mode != "funnel" else "public",
            )
            try:
                if provider == "tailscale":
                    await self._start_tailscale(base, mode or "serve")
                else:
                    await self._start_process(provider, base)
            except TunnelError as e:
                await self._kill()
                self.status.state, self.status.error = "error", e.message
                raise
        return self.status.to_dict()

    def _local_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _start_process(self, provider: str, base: list[str]) -> None:
        # secrets transmis par variables d'environnement, jamais en ligne de commande : celle-ci est lisible
        # par tout utilisateur de la machine dans la liste des processus (9.6)
        env = dict(os.environ)
        if provider == "cloudflare":
            managed = self._managed or {}
            token = managed.get("token") or os.getenv("CLOUDFLARE_TUNNEL_TOKEN")
            args = (
                ["tunnel", "--no-autoupdate", "run"]
                if token
                else ["tunnel", "--no-autoupdate", "--url", self._local_url()]
            )
            if token:
                env["TUNNEL_TOKEN"] = token  # lu nativement par cloudflared
            hostname = managed.get("hostname") or os.getenv("CLOUDFLARE_TUNNEL_HOSTNAME")
            fixed_url = hostname if token else None
        else:
            # ngrok lit NGROK_AUTHTOKEN dans son environnement (hérité tel quel)
            args = ["http", str(self.port), "--log", "stdout", "--log-format", "json"]
            if os.getenv("NGROK_DOMAIN"):
                args += ["--url", os.environ["NGROK_DOMAIN"]]
            fixed_url = None
        orphans = provider == "cloudflare" and not self._bins  # jamais avec les binaires de test
        if orphans:
            await asyncio.to_thread(_stop_orphan)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *base,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except OSError as e:
            raise TunnelError(424, f"Impossible de lancer {base[0]} : {e}")
        if orphans:
            try:
                _pid_file().write_text(str(self._proc.pid), encoding="ascii")
            except OSError:
                pass
        found: asyncio.Future = asyncio.get_running_loop().create_future()
        self._reader = asyncio.create_task(self._read(provider, found, fixed_url))
        try:
            url = await asyncio.wait_for(asyncio.shield(found), self.timeout)
        except TimeoutError:
            raise TunnelError(
                504,
                f"Aucune URL publique obtenue en {self.timeout:.0f} s. "
                f"Derniers logs : {' | '.join(list(self.status.logs)[-3:])}",
            )
        self.status.public_url, self.status.state = url, "running"
        log.info("Accès distant actif via %s : %s", provider, url)

    async def _read(self, provider: str, found: asyncio.Future, fixed_url: str | None) -> None:
        """Lit la sortie du tunnel en continu (évite de bloquer le tube) et y détecte l'URL."""
        assert self._proc and self._proc.stdout
        async for raw in self._proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            self.status.logs.append(line[:300])
            url = None
            if provider == "cloudflare":
                m = TRYCLOUDFLARE.search(line)
                if m:
                    url = m.group(0)
                elif fixed_url and "Registered tunnel connection" in line:
                    url = fixed_url if fixed_url.startswith("http") else f"https://{fixed_url}"
            else:
                try:
                    entry = json.loads(line)
                    if entry.get("msg") == "started tunnel" and entry.get("url"):
                        url = entry["url"]
                    if entry.get("lvl") in ("eror", "crit") and not found.done():
                        found.set_exception(TunnelError(502, f"ngrok : {entry.get('err') or entry.get('msg')}"))
                except ValueError:
                    pass
            if url and not found.done():
                found.set_result(url)
        code = await self._proc.wait()
        if not found.done():
            found.set_exception(
                TunnelError(
                    502,
                    f"Le tunnel s'est arrêté (code {code}) : {' | '.join(list(self.status.logs)[-3:]) or 'aucun log'}",
                )
            )
        elif self.status.state == "running" and not self._stopping:  # coupure après démarrage (12.4)
            self.status.state, self.status.error = "reconnecting", f"Tunnel interrompu (code {code}) : reconnexion…"
            log.warning("Tunnel %s interrompu (code %s) : redémarrage automatique", provider, code)
            self._reconnect_task = asyncio.create_task(self._reconnect(provider, self.status.public_url))

    async def _reconnect(self, provider: str, previous_url: str | None) -> None:
        """Relance le tunnel avec une attente progressive (retry_min -> retry_max) jusqu'au succès ou à stop()."""
        delay = self.retry_min
        while not self._stopping:
            self.status.state, self.status.next_retry_at = "reconnecting", time.time() + delay
            await asyncio.sleep(delay)
            async with self._lock:
                if self._stopping:
                    return
                base = self.command(provider)
                try:
                    if not base:
                        raise TunnelError(424, f"Binaire '{BINARIES[provider]}' introuvable.")
                    self._proc, self.status.public_url = None, None
                    await self._start_process(provider, base)
                except TunnelError as e:
                    if self._proc and self._proc.returncode is None:
                        self._proc.terminate()
                    self.status.error = (
                        f"Reconnexion impossible : {e.message} (nouvel essai dans "
                        f"{min(delay * 2, self.retry_max):.0f} s)"
                    )
                    self.status.logs.append(self.status.error[:300])
                    delay = min(delay * 2, self.retry_max)
                    continue
                self.status.restarts += 1
                self.status.error = self.status.next_retry_at = None
                new_url = self.status.public_url
            if new_url != previous_url:
                log.warning(
                    "Tunnel %s relancé : NOUVELLE adresse publique %s (avant : %s)", provider, new_url, previous_url
                )
                if self.on_url_change:
                    try:
                        self.on_url_change(previous_url, new_url)
                    except Exception:  # noqa: BLE001 — une notification ne bloque jamais le tunnel
                        log.exception("Notification de changement d'adresse impossible")
            return

    async def _run(self, *cmd: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
        text = out.decode(errors="replace")
        for line in text.splitlines()[-10:]:
            self.status.logs.append(line[:300])
        if proc.returncode:
            raise TunnelError(502, f"{' '.join(cmd[1:3])} a échoué : {text.strip()[-300:]}")
        return text

    async def _start_tailscale(self, base: list[str], mode: str) -> None:
        if mode not in ("serve", "funnel"):
            raise TunnelError(400, "Mode Tailscale : serve (privé) ou funnel (public).")
        await self._run(*base, mode, "--bg", str(self.port))
        status = json.loads(await self._run(*base, "status", "--json"))
        dns = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
        if not dns:
            raise TunnelError(502, "Nom DNS Tailscale introuvable (tailscale est-il connecté ?).")
        self.status.public_url, self.status.state = f"https://{dns}", "running"
        self._tailscale_base = base

    # ---------- arrêt ----------
    async def _kill(self) -> None:
        if self._proc and not self._bins:  # arrêt normal : plus d'orphelin à surveiller
            try:
                if _pid_file().read_text(encoding="ascii") == str(self._proc.pid):
                    _pid_file().unlink()
            except OSError:
                pass
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        if self._reader:
            self._reader.cancel()
        self._proc, self._reader = None, None

    async def stop(self) -> dict:
        self._stopping = True  # avant tout : la coupure qui suit n'est pas une panne
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        async with self._lock:
            if self.status.provider == "tailscale" and self.status.state == "running":
                try:
                    await self._run(*self._tailscale_base, "serve", "reset")
                except (TimeoutError, TunnelError, OSError) as e:
                    log.warning("Arrêt Tailscale : %s", e)
            await self._kill()
            self.status.state, self.status.public_url, self.status.next_retry_at = "stopped", None, None
        return self.status.to_dict()
