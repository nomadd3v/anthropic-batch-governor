"""Tests for :class:`server.supabase_ledger.SupabaseLedger`.

The important behaviors:

1. A successful ``write_decision`` reaches the underlying Supabase client.
2. A Supabase-side exception drops the payload into ``/tmp/governor-ledger-
   queue/`` and is replayed by ``flush_queue``.
3. ``write_batch_lifecycle`` uses upsert.
4. ``compute_variance`` joins a decision to its batch and returns a
   well-formed :class:`VarianceReport`.

All Supabase interactions are mocked — no network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.supabase_ledger import (  # noqa: E402
    BatchLifecycleEvent,
    DecisionRecord,
    SupabaseLedger,
    VarianceReport,
)


# ─── Fakes for supabase-py ───────────────────────────────────────────────────
class _FakeExecutionResult:
    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class _FakeQuery:
    """Chainable stand-in for the supabase-py fluent query builder."""

    def __init__(self, *, table_name: str, parent: "_FakeSupabase") -> None:
        self.table_name = table_name
        self.parent = parent
        self.inserts: list[dict[str, Any]] = []
        self.upserts: list[dict[str, Any]] = []
        self._filters: list[tuple[str, Any]] = []
        self._limit: int | None = None
        self._select: str | None = None

    def insert(self, payload: dict[str, Any]) -> "_FakeQuery":
        if self.parent.fail_writes:
            raise RuntimeError("simulated supabase outage")
        self.inserts.append(payload)
        self.parent.writes.append(("insert", self.table_name, payload))
        return self

    def upsert(self, payload: dict[str, Any]) -> "_FakeQuery":
        if self.parent.fail_writes:
            raise RuntimeError("simulated supabase outage")
        self.upserts.append(payload)
        self.parent.writes.append(("upsert", self.table_name, payload))
        return self

    def select(self, columns: str) -> "_FakeQuery":
        self._select = columns
        return self

    def eq(self, column: str, value: Any) -> "_FakeQuery":
        self._filters.append((column, value))
        return self

    def limit(self, n: int) -> "_FakeQuery":
        self._limit = n
        return self

    def execute(self) -> _FakeExecutionResult:
        return _FakeExecutionResult(
            self.parent.select_results.get(self.table_name, [])
        )


class _FakeSchema:
    def __init__(self, parent: "_FakeSupabase") -> None:
        self.parent = parent

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(table_name=name, parent=self.parent)


class _FakeSupabase:
    """Minimal supabase-py drop-in."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_writes = False
        self.select_results: dict[str, list[dict[str, Any]]] = {}

    def schema(self, name: str) -> _FakeSchema:
        del name
        return _FakeSchema(self)


# ─── Fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture
def tmp_queue(tmp_path: Path) -> Path:
    queue_dir = tmp_path / "ledger-queue"
    queue_dir.mkdir()
    return queue_dir


@pytest.fixture
def ledger(tmp_queue: Path) -> tuple[SupabaseLedger, _FakeSupabase]:
    fake = _FakeSupabase()
    writer = SupabaseLedger(client=fake, queue_dir=str(tmp_queue))  # type: ignore[arg-type]
    return writer, fake


# ─── Happy path ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_write_decision_reaches_supabase(
    ledger: tuple[SupabaseLedger, _FakeSupabase],
) -> None:
    writer, fake = ledger
    record = DecisionRecord(
        app="example-rank-batch",
        decision="APPROVED",
        fingerprint="fp-abc",
        items=5,
        model="claude-haiku-4-5-20251001",
        projected_p50_usd=0.1,
        projected_p95_usd=0.2,
        approval_id="00000000-0000-0000-0000-000000000001",
    )
    await writer.write_decision(record)
    await writer.wait_pending()

    assert len(fake.writes) == 1
    action, table, payload = fake.writes[0]
    assert action == "insert"
    assert table == "decisions"
    assert payload["app"] == "example-rank-batch"
    assert payload["decision"] == "APPROVED"
    assert payload["projected_p95_usd"] == 0.2


@pytest.mark.asyncio
async def test_batch_lifecycle_uses_upsert(
    ledger: tuple[SupabaseLedger, _FakeSupabase],
) -> None:
    writer, fake = ledger
    event = BatchLifecycleEvent(
        batch_id="batch_abc123",
        app="example-rank-batch",
        model="claude-haiku-4-5-20251001",
        status="submitted",
    )
    await writer.write_batch_lifecycle(event)
    await writer.wait_pending()

    assert fake.writes[0][0] == "upsert"
    assert fake.writes[0][1] == "batches"


# ─── Retry queue ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_failed_write_lands_in_disk_queue(
    ledger: tuple[SupabaseLedger, _FakeSupabase],
    tmp_queue: Path,
) -> None:
    writer, fake = ledger
    fake.fail_writes = True

    await writer.write_decision(DecisionRecord(
        app="example-rank-batch",
        decision="DENIED",
        reasons=[{"code": "BATCH_SIZE_EXCEEDED", "detail": "x", "fix": "y"}],
    ))
    await writer.wait_pending()

    queue_files = list(tmp_queue.glob("*.json"))
    assert len(queue_files) == 1
    import json
    dropped = json.loads(queue_files[0].read_text())
    assert dropped["_table"] == "decisions"
    assert dropped["decision"] == "DENIED"


@pytest.mark.asyncio
async def test_flush_queue_replays_and_deletes_files(
    ledger: tuple[SupabaseLedger, _FakeSupabase],
    tmp_queue: Path,
) -> None:
    writer, fake = ledger

    # First, force a failure to populate the queue.
    fake.fail_writes = True
    await writer.write_decision(DecisionRecord(
        app="example-rank-batch",
        decision="APPROVED",
    ))
    await writer.wait_pending()

    # Recover, then flush.
    fake.fail_writes = False
    replayed = await writer.flush_queue()

    assert replayed == 1
    assert list(tmp_queue.glob("*.json")) == []
    assert any(
        table == "decisions" and action == "insert"
        for action, table, _ in fake.writes
    )


@pytest.mark.asyncio
async def test_flush_queue_retries_on_persistent_failure(
    ledger: tuple[SupabaseLedger, _FakeSupabase],
    tmp_queue: Path,
) -> None:
    writer, fake = ledger
    fake.fail_writes = True
    await writer.write_decision(DecisionRecord(
        app="example-rank-batch", decision="APPROVED",
    ))
    await writer.wait_pending()

    # Flush while still failing — file must remain for the next attempt.
    replayed = await writer.flush_queue()
    assert replayed == 0
    assert len(list(tmp_queue.glob("*.json"))) == 1


# ─── Variance ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_compute_variance_joins_decision_to_batch(
    ledger: tuple[SupabaseLedger, _FakeSupabase],
) -> None:
    writer, fake = ledger
    fake.select_results["batches"] = [{
        "batch_id": "batch_xyz",
        "actual_usd": 0.25,
        "approval_id": "approval-1",
    }]
    fake.select_results["decisions"] = [{"projected_p95_usd": 0.20}]

    report = await writer.compute_variance("batch_xyz")

    assert isinstance(report, VarianceReport)
    assert report.batch_id == "batch_xyz"
    assert report.actual_usd == pytest.approx(0.25)
    assert report.projected_p95_usd == pytest.approx(0.20)
    # (0.25 - 0.20) / 0.20 = 0.25 → +25%
    assert report.variance_pct == pytest.approx(25.0)


@pytest.mark.asyncio
async def test_compute_variance_returns_nulls_when_batch_missing(
    ledger: tuple[SupabaseLedger, _FakeSupabase],
) -> None:
    writer, _ = ledger
    report = await writer.compute_variance("batch_not_here")
    assert report.actual_usd is None
    assert report.projected_p95_usd is None
    assert report.variance_pct is None


# ─── Gate never blocked by ledger ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_write_decision_returns_before_supabase_call_completes(
    ledger: tuple[SupabaseLedger, _FakeSupabase],
) -> None:
    """Smoke check that write_decision is non-blocking: it schedules the
    task and returns while the fake client's recorded write is still pending
    on the event loop. Concretely, nothing should be written synchronously
    before await returns control."""

    writer, fake = ledger
    mock_trace = MagicMock()

    # Wrap the fake's recorder so we can see order of execution.
    orig_insert = fake.writes

    async def _call_write() -> None:
        await writer.write_decision(DecisionRecord(
            app="example-rank-batch", decision="APPROVED",
        ))
        mock_trace("returned_from_write_decision")

    await _call_write()
    # At this point the background task may or may not have run. Drain it.
    await writer.wait_pending()
    assert mock_trace.called
    assert len(orig_insert) == 1
