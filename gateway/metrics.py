"""Capture asynchrone des métriques par requête (tâche 6.1).

La requête dépose sa ligne dans une file mémoire (put_nowait, aucune E/S) ; une tâche de fond
écrit par lots en SQLite (thread séparé). File pleine : la ligne est abandonnée et comptée —
la capture ne ralentit ni ne fait jamais échouer une requête.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

log = logging.getLogger("gateway.metrics")

COLUMNS = (
    "ts",
    "owner",
    "key_id",
    "project_id",
    "plan",
    "endpoint",
    "stream",
    "status",
    "error_code",
    "provider",
    "model",
    "hosting",
    "mode",
    "effort",
    "task",
    "complexity",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "total_tokens",
    "cost_usd",
    "billed_usd",
    "billed_eur",
    "fx_rate",
    "saved_usd",
    "compression_usd",
    "latency_ms",
    "ttft_ms",
    "cache_hit",
    "usage_estimated",
    "fallback_from",
    "agents",
)


class MetricsRecorder:
    def __init__(self, store, batch: int | None = None, interval: float | None = None, maxsize: int | None = None):
        self.store = store
        self.batch = batch or int(os.getenv("METRICS_BATCH", "200"))
        self.interval = interval if interval is not None else float(os.getenv("METRICS_FLUSH_SECONDS", "1"))
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize or int(os.getenv("METRICS_QUEUE_MAX", "10000")))
        self.captured = self.written = self.dropped = 0
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.on_written = None  # rappel après écriture (alertes budgétaires, 6.5)
        self.prometheus = None  # compteurs de /metrics (18.5)

    # ---------- côté requête : jamais bloquant ----------
    def capture(self, row: dict) -> None:
        row = {c: row.get(c) for c in COLUMNS}
        row["ts"] = row["ts"] or time.time()
        if self.prometheus is not None:
            self.prometheus.observe_request(row)
        try:
            self.queue.put_nowait(row)
            self.captured += 1
            self._wake.set()
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped in (1, 100) or self.dropped % 1000 == 0:
                log.warning("File de métriques pleine : %s ligne(s) abandonnée(s)", self.dropped)

    # ---------- écrivain de fond ----------
    def _drain(self, limit: int) -> list[dict]:
        rows = []
        while len(rows) < limit:
            try:
                rows.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return rows

    async def _write(self, rows: list[dict]) -> None:
        if not rows:
            return
        try:
            await asyncio.to_thread(self.store.insert_metrics, rows)
            self.written += len(rows)
        except Exception as e:  # noqa: BLE001 — ne jamais tuer l'écrivain
            self.dropped += len(rows)
            log.error("Écriture de %s métrique(s) impossible : %s", len(rows), e)
            return
        if self.on_written:
            try:
                await self.on_written(rows)
            except Exception as e:  # noqa: BLE001 — une alerte ne bloque pas l'écriture
                log.error("Évaluation des alertes impossible : %s", e)

    async def run(self) -> None:
        # les lignes restent dans la file jusqu'à leur écriture : flush() les voit toujours
        while True:
            await self._wake.wait()
            await asyncio.sleep(self.interval)  # laisse s'accumuler un lot
            self._wake.clear()
            await self.flush()

    async def flush(self) -> None:
        """Écrit tout ce qui est en attente (lecture cohérente, arrêt propre)."""
        async with self._lock:
            while not self.queue.empty():
                await self._write(self._drain(self.batch))

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self.flush()

    def stats(self) -> dict:
        return {
            "captured": self.captured,
            "written": self.written,
            "dropped": self.dropped,
            "pending": self.queue.qsize(),
        }
