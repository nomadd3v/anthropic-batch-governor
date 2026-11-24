"""Active monitor (watchdog) for the batch-governor.

Runs as a separate process under launchd alongside the FastAPI service.
Every ``MONITOR_POLL_INTERVAL_SECONDS`` it performs one tick:

1. Writes ``gov:monitor:heartbeat`` so ``/healthz`` can report freshness.
2. Fetches the Anthropic batch list and, for each batch, classifies it:

   * **Registered** — exists in ``gov:batch:{id}``. Compute running cost from
     ``request_counts`` and the approved P95. If the running cost exceeds
     ``MONITOR_RUNAWAY_MULTIPLIER x approved_p95_usd`` the batch is cancelled
     and logged as ``killed``. On ``ended`` the variance is computed and, if
     it exceeds ``VARIANCE_STALE_THRESHOLD``, the calibration reservoir is
     flagged ``stale=1`` (24h TTL) so callers refresh projections.
   * **Unregistered** AND older than ``MONITOR_ROGUE_GRACE_SECONDS`` — treated
     as a rogue submission (bypass of the gate). Cancelled and logged. The
     ``gov:rogue_seen:{id}`` key dedups across ticks (24h TTL).

3. Sleeps ``MONITOR_POLL_INTERVAL_SECONDS`` and repeats.

Graceful degradation: Anthropic 5xx / timeouts and Upstash failures skip
the current tick but never crash the loop. The heartbeat is the only signal
``/healthz`` uses to decide ``ok`` vs ``degraded`` — a stuck monitor shows up
immediately to operators.

# AI: Every Anthropic call flows through ``AnthropicClient`` (``list_batches``,
# ``cancel_batch``). No prompt calls from this module.

Size note
---------
This file is above the 300-line soft limit in STANDARDS.md. The sections are
tightly coupled — the tick body, the three per-batch classifiers, the SDK
accessors, and the run loop all share the same ``MonitorDeps`` and Upstash
key layout. Splitting would push key constants and helpers across multiple
files and create circular imports (monitor <-> accessors <-> deps). Keeping
one file is the lesser evil; the section dividers below partition the logic
so readers can jump straight to the part they care about.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Final

from anthropic import APIStatusError

from server.anthropic_client import AnthropicClient, AnthropicUnavailable
from server.flags import FlagEvent, FlagSink
from server.supabase_ledger import BatchLifecycleEvent, SupabaseLedger
from server.upstash import UpstashClient, UpstashUnavailable


# ─── Constants ───────────────────────────────────────────────────────────────
DEFAULT_POLL_INTERVAL_SECONDS: Final[int] = 60
DEFAULT_RUNAWAY_MULTIPLIER: Final[float] = 1.5
DEFAULT_ROGUE_GRACE_SECONDS: Final[int] = 30

# BUSINESS RULE: A variance of >10% (positive or negative) between projected
# P95 and actual spend flags the calibration reservoir as stale so the next
# projection refreshes fallbacks. This matches the variance threshold that
# surfaced the 2026-04-15 miscalibration.
VARIANCE_STALE_THRESHOLD: Final[float] = 0.10
CALIBRATION_STALE_TTL_SECONDS: Final[int] = 24 * 60 * 60
ROGUE_SEEN_TTL_SECONDS: Final[int] = 24 * 60 * 60
MONITOR_HEARTBEAT_TTL_SECONDS: Final[int] = 180

MONITOR_POLL_INTERVAL_ENV: Final[str] = "MONITOR_POLL_INTERVAL_SECONDS"
MONITOR_RUNAWAY_MULTIPLIER_ENV: Final[str] = "MONITOR_RUNAWAY_MULTIPLIER"
MONITOR_ROGUE_GRACE_ENV: Final[str] = "MONITOR_ROGUE_GRACE_SECONDS"
ANTHROPIC_LIST_LIMIT: Final[int] = 100

UPSTASH_MONITOR_HEARTBEAT_KEY: Final[str] = "gov:monitor:heartbeat"
UPSTASH_BATCH_KEY_PREFIX: Final[str] = "gov:batch:"
UPSTASH_APPROVAL_KEY_PREFIX: Final[str] = "gov:approval:"
UPSTASH_APP_INFLIGHT_KEY_PREFIX: Final[str] = "gov:app:"
UPSTASH_INFLIGHT_GLOBAL_KEY: Final[str] = "gov:inflight"
UPSTASH_ROGUE_SEEN_PREFIX: Final[str] = "gov:rogue_seen:"
UPSTASH_CALIBRATION_STALE_KEY_FORMAT: Final[str] = "calc:stats:{op}:{model}:stale"

# Terminal batch states as reported by Anthropic's SDK. Anything else is
# treated as in-progress / still-running for the purposes of the watchdog.
BATCH_ENDED_STATES: Final[frozenset[str]] = frozenset({
    "ended", "completed", "canceled", "expired", "failed",
})

# Event-type literals used in the structured-log lines. Keep this list short
# and stable — dashboards key off these values.
EVENT_HEARTBEAT: Final[str] = "heartbeat"
EVENT_TICK_START: Final[str] = "tick_start"
EVENT_TICK_END: Final[str] = "tick_end"
EVENT_TICK_SKIPPED: Final[str] = "tick_skipped"
EVENT_RUNNING_COST: Final[str] = "running_cost"
EVENT_KILLED: Final[str] = "killed"
EVENT_ROGUE: Final[str] = "rogue"
EVENT_VARIANCE: Final[str] = "variance"
EVENT_CALIBRATION_STALE: Final[str] = "calibration_stale"


# ─── Structured logger ──────────────────────────────────────────────────────
def _emitEvent(event: str, **fields: Any) -> None:
    """Emit a single JSON log line to stderr.

    We avoid the standard ``logging`` module for the monitor's event stream
    because the launchd plist redirects stderr to a log file already, and a
    single ``print`` call keeps the structured shape guaranteed (no handler
    formatting surprises across environments).
    """
    payload: dict[str, Any] = {
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(fields)
    print(json.dumps(payload, default=str), file=sys.stderr, flush=True)


_logger = logging.getLogger(__name__)


# ─── Configuration ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MonitorConfig:
    """Runtime knobs read from the environment at startup."""

    poll_interval_seconds: int
    runaway_multiplier: float
    rogue_grace_seconds: int

    @classmethod
    def fromEnv(cls) -> "MonitorConfig":
        return cls(
            poll_interval_seconds=_readIntEnv(
                MONITOR_POLL_INTERVAL_ENV, DEFAULT_POLL_INTERVAL_SECONDS
            ),
            runaway_multiplier=_readFloatEnv(
                MONITOR_RUNAWAY_MULTIPLIER_ENV, DEFAULT_RUNAWAY_MULTIPLIER
            ),
            rogue_grace_seconds=_readIntEnv(
                MONITOR_ROGUE_GRACE_ENV, DEFAULT_ROGUE_GRACE_SECONDS
            ),
        )


def _readIntEnv(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _readFloatEnv(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ─── Pure-function helpers (fully unit-testable) ────────────────────────────
def computeRunningCost(
    *,
    request_counts: dict[str, Any],
    approved_p95_usd: float,
    approved_requests: int,
) -> float:
    """Estimate the batch's spend so far from its ``request_counts`` block.

    ``request_counts`` is the dict returned by Anthropic on every batch
    object. We add ``processing + succeeded + errored`` (every request that
    has touched the model) and multiply by the per-item cost derived from
    the approved projection:

        per_item_usd = approved_p95_usd / approved_requests
        running_usd  = per_item_usd * (processing + succeeded + errored)

    # BUSINESS RULE: ``canceled`` requests are excluded because Anthropic
    # does not bill them. Requests still in ``expired`` are also excluded.
    # We DO NOT cap the proportion at 1.0 — a count that exceeds the
    # approved ``requests`` field indicates the submission actually sent
    # more items than the governor approved (e.g., a WAVE_MAX_SIZE bug
    # re-running the 2026-04-15 pattern). That is exactly the runaway
    # signal the monitor exists to catch, so let the number rise.

    Returns 0.0 when ``approved_requests`` is zero (divide-by-zero guard)
    or when the approved projection is missing. The monitor uses this
    value both to update the ledger and to decide whether to cancel — an
    inability to compute must never produce a false cancel.
    """
    if approved_requests <= 0 or approved_p95_usd <= 0:
        return 0.0
    processing = int(request_counts.get("processing") or 0)
    succeeded = int(request_counts.get("succeeded") or 0)
    errored = int(request_counts.get("errored") or 0)
    touched = processing + succeeded + errored
    if touched <= 0:
        return 0.0
    per_item_usd = approved_p95_usd / approved_requests
    return per_item_usd * touched


def isRunaway(
    *, running_usd: float, approved_p95_usd: float, multiplier: float
) -> bool:
    """Return True when running spend has exceeded the runaway multiplier.

    # BUSINESS RULE: Cancel when ``running_usd > multiplier * approved_p95``.
    # Default multiplier is 1.5 — anything higher than that means either the
    # approved projection was badly wrong OR the batch is looping beyond
    # what was approved. In either case the safe action is cancel.
    """
    if approved_p95_usd <= 0:
        return False
    return running_usd > (multiplier * approved_p95_usd)


def computeVariance(
    *, actual_usd: float, projected_p95_usd: float
) -> float:
    """Return the signed variance ratio ``(actual - projected) / projected``.

    Matches ``supabase_ledger.compute_variance`` semantics but operates on
    floats directly so the monitor can decide fast without a Supabase
    round-trip. Returns 0.0 when ``projected_p95_usd`` is 0 or negative.
    """
    if projected_p95_usd <= 0:
        return 0.0
    return (actual_usd - projected_p95_usd) / projected_p95_usd


def isGracePassed(
    *, created_at: datetime | None, now: datetime, grace_seconds: int
) -> bool:
    """Return True when a batch is older than the rogue grace window.

    Unregistered batches newer than the grace window are ignored — they may
    still be in the middle of the wrapper's ``/v1/check`` → ``/v1/submit``
    round-trip, and we do not want to cancel a legitimate approval that
    simply has not written its ``gov:batch:{id}`` hash yet.
    """
    if created_at is None:
        return False
    age_seconds = (now - created_at).total_seconds()
    return age_seconds >= grace_seconds


# ─── Monitor object ─────────────────────────────────────────────────────────
@dataclass
class MonitorDeps:
    """The external clients the monitor talks to. Injectable in tests.

    ``flags`` is optional so existing call sites/tests that predate the flag
    stream keep working; when present, reconciliation mismatches beyond the
    variance threshold are written to the shared flag stream.
    """

    upstash: UpstashClient
    ledger: SupabaseLedger
    anthropic: AnthropicClient
    flags: FlagSink | None = None


async def run_tick(
    *,
    deps: MonitorDeps,
    config: MonitorConfig,
    now: datetime | None = None,
) -> None:
    """Execute a single monitor tick.

    Ticks are the granularity of failure: any uncaught exception inside this
    function is logged and swallowed by :func:`runForever` so the next tick
    still runs. Heartbeat is written FIRST so a failure later in the tick
    still leaves ``/healthz`` reporting a fresh monitor.
    """
    resolved_now = now or datetime.now(timezone.utc)
    tick_id = str(uuid.uuid4())[:8]
    _emitEvent(EVENT_TICK_START, tick_id=tick_id)

    await _writeHeartbeat(upstash=deps.upstash, tick_id=tick_id, now=resolved_now)

    try:
        batches = await deps.anthropic.list_batches(limit=ANTHROPIC_LIST_LIMIT)
    except AnthropicUnavailable as exc:
        _emitEvent(
            EVENT_TICK_SKIPPED,
            tick_id=tick_id,
            reason="anthropic_unavailable",
            error=str(exc),
        )
        return

    for batch in batches:
        try:
            await _processBatch(
                deps=deps,
                config=config,
                batch=batch,
                tick_id=tick_id,
                now=resolved_now,
            )
        except UpstashUnavailable as exc:
            # One bad batch must not poison the rest of the tick.
            _emitEvent(
                EVENT_TICK_SKIPPED,
                tick_id=tick_id,
                batch_id=_batchId(batch),
                reason="upstash_unavailable",
                error=str(exc),
            )
            continue

    _emitEvent(EVENT_TICK_END, tick_id=tick_id, batches=len(batches))


# ─── Heartbeat ──────────────────────────────────────────────────────────────
async def _writeHeartbeat(
    *, upstash: UpstashClient, tick_id: str, now: datetime
) -> None:
    """Write the monitor heartbeat. Swallow Upstash failure and log."""
    try:
        await upstash.set(
            UPSTASH_MONITOR_HEARTBEAT_KEY,
            str(int(now.timestamp())),
            ex=MONITOR_HEARTBEAT_TTL_SECONDS,
        )
    except UpstashUnavailable as exc:
        _emitEvent(
            EVENT_TICK_SKIPPED,
            tick_id=tick_id,
            reason="heartbeat_upstash_unavailable",
            error=str(exc),
        )
        return
    _emitEvent(EVENT_HEARTBEAT, tick_id=tick_id)


# ─── Per-batch dispatch ─────────────────────────────────────────────────────
async def _processBatch(
    *,
    deps: MonitorDeps,
    config: MonitorConfig,
    batch: Any,
    tick_id: str,
    now: datetime,
) -> None:
    """Classify and act on one batch. Registered vs rogue split lives here."""
    batch_id = _batchId(batch)
    if batch_id is None:
        return

    registered = await _loadRegistered(upstash=deps.upstash, batch_id=batch_id)
    if registered is not None:
        await _handleRegistered(
            deps=deps,
            config=config,
            batch=batch,
            batch_id=batch_id,
            registered=registered,
            tick_id=tick_id,
        )
        return

    await _handleUnregistered(
        deps=deps,
        config=config,
        batch=batch,
        batch_id=batch_id,
        tick_id=tick_id,
        now=now,
    )


async def _handleRegistered(
    *,
    deps: MonitorDeps,
    config: MonitorConfig,
    batch: Any,
    batch_id: str,
    registered: dict[str, str],
    tick_id: str,
) -> None:
    """Update running cost, kill if runaway, reconcile if ended."""
    app_name = registered.get("app", "unknown")
    approval_id = registered.get("approval_id")
    approved_p95_usd, approved_requests = await _loadApprovalFacts(
        upstash=deps.upstash, approval_id=approval_id
    )
    request_counts = _requestCounts(batch)
    running_usd = computeRunningCost(
        request_counts=request_counts,
        approved_p95_usd=approved_p95_usd,
        approved_requests=approved_requests,
    )
    await _persistRunningCost(
        upstash=deps.upstash,
        batch_id=batch_id,
        running_usd=running_usd,
        ledger=deps.ledger,
        app_name=app_name,
        approval_id=approval_id,
    )
    _emitEvent(
        EVENT_RUNNING_COST,
        tick_id=tick_id,
        batch_id=batch_id,
        running_usd=running_usd,
        p95_usd=approved_p95_usd,
    )

    if isRunaway(
        running_usd=running_usd,
        approved_p95_usd=approved_p95_usd,
        multiplier=config.runaway_multiplier,
    ):
        await _cancelRunaway(
            deps=deps,
            batch_id=batch_id,
            app_name=app_name,
            running_usd=running_usd,
            approved_p95_usd=approved_p95_usd,
            multiplier=config.runaway_multiplier,
            tick_id=tick_id,
        )
        return

    state = _batchState(batch)
    if state in BATCH_ENDED_STATES:
        await _reconcileEnded(
            deps=deps,
            batch=batch,
            batch_id=batch_id,
            app_name=app_name,
            approved_p95_usd=approved_p95_usd,
            tick_id=tick_id,
        )


async def _handleUnregistered(
    *,
    deps: MonitorDeps,
    config: MonitorConfig,
    batch: Any,
    batch_id: str,
    tick_id: str,
    now: datetime,
) -> None:
    """Rogue-batch path: dedup, cancel, log, write ledger row."""
    created_at = _batchCreatedAt(batch)
    if not isGracePassed(
        created_at=created_at,
        now=now,
        grace_seconds=config.rogue_grace_seconds,
    ):
        return

    if await _wasRogueSeen(upstash=deps.upstash, batch_id=batch_id):
        return
    await _markRogueSeen(upstash=deps.upstash, batch_id=batch_id)

    _emitEvent(
        EVENT_ROGUE,
        tick_id=tick_id,
        batch_id=batch_id,
        created_at=created_at.isoformat() if created_at else None,
    )
    await _cancelBatchSafe(anthropic=deps.anthropic, batch_id=batch_id)
    await deps.ledger.write_batch_lifecycle(BatchLifecycleEvent(
        batch_id=batch_id,
        app="unknown",
        status="rogue",
        kill_reason="rogue batch (not registered with governor)",
        ended_at=now,
    ))


# ─── Runaway cancellation ───────────────────────────────────────────────────
async def _cancelRunaway(
    *,
    deps: MonitorDeps,
    batch_id: str,
    app_name: str,
    running_usd: float,
    approved_p95_usd: float,
    multiplier: float,
    tick_id: str,
) -> None:
    """Cancel at Anthropic, remove from in-flight set, write killed row."""
    reason = f"exceeded {multiplier:.1f}x p95"
    _emitEvent(
        EVENT_KILLED,
        tick_id=tick_id,
        batch_id=batch_id,
        running_usd=running_usd,
        p95_usd=approved_p95_usd,
        reason=reason,
    )
    await _cancelBatchSafe(anthropic=deps.anthropic, batch_id=batch_id)
    await _removeInflight(
        upstash=deps.upstash, batch_id=batch_id, app_name=app_name
    )
    await deps.ledger.write_batch_lifecycle(BatchLifecycleEvent(
        batch_id=batch_id,
        app=app_name,
        status="killed",
        running_usd=running_usd,
        kill_reason=reason,
        ended_at=datetime.now(timezone.utc),
    ))


# ─── Ended reconciliation ───────────────────────────────────────────────────
async def _reconcileEnded(
    *,
    deps: MonitorDeps,
    batch: Any,
    batch_id: str,
    app_name: str,
    approved_p95_usd: float,
    tick_id: str,
) -> None:
    """Write variance and flag calibration stale when it exceeds the threshold."""
    actual_usd = _batchActualUsd(batch)
    if actual_usd is None or approved_p95_usd <= 0:
        return
    variance_ratio = computeVariance(
        actual_usd=actual_usd, projected_p95_usd=approved_p95_usd
    )
    variance_pct = round(variance_ratio * 100.0, 2)
    _emitEvent(
        EVENT_VARIANCE,
        tick_id=tick_id,
        batch_id=batch_id,
        variance_pct=variance_pct,
        actual_usd=actual_usd,
        p95_usd=approved_p95_usd,
    )
    await deps.ledger.write_batch_lifecycle(BatchLifecycleEvent(
        batch_id=batch_id,
        app=app_name,
        status="ended",
        actual_usd=actual_usd,
        variance_pct=variance_pct,
        ended_at=datetime.now(timezone.utc),
    ))

    if abs(variance_ratio) <= VARIANCE_STALE_THRESHOLD:
        return

    # BUSINESS RULE: A reconciliation mismatch beyond tolerance is flagged for
    # review in both modes (observation, not gating). The batches row above
    # already carries variance_pct for Supabase; the JSONL flag feeds mining.
    if deps.flags is not None:
        deps.flags.emit(FlagEvent(
            kind="projection_mismatch",
            app=app_name,
            batch_id=batch_id,
            projected_usd=approved_p95_usd,
            actual_usd=actual_usd,
            delta_usd=round(actual_usd - approved_p95_usd, 4),
            ratio=round(variance_ratio, 4),
            tolerance=VARIANCE_STALE_THRESHOLD,
        ))

    operation = _batchOperation(batch)
    model = _batchModel(batch)
    if operation is None or model is None:
        return
    await _flagCalibrationStale(
        upstash=deps.upstash,
        operation=operation,
        model=model,
        tick_id=tick_id,
        variance_pct=variance_pct,
    )


async def _flagCalibrationStale(
    *,
    upstash: UpstashClient,
    operation: str,
    model: str,
    tick_id: str,
    variance_pct: float,
) -> None:
    key = UPSTASH_CALIBRATION_STALE_KEY_FORMAT.format(op=operation, model=model)
    try:
        await upstash.set(key, "1", ex=CALIBRATION_STALE_TTL_SECONDS)
    except UpstashUnavailable as exc:
        _logger.warning(
            "could not flag calibration stale for %s:%s: %s",
            operation, model, exc,
        )
        return
    _emitEvent(
        EVENT_CALIBRATION_STALE,
        tick_id=tick_id,
        operation=operation,
        model=model,
        variance_pct=variance_pct,
    )


# ─── Upstash helpers ────────────────────────────────────────────────────────
async def _loadRegistered(
    *, upstash: UpstashClient, batch_id: str
) -> dict[str, str] | None:
    """Return the ``gov:batch:{id}`` hash or ``None`` when not registered."""
    hash_entry = await upstash.hgetall(f"{UPSTASH_BATCH_KEY_PREFIX}{batch_id}")
    if not hash_entry:
        return None
    return hash_entry


async def _loadApprovalFacts(
    *, upstash: UpstashClient, approval_id: str | None
) -> tuple[float, int]:
    """Read ``(approved_p95_usd, approved_requests)`` from ``gov:approval:{id}``.

    Returns ``(0.0, 0)`` on miss so ``computeRunningCost`` degrades to 0
    rather than producing a nonsense running total. Both fields are
    written by the gate on every APPROVED decision — missing either is
    either a key-expiry race or a direct-wrapper path that has lost the
    original approval detail.
    """
    if not approval_id:
        return 0.0, 0
    entry = await upstash.hgetall(f"{UPSTASH_APPROVAL_KEY_PREFIX}{approval_id}")
    p95_usd = 0.0
    try:
        p95_usd = float(entry.get("p95_usd") or 0.0)
    except (TypeError, ValueError):
        p95_usd = 0.0
    requests = 0
    try:
        requests = int(entry.get("requests") or 0)
    except (TypeError, ValueError):
        requests = 0
    return p95_usd, requests


async def _persistRunningCost(
    *,
    upstash: UpstashClient,
    batch_id: str,
    running_usd: float,
    ledger: SupabaseLedger,
    app_name: str,
    approval_id: str | None,
) -> None:
    """Write ``last_running_usd`` to the Upstash hash + ledger row."""
    try:
        await upstash.hset(
            f"{UPSTASH_BATCH_KEY_PREFIX}{batch_id}",
            {"last_running_usd": f"{running_usd:.4f}"},
        )
    except UpstashUnavailable as exc:
        _logger.warning(
            "could not write last_running_usd for %s: %s", batch_id, exc
        )
    await ledger.write_batch_lifecycle(BatchLifecycleEvent(
        batch_id=batch_id,
        app=app_name,
        approval_id=approval_id,
        status="in_progress",
        running_usd=running_usd,
    ))


async def _removeInflight(
    *, upstash: UpstashClient, batch_id: str, app_name: str
) -> None:
    """Remove a batch from both the per-app and global in-flight sets."""
    try:
        if app_name and app_name != "unknown":
            await upstash.srem(
                f"{UPSTASH_APP_INFLIGHT_KEY_PREFIX}{app_name}:inflight",
                batch_id,
            )
        await upstash.srem(UPSTASH_INFLIGHT_GLOBAL_KEY, batch_id)
    except UpstashUnavailable as exc:
        _logger.warning(
            "could not clean inflight set for %s: %s", batch_id, exc
        )


async def _wasRogueSeen(*, upstash: UpstashClient, batch_id: str) -> bool:
    return await upstash.exists(f"{UPSTASH_ROGUE_SEEN_PREFIX}{batch_id}")


async def _markRogueSeen(*, upstash: UpstashClient, batch_id: str) -> None:
    await upstash.set(
        f"{UPSTASH_ROGUE_SEEN_PREFIX}{batch_id}",
        "1",
        ex=ROGUE_SEEN_TTL_SECONDS,
    )


async def _cancelBatchSafe(
    *, anthropic: AnthropicClient, batch_id: str
) -> None:
    """Cancel via the SDK wrapper, swallowing Anthropic outages.

    We deliberately do NOT raise on cancel failure — the next tick will
    re-observe the batch and retry. What matters is that the log line
    captures the attempt for operator follow-up.

    4xx responses (e.g. "cannot be canceled because it has already
    finished processing", 404 not found) are idempotent no-ops for
    the monitor's purposes: the batch is already in a terminal state,
    so "cancel" has nothing to do. Swallow and log.
    """
    try:
        await anthropic.cancel_batch(batch_id)
    except AnthropicUnavailable as exc:
        _logger.warning(
            "cancel_batch failed for %s: %s (will retry next tick)",
            batch_id, exc,
        )
    except APIStatusError as exc:
        if exc.status_code >= 500:
            raise
        _logger.info(
            "cancel_batch no-op for %s (status=%d): %s",
            batch_id, exc.status_code, exc,
        )


# ─── SDK object accessors ───────────────────────────────────────────────────
# Each helper tolerates both attribute and dict style SDK objects so tests can
# pass lightweight dicts without wrapping them in a mock class.
def _batchId(batch: Any) -> str | None:
    return _coerceStr(_readAttr(batch, "id"))


def _batchState(batch: Any) -> str:
    raw = _readAttr(batch, "processing_status")
    if raw is None:
        raw = _readAttr(batch, "status")
    return str(raw or "").lower()


def _batchCreatedAt(batch: Any) -> datetime | None:
    raw = _readAttr(batch, "created_at")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _requestCounts(batch: Any) -> dict[str, Any]:
    raw = _readAttr(batch, "request_counts") or {}
    if isinstance(raw, dict):
        return raw
    # SDK native objects expose counts as attributes.
    return {
        "processing": _readAttr(raw, "processing") or 0,
        "succeeded": _readAttr(raw, "succeeded") or 0,
        "errored": _readAttr(raw, "errored") or 0,
        "canceled": _readAttr(raw, "canceled") or 0,
        "expired": _readAttr(raw, "expired") or 0,
    }


def _batchActualUsd(batch: Any) -> float | None:
    raw = _readAttr(batch, "actual_usd")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _batchOperation(batch: Any) -> str | None:
    return _coerceStr(_readAttr(batch, "operation"))


def _batchModel(batch: Any) -> str | None:
    return _coerceStr(_readAttr(batch, "model"))


def _readAttr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _coerceStr(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ─── Run loop + signal handling ─────────────────────────────────────────────
async def runForever(
    *,
    deps: MonitorDeps,
    config: MonitorConfig,
    should_stop: Callable[[], bool] | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Main loop: run a tick, sleep, repeat. Exits cleanly when ``should_stop``.

    Arguments:
        deps: injectable external-client container.
        config: runtime knobs.
        should_stop: callable returning True when the loop should exit after
            the current tick. Defaults to "never stop" — the real exit path
            goes through SIGTERM / SIGINT.
        sleeper: overrideable sleep so tests can advance time without
            actually waiting.
    """
    stop_check = should_stop or (lambda: False)
    while not stop_check():
        try:
            await run_tick(deps=deps, config=config)
        except Exception as exc:  # noqa: BLE001 — loop must not die on bug.
            _emitEvent(
                EVENT_TICK_SKIPPED,
                reason="unhandled_exception",
                error=repr(exc),
            )
        if stop_check():
            break
        await sleeper(config.poll_interval_seconds)


async def main() -> None:
    """Real launchd entry point. Builds clients from env and runs forever."""
    config = MonitorConfig.fromEnv()
    deps = MonitorDeps(
        upstash=UpstashClient.fromEnv(),
        ledger=SupabaseLedger.fromEnv(),
        anthropic=AnthropicClient(),
        flags=FlagSink.fromEnv(),
    )
    stop_flag = {"stop": False}

    def _request_stop(*_args: Any) -> None:
        stop_flag["stop"] = True
        _emitEvent(EVENT_TICK_SKIPPED, reason="shutdown_signal")

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGTERM", "SIGINT"):
        try:
            loop.add_signal_handler(getattr(signal, sig_name), _request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows / environments without signal support — skip silently.
            continue

    await runForever(
        deps=deps,
        config=config,
        should_stop=lambda: stop_flag["stop"],
    )
    await deps.upstash.aclose()


if __name__ == "__main__":
    asyncio.run(main())
