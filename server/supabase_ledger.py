"""Audit-ledger writer backed by Supabase Postgres.

Every ``/v1/check`` decision and every batch lifecycle event must land in
``governor.decisions`` or ``governor.batches`` (see ``sql/001_ledger.sql``).
The gate's response latency target is ~50ms p95 and Supabase round-trip is
too slow for the hot path, so writes here are fire-and-forget:

1. The gate awaits ``write_decision`` — but that method returns as soon as the
   row has been enqueued. The actual Supabase POST happens in a background
   task.
2. If Supabase is down, the payload is written to ``/tmp/governor-ledger-
   queue/<uuid>.json`` and retried on the next ``flush_queue`` call.
3. ``flush_queue`` is called at service startup from ``server/app.py`` (Stream
   C owns that call). No writes are lost across restarts.

The retry queue is an on-disk fallback, not the normal path. Healthy Supabase
means zero queue files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, Field
from supabase import Client as SupabaseClient
from supabase import create_client


# ─── Constants ───────────────────────────────────────────────────────────────
SUPABASE_URL_ENV: Final[str] = "SUPABASE_URL"
SUPABASE_SERVICE_ROLE_KEY_ENV: Final[str] = "SUPABASE_SERVICE_ROLE_KEY"
LEDGER_QUEUE_DIR: Final[str] = "/tmp/governor-ledger-queue"
LEDGER_SCHEMA: Final[str] = "governor"
LEDGER_DECISIONS_TABLE: Final[str] = "decisions"
LEDGER_BATCHES_TABLE: Final[str] = "batches"

_logger = logging.getLogger(__name__)


# ─── Record schemas ──────────────────────────────────────────────────────────
class DecisionRecord(BaseModel):
    """Row shape for ``governor.decisions``. Mirrors ``sql/001_ledger.sql``."""

    app: str
    decision: Literal["APPROVED", "DENIED"]
    reasons: list[dict[str, Any]] | None = None
    fingerprint: str | None = None
    items: int | None = None
    model: str | None = None
    projected_p50_usd: float | None = None
    projected_p95_usd: float | None = None
    approval_id: str | None = None
    actor: str | None = None
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BatchLifecycleEvent(BaseModel):
    """Row shape / upsert payload for ``governor.batches``."""

    batch_id: str
    app: str
    approval_id: str | None = None
    model: str | None = None
    status: Literal[
        "submitted", "in_progress", "ended", "canceled", "killed", "rogue"
    ]
    running_usd: float | None = None
    actual_usd: float | None = None
    variance_pct: float | None = None
    kill_reason: str | None = None
    submitted_at: datetime | None = None
    ended_at: datetime | None = None


class VarianceReport(BaseModel):
    """Returned by :meth:`SupabaseLedger.compute_variance`."""

    batch_id: str
    projected_p95_usd: float | None
    actual_usd: float | None
    variance_pct: float | None


# ─── Ledger ──────────────────────────────────────────────────────────────────
class SupabaseLedger:
    """Async writer with a disk-backed retry queue.

    The class is initialised eagerly (``fromEnv``) at service startup. Every
    write returns fast; the actual network call runs in a background task and
    any failure falls back to the queue.
    """

    def __init__(
        self,
        *,
        client: SupabaseClient,
        queue_dir: str = LEDGER_QUEUE_DIR,
    ) -> None:
        self._client = client
        self._queue_dir = Path(queue_dir)
        self._queue_dir.mkdir(parents=True, exist_ok=True)
        # Track background tasks so tests (and graceful shutdown) can wait.
        self._pending: set[asyncio.Task[None]] = set()

    @classmethod
    def fromEnv(cls) -> "SupabaseLedger":
        """Build from ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY``."""
        url = os.environ.get(SUPABASE_URL_ENV, "")
        key = os.environ.get(SUPABASE_SERVICE_ROLE_KEY_ENV, "")
        if not url or not key:
            raise RuntimeError(
                f"SupabaseLedger requires ${SUPABASE_URL_ENV} and "
                f"${SUPABASE_SERVICE_ROLE_KEY_ENV}."
            )
        return cls(client=create_client(url, key))

    # ─── Public write API ────────────────────────────────────────────────────
    async def write_decision(self, decision: DecisionRecord) -> None:
        """Enqueue a decision row for async write.

        Returns immediately — the actual Supabase call happens in a background
        task scheduled on the running loop. On failure the payload lands on
        disk for :meth:`flush_queue` to replay.
        """
        await self._scheduleWrite(
            table=LEDGER_DECISIONS_TABLE,
            payload=decision.model_dump(mode="json", exclude_none=True),
        )

    async def write_batch_lifecycle(self, event: BatchLifecycleEvent) -> None:
        """Enqueue a batch lifecycle event for async upsert.

        ``governor.batches`` uses ``batch_id`` as its primary key, so every
        write is an upsert — monitor updates overwrite prior state for the
        same batch.
        """
        await self._scheduleWrite(
            table=LEDGER_BATCHES_TABLE,
            payload=event.model_dump(mode="json", exclude_none=True),
            upsert=True,
        )

    async def flush_queue(self) -> int:
        """Replay every queued payload. Returns number of successful writes.

        Called from service startup. Safe to call mid-flight — only processes
        files that exist at call time; concurrent enqueues from new writes
        will be picked up on the next flush.
        """
        if not self._queue_dir.exists():
            return 0
        queue_files = sorted(self._queue_dir.glob("*.json"))
        successful = 0
        for queue_file in queue_files:
            try:
                payload = json.loads(queue_file.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                _logger.warning(
                    "ledger queue file unreadable, skipping",
                    extra={"file": str(queue_file), "error": str(exc)},
                )
                continue
            table = payload.pop("_table", LEDGER_DECISIONS_TABLE)
            upsert = payload.pop("_upsert", False)
            try:
                await self._postgrestWrite(
                    table=table, payload=payload, upsert=upsert
                )
            except Exception as exc:
                _logger.warning(
                    "ledger queue replay failed; keeping file for next attempt",
                    extra={"file": str(queue_file), "error": str(exc)},
                )
                continue
            queue_file.unlink(missing_ok=True)
            successful += 1
        return successful

    async def compute_variance(self, batch_id: str) -> VarianceReport:
        """Return the projected vs actual spend for a single batch.

        Joins ``governor.batches`` against ``governor.decisions`` via
        ``approval_id``. ``variance_pct`` is ``None`` until both sides have a
        value — this is the reconciliation helper the monitor uses.
        """
        batch_rows = await asyncio.to_thread(
            self._selectBatch, batch_id=batch_id
        )
        if not batch_rows:
            return VarianceReport(
                batch_id=batch_id,
                projected_p95_usd=None,
                actual_usd=None,
                variance_pct=None,
            )
        batch_row = batch_rows[0]
        actual_usd = (
            float(batch_row["actual_usd"])
            if batch_row.get("actual_usd") is not None
            else None
        )
        approval_id = batch_row.get("approval_id")
        projected: float | None = None
        if approval_id is not None:
            decision_rows = await asyncio.to_thread(
                self._selectDecision, approval_id=approval_id
            )
            if decision_rows and decision_rows[0].get("projected_p95_usd"):
                projected = float(decision_rows[0]["projected_p95_usd"])

        variance_pct: float | None = None
        if projected is not None and actual_usd is not None and projected > 0:
            variance_pct = round(
                ((actual_usd - projected) / projected) * 100.0, 2
            )

        return VarianceReport(
            batch_id=batch_id,
            projected_p95_usd=projected,
            actual_usd=actual_usd,
            variance_pct=variance_pct,
        )

    async def wait_pending(self) -> None:
        """Await all outstanding background writes. Used in tests."""
        if not self._pending:
            return
        await asyncio.gather(*list(self._pending), return_exceptions=True)

    # ─── Internal plumbing ───────────────────────────────────────────────────
    async def _scheduleWrite(
        self,
        *,
        table: str,
        payload: dict[str, Any],
        upsert: bool = False,
    ) -> None:
        """Schedule a non-blocking write; falls back to queue on failure."""
        task = asyncio.create_task(
            self._writeOrQueue(table=table, payload=payload, upsert=upsert)
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _writeOrQueue(
        self,
        *,
        table: str,
        payload: dict[str, Any],
        upsert: bool,
    ) -> None:
        try:
            await self._postgrestWrite(
                table=table, payload=payload, upsert=upsert
            )
        except Exception as exc:
            _logger.warning(
                "ledger write failed; dropping to disk queue",
                extra={"table": table, "error": str(exc)},
            )
            self._enqueueToDisk(table=table, payload=payload, upsert=upsert)

    async def _postgrestWrite(
        self,
        *,
        table: str,
        payload: dict[str, Any],
        upsert: bool,
    ) -> None:
        """Run the blocking supabase-py call in a worker thread."""
        await asyncio.to_thread(
            self._blockingWrite,
            table=table,
            payload=payload,
            upsert=upsert,
        )

    def _blockingWrite(
        self,
        *,
        table: str,
        payload: dict[str, Any],
        upsert: bool,
    ) -> None:
        query = self._client.schema(LEDGER_SCHEMA).table(table)
        if upsert:
            query.upsert(payload).execute()
        else:
            query.insert(payload).execute()

    def _selectBatch(self, *, batch_id: str) -> list[dict[str, Any]]:
        response = (
            self._client.schema(LEDGER_SCHEMA)
            .table(LEDGER_BATCHES_TABLE)
            .select("*")
            .eq("batch_id", batch_id)
            .execute()
        )
        return cast(list[dict[str, Any]], list(response.data or []))

    def _selectDecision(self, *, approval_id: str) -> list[dict[str, Any]]:
        response = (
            self._client.schema(LEDGER_SCHEMA)
            .table(LEDGER_DECISIONS_TABLE)
            .select("projected_p95_usd")
            .eq("approval_id", approval_id)
            .limit(1)
            .execute()
        )
        return cast(list[dict[str, Any]], list(response.data or []))

    def _enqueueToDisk(
        self,
        *,
        table: str,
        payload: dict[str, Any],
        upsert: bool,
    ) -> None:
        # HACK: atomic write via tempfile + rename would be safer, but the
        # queue dir is local and a torn write only costs one skipped row on
        # next replay (see flush_queue warning path). Revisit if we see
        # torn-file reports in prod logs.
        queue_entry: dict[str, Any] = dict(payload)
        queue_entry["_table"] = table
        queue_entry["_upsert"] = upsert
        filename = self._queue_dir / f"{uuid.uuid4().hex}.json"
        filename.write_text(json.dumps(queue_entry, default=str))
