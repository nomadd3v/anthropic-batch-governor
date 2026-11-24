"""Tests for :mod:`server.monitor`.

Covers the full behaviour surface:

* Pure functions — running cost, runaway detection, variance.
* Tick path — heartbeat, runaway cancellation, ended reconciliation,
  rogue detection (with and without grace window).
* Resilience — Anthropic 5xx skips the tick without crashing.

All external services are mocked. The only real dependency is
``asyncio.sleep`` inside ``runForever``, which tests override via the
``sleeper`` parameter.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.anthropic_client import AnthropicUnavailable  # noqa: E402
from server.flags import FlagSink  # noqa: E402
from server.monitor import (  # noqa: E402
    MonitorConfig,
    MonitorDeps,
    UPSTASH_APPROVAL_KEY_PREFIX,
    UPSTASH_BATCH_KEY_PREFIX,
    UPSTASH_MONITOR_HEARTBEAT_KEY,
    UPSTASH_ROGUE_SEEN_PREFIX,
    computeRunningCost,
    computeVariance,
    isGracePassed,
    isRunaway,
    run_tick,
    runForever,
)
from server.upstash import UpstashUnavailable  # noqa: E402


# ─── Fakes ───────────────────────────────────────────────────────────────────
class FakeUpstash:
    """In-memory Upstash covering exactly the subset the monitor touches."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.fail: bool = False
        self.last_ex: dict[str, int | None] = {}

    def _check(self) -> None:
        if self.fail:
            raise UpstashUnavailable("fake upstash down")

    async def get(self, key: str) -> str | None:
        self._check()
        return self.strings.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        self._check()
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        self.last_ex[key] = ex
        return True

    async def delete(self, key: str) -> int:
        self._check()
        removed = 0
        for bucket in (self.strings, self.hashes, self.sets):
            if key in bucket:
                del bucket[key]  # type: ignore[arg-type]
                removed = 1
        return removed

    async def exists(self, key: str) -> bool:
        self._check()
        return (
            key in self.strings
            or key in self.hashes
            or key in self.sets
        )

    async def hset(self, key: str, mapping: dict[str, Any]) -> int:
        self._check()
        bucket = self.hashes.setdefault(key, {})
        for field_name, value in mapping.items():
            bucket[field_name] = str(value)
        return len(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        self._check()
        return dict(self.hashes.get(key, {}))

    async def sadd(self, key: str, value: str) -> int:
        self._check()
        bucket = self.sets.setdefault(key, set())
        if value in bucket:
            return 0
        bucket.add(value)
        return 1

    async def srem(self, key: str, value: str) -> int:
        self._check()
        bucket = self.sets.get(key)
        if bucket and value in bucket:
            bucket.remove(value)
            return 1
        return 0

    async def aclose(self) -> None:
        return None


class FakeLedger:
    """Collects lifecycle events. Ignores decisions — monitor writes none."""

    def __init__(self) -> None:
        self.lifecycles: list[Any] = []

    async def write_batch_lifecycle(self, event: Any) -> None:
        self.lifecycles.append(event)

    async def write_decision(self, record: Any) -> None:
        del record


class FakeAnthropic:
    """Records list/cancel calls. Configurable failure injection."""

    def __init__(self, batches: list[Any] | None = None) -> None:
        self._batches = list(batches or [])
        self.canceled: list[str] = []
        self.list_calls: int = 0
        self.fail_list: bool = False
        self.fail_cancel: bool = False

    async def list_batches(self, limit: int = 100) -> list[Any]:
        del limit
        self.list_calls += 1
        if self.fail_list:
            raise AnthropicUnavailable("simulated list outage")
        return list(self._batches)

    async def cancel_batch(self, batch_id: str) -> Any:
        if self.fail_cancel:
            raise AnthropicUnavailable("simulated cancel outage")
        self.canceled.append(batch_id)
        return {"id": batch_id, "processing_status": "canceled"}


def _makeBatch(
    *,
    batch_id: str,
    processing: int = 0,
    succeeded: int = 0,
    errored: int = 0,
    canceled: int = 0,
    expired: int = 0,
    state: str = "in_progress",
    created_at: datetime | None = None,
    actual_usd: float | None = None,
    operation: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Build a plain-dict batch that matches the SDK shape the monitor reads."""
    return {
        "id": batch_id,
        "processing_status": state,
        "request_counts": {
            "processing": processing,
            "succeeded": succeeded,
            "errored": errored,
            "canceled": canceled,
            "expired": expired,
        },
        "created_at": created_at,
        "actual_usd": actual_usd,
        "operation": operation,
        "model": model,
    }


def _makeDeps(
    *, batches: list[Any] | None = None
) -> tuple[MonitorDeps, FakeUpstash, FakeLedger, FakeAnthropic]:
    upstash = FakeUpstash()
    ledger = FakeLedger()
    anthropic = FakeAnthropic(batches=batches)
    deps = MonitorDeps(
        upstash=upstash,  # type: ignore[arg-type]
        ledger=ledger,  # type: ignore[arg-type]
        anthropic=anthropic,  # type: ignore[arg-type]
    )
    return deps, upstash, ledger, anthropic


def _registerBatch(
    *,
    upstash: FakeUpstash,
    batch_id: str,
    app: str,
    approval_id: str,
    approved_p95_usd: float,
    approved_requests: int = 100,
    model: str = "claude-haiku-4-5-20251001",
) -> None:
    """Seed the Upstash entries the monitor uses to classify a batch."""
    upstash.hashes[f"{UPSTASH_BATCH_KEY_PREFIX}{batch_id}"] = {
        "app": app,
        "approval_id": approval_id,
        "model": model,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    upstash.hashes[f"{UPSTASH_APPROVAL_KEY_PREFIX}{approval_id}"] = {
        "app": app,
        "model": model,
        "p95_usd": f"{approved_p95_usd:.4f}",
        "requests": str(approved_requests),
    }
    upstash.sets.setdefault(f"gov:app:{app}:inflight", set()).add(batch_id)
    upstash.sets.setdefault("gov:inflight", set()).add(batch_id)


# ─── Pure function tests ─────────────────────────────────────────────────────
def test_running_cost_calculation_matches_approved_projection_proportion() -> None:
    """50/100 touched → running cost is exactly half of approved P95."""
    running = computeRunningCost(
        request_counts={
            "processing": 10,
            "succeeded": 30,
            "errored": 10,
            "canceled": 0,
            "expired": 0,
        },
        approved_p95_usd=4.00,
        approved_requests=100,
    )
    # per_item = 4.00 / 100 = 0.04; touched = 50; running = 2.00.
    assert running == pytest.approx(2.00)


def test_running_cost_exceeds_approved_when_touched_exceeds_requests() -> None:
    """Touched > approved_requests is the runaway signal we must preserve."""
    # 200 touched against a batch the governor only approved 100 for.
    running = computeRunningCost(
        request_counts={"succeeded": 200},
        approved_p95_usd=5.00,
        approved_requests=100,
    )
    # per_item = 0.05; running = 200 × 0.05 = 10.00 (2× approved P95).
    assert running == pytest.approx(10.00)


def test_running_cost_returns_zero_when_totals_missing() -> None:
    assert computeRunningCost(
        request_counts={}, approved_p95_usd=3.0, approved_requests=0,
    ) == 0.0
    assert computeRunningCost(
        request_counts={"succeeded": 5}, approved_p95_usd=0.0, approved_requests=5,
    ) == 0.0


def test_is_runaway_matches_threshold() -> None:
    # Running = 2 * p95 (strictly above 1.5x threshold).
    assert isRunaway(running_usd=2.0, approved_p95_usd=1.0, multiplier=1.5)
    # Running = 1.5 * p95 — strictly NOT above 1.5x (boundary is inclusive-below).
    assert not isRunaway(running_usd=1.5, approved_p95_usd=1.0, multiplier=1.5)
    # Zero p95 never trips.
    assert not isRunaway(running_usd=100.0, approved_p95_usd=0.0, multiplier=1.5)


def test_compute_variance_signs_match_expected() -> None:
    assert computeVariance(actual_usd=1.2, projected_p95_usd=1.0) == pytest.approx(0.20)
    assert computeVariance(actual_usd=0.8, projected_p95_usd=1.0) == pytest.approx(-0.20)
    assert computeVariance(actual_usd=5.0, projected_p95_usd=0.0) == 0.0


def test_is_grace_passed() -> None:
    now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
    assert isGracePassed(
        created_at=now - timedelta(seconds=45),
        now=now,
        grace_seconds=30,
    )
    assert not isGracePassed(
        created_at=now - timedelta(seconds=10),
        now=now,
        grace_seconds=30,
    )
    assert not isGracePassed(created_at=None, now=now, grace_seconds=30)


# ─── Tick-level tests ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_heartbeat_written_each_tick() -> None:
    deps, upstash, _, _ = _makeDeps(batches=[])
    config = MonitorConfig(
        poll_interval_seconds=60,
        runaway_multiplier=1.5,
        rogue_grace_seconds=30,
    )
    await run_tick(deps=deps, config=config)
    assert UPSTASH_MONITOR_HEARTBEAT_KEY in upstash.strings
    # TTL is set so /healthz reports degraded when the monitor stops.
    assert upstash.last_ex[UPSTASH_MONITOR_HEARTBEAT_KEY] == 180


@pytest.mark.asyncio
async def test_batch_exceeding_runaway_multiplier_is_canceled() -> None:
    """A registered batch running at >1.5x its approved P95 is canceled.

    Scenario: the governor approved a batch of 100 requests with a $2 P95
    projection. Anthropic is reporting 400 requests already touched — this
    is the 2026-04-15 WAVE_MAX_SIZE-exceeded pattern the monitor must catch.
    running = (2.00 / 100) × 400 = $8.00, which exceeds 1.5 × $2 = $3.
    """
    batch = _makeBatch(
        batch_id="batch_001",
        succeeded=400,
        processing=0,
        state="in_progress",
    )
    deps, upstash, ledger, anthropic = _makeDeps(batches=[batch])
    _registerBatch(
        upstash=upstash,
        batch_id="batch_001",
        app="example-rank-batch",
        approval_id="approval-1",
        approved_p95_usd=2.00,
        approved_requests=100,
    )
    config = MonitorConfig(
        poll_interval_seconds=60,
        runaway_multiplier=1.5,
        rogue_grace_seconds=30,
    )

    await run_tick(deps=deps, config=config)

    assert anthropic.canceled == ["batch_001"]
    killed = [ev for ev in ledger.lifecycles if ev.status == "killed"]
    assert len(killed) == 1
    assert killed[0].batch_id == "batch_001"
    assert killed[0].kill_reason == "exceeded 1.5x p95"
    # Removed from per-app and global in-flight sets.
    assert "batch_001" not in upstash.sets.get(
        "gov:app:example-rank-batch:inflight", set()
    )
    assert "batch_001" not in upstash.sets.get("gov:inflight", set())


@pytest.mark.asyncio
async def test_batch_within_threshold_is_not_canceled() -> None:
    """A registered batch running under the threshold is left alone."""
    batch = _makeBatch(
        batch_id="batch_002",
        succeeded=50,  # 50/100 of $2.00 P95 = $1.00, well under 1.5 × $2 = $3.
        state="in_progress",
    )
    deps, upstash, ledger, anthropic = _makeDeps(batches=[batch])
    _registerBatch(
        upstash=upstash,
        batch_id="batch_002",
        app="example-rank-batch",
        approval_id="approval-2",
        approved_p95_usd=2.00,
        approved_requests=100,
    )
    config = MonitorConfig(
        poll_interval_seconds=60,
        runaway_multiplier=1.5,
        rogue_grace_seconds=30,
    )

    await run_tick(deps=deps, config=config)

    assert anthropic.canceled == []
    killed = [ev for ev in ledger.lifecycles if ev.status == "killed"]
    assert killed == []
    # Running cost persisted to the ledger as an in_progress update.
    in_progress = [ev for ev in ledger.lifecycles if ev.status == "in_progress"]
    assert len(in_progress) == 1
    assert in_progress[0].running_usd == pytest.approx(1.00)


@pytest.mark.asyncio
async def test_unregistered_batch_older_than_grace_is_marked_rogue() -> None:
    """A batch not in ``gov:batch:*`` and past the grace window is killed."""
    old_created = datetime.now(timezone.utc) - timedelta(seconds=120)
    batch = _makeBatch(
        batch_id="batch_rogue",
        processing=5,
        created_at=old_created,
    )
    deps, upstash, ledger, anthropic = _makeDeps(batches=[batch])
    config = MonitorConfig(
        poll_interval_seconds=60,
        runaway_multiplier=1.5,
        rogue_grace_seconds=30,
    )

    await run_tick(deps=deps, config=config)

    assert anthropic.canceled == ["batch_rogue"]
    rogue_events = [ev for ev in ledger.lifecycles if ev.status == "rogue"]
    assert len(rogue_events) == 1
    assert rogue_events[0].app == "unknown"
    assert f"{UPSTASH_ROGUE_SEEN_PREFIX}batch_rogue" in upstash.strings


@pytest.mark.asyncio
async def test_unregistered_batch_within_grace_window_is_ignored() -> None:
    """Grace window prevents false cancels on newly-submitted batches."""
    recent_created = datetime.now(timezone.utc) - timedelta(seconds=5)
    batch = _makeBatch(
        batch_id="batch_new",
        processing=1,
        created_at=recent_created,
    )
    deps, _, ledger, anthropic = _makeDeps(batches=[batch])
    config = MonitorConfig(
        poll_interval_seconds=60,
        runaway_multiplier=1.5,
        rogue_grace_seconds=30,
    )

    await run_tick(deps=deps, config=config)

    assert anthropic.canceled == []
    rogue_events = [ev for ev in ledger.lifecycles if ev.status == "rogue"]
    assert rogue_events == []


@pytest.mark.asyncio
async def test_rogue_batch_deduped_across_ticks() -> None:
    """Second tick against the same rogue batch does not re-cancel."""
    old_created = datetime.now(timezone.utc) - timedelta(seconds=120)
    batch = _makeBatch(
        batch_id="batch_rogue_dup",
        processing=3,
        created_at=old_created,
    )
    deps, _, ledger, anthropic = _makeDeps(batches=[batch, batch])
    config = MonitorConfig(
        poll_interval_seconds=60,
        runaway_multiplier=1.5,
        rogue_grace_seconds=30,
    )

    await run_tick(deps=deps, config=config)
    # Re-run tick against same batch — dedup key should suppress second kill.
    await run_tick(deps=deps, config=config)

    # Cancel attempted once across the two appearances in tick 1 and again
    # across the two appearances in tick 2? Dedup means exactly one cancel.
    assert anthropic.canceled.count("batch_rogue_dup") == 1
    rogue_events = [ev for ev in ledger.lifecycles if ev.status == "rogue"]
    assert len(rogue_events) == 1


@pytest.mark.asyncio
async def test_variance_exceeding_threshold_flags_calibration_stale() -> None:
    """An ended batch with >10% variance flags ``calc:stats:*:stale``."""
    batch = _makeBatch(
        batch_id="batch_var",
        succeeded=100,
        state="ended",
        actual_usd=3.00,  # 50% over approved P95 of $2.00.
        operation="pt_rank",
        model="claude-haiku-4-5-20251001",
    )
    deps, upstash, ledger, _ = _makeDeps(batches=[batch])
    _registerBatch(
        upstash=upstash,
        batch_id="batch_var",
        app="example-rank-batch",
        approval_id="approval-var",
        approved_p95_usd=2.00,
        approved_requests=100,
    )
    # Cap the runaway check so the ended path is reached rather than kill.
    config = MonitorConfig(
        poll_interval_seconds=60,
        runaway_multiplier=10.0,
        rogue_grace_seconds=30,
    )

    await run_tick(deps=deps, config=config)

    stale_key = "calc:stats:pt_rank:claude-haiku-4-5-20251001:stale"
    assert upstash.strings.get(stale_key) == "1"
    assert upstash.last_ex[stale_key] == 24 * 60 * 60
    ended_events = [ev for ev in ledger.lifecycles if ev.status == "ended"]
    assert len(ended_events) == 1
    assert ended_events[0].variance_pct == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_variance_exceeding_threshold_emits_projection_mismatch_flag(
    tmp_path: Path,
) -> None:
    """An ended batch beyond tolerance writes a projection_mismatch flag."""
    batch = _makeBatch(
        batch_id="batch_flag",
        succeeded=100,
        state="ended",
        actual_usd=3.00,  # 50% over approved P95 of $2.00.
        operation="pt_rank",
        model="claude-haiku-4-5-20251001",
    )
    deps, upstash, _, _ = _makeDeps(batches=[batch])
    sink = FlagSink(path=tmp_path / "flags.jsonl")
    deps.flags = sink
    _registerBatch(
        upstash=upstash,
        batch_id="batch_flag",
        app="example-rank-batch",
        approval_id="approval-flag",
        approved_p95_usd=2.00,
        approved_requests=100,
    )
    config = MonitorConfig(
        poll_interval_seconds=60,
        runaway_multiplier=10.0,
        rogue_grace_seconds=30,
    )

    await run_tick(deps=deps, config=config)

    assert sink.counts()["projection_mismatch"] == 1
    line = (tmp_path / "flags.jsonl").read_text(encoding="utf-8").strip()
    import json as _json

    payload = _json.loads(line)
    assert payload["kind"] == "projection_mismatch"
    assert payload["app"] == "example-rank-batch"
    assert payload["ratio"] == pytest.approx(0.50)
    assert payload["actual_usd"] == pytest.approx(3.00)


@pytest.mark.asyncio
async def test_variance_within_threshold_does_not_flag_stale() -> None:
    """Ended batch with <10% variance leaves calibration alone."""
    batch = _makeBatch(
        batch_id="batch_var_ok",
        succeeded=100,
        state="ended",
        actual_usd=2.05,  # 2.5% over approved P95 — within threshold.
        operation="pt_rank",
        model="claude-haiku-4-5-20251001",
    )
    deps, upstash, _, _ = _makeDeps(batches=[batch])
    _registerBatch(
        upstash=upstash,
        batch_id="batch_var_ok",
        app="example-rank-batch",
        approval_id="approval-var-ok",
        approved_p95_usd=2.00,
        approved_requests=100,
    )
    config = MonitorConfig(
        poll_interval_seconds=60,
        runaway_multiplier=10.0,
        rogue_grace_seconds=30,
    )

    await run_tick(deps=deps, config=config)

    stale_key = "calc:stats:pt_rank:claude-haiku-4-5-20251001:stale"
    assert stale_key not in upstash.strings


# ─── Resilience tests ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_tick_continues_on_anthropic_5xx() -> None:
    """An Anthropic outage skips the tick without raising."""
    deps, upstash, ledger, anthropic = _makeDeps(batches=[])
    anthropic.fail_list = True
    config = MonitorConfig(
        poll_interval_seconds=60,
        runaway_multiplier=1.5,
        rogue_grace_seconds=30,
    )

    # Must not raise — the loop keeps running.
    await run_tick(deps=deps, config=config)

    # Heartbeat was still written BEFORE the list attempt.
    assert UPSTASH_MONITOR_HEARTBEAT_KEY in upstash.strings
    # No batches were acted on.
    assert anthropic.canceled == []
    assert ledger.lifecycles == []


@pytest.mark.asyncio
async def test_tick_continues_on_upstash_outage() -> None:
    """Upstash outage does not blow up the loop; heartbeat path no-ops."""
    deps, upstash, _, _ = _makeDeps(batches=[])
    upstash.fail = True
    config = MonitorConfig(
        poll_interval_seconds=60,
        runaway_multiplier=1.5,
        rogue_grace_seconds=30,
    )

    # Anthropic list still runs; no exception escapes.
    await run_tick(deps=deps, config=config)


@pytest.mark.asyncio
async def test_run_forever_stops_when_flag_trips() -> None:
    """``should_stop`` is checked after each tick so SIGTERM drains cleanly."""
    deps, _, _, _ = _makeDeps(batches=[])
    config = MonitorConfig(
        poll_interval_seconds=0,
        runaway_multiplier=1.5,
        rogue_grace_seconds=30,
    )
    tick_count = {"n": 0}

    def _stop() -> bool:
        tick_count["n"] += 1
        # Let the first tick run; stop on the second check.
        return tick_count["n"] > 1

    async def _no_sleep(_: float) -> None:
        return None

    await runForever(
        deps=deps, config=config, should_stop=_stop, sleeper=_no_sleep,
    )
    # 1st check: allow tick; 2nd check (post-sleep): stop.
    assert tick_count["n"] >= 2


# ─── Config tests ────────────────────────────────────────────────────────────
def test_monitor_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONITOR_POLL_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("MONITOR_RUNAWAY_MULTIPLIER", "2.0")
    monkeypatch.setenv("MONITOR_ROGUE_GRACE_SECONDS", "5")
    config = MonitorConfig.fromEnv()
    assert config.poll_interval_seconds == 15
    assert config.runaway_multiplier == 2.0
    assert config.rogue_grace_seconds == 5


def test_monitor_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MONITOR_POLL_INTERVAL_SECONDS",
        "MONITOR_RUNAWAY_MULTIPLIER",
        "MONITOR_ROGUE_GRACE_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    config = MonitorConfig.fromEnv()
    assert config.poll_interval_seconds == 60
    assert config.runaway_multiplier == 1.5
    assert config.rogue_grace_seconds == 30
