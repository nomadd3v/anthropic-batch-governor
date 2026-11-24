"""Tests for :mod:`server.gate`.

One test per denial code — covering the happy path plus every code listed in
``docs/business-rules/denial-rules.md``:

* BATCH_SIZE_EXCEEDED
* PER_SUBMIT_CAP
* DAILY_CEILING (includes "current remaining budget" assertion)
* MONTHLY_CEILING
* MODEL_NOT_ALLOWED
* MISSING_CUSTOM_ID
* DUPLICATE_FINGERPRINT
* KILL_SWITCH_ACTIVE
* CACHE_BREAK_EVEN_WARNING (warning, not denial — assertion is on APPROVED
  decision's warnings list)
* STATE_UNAVAILABLE (added for Upstash failure surface)

The gate accepts the ``BatchProjection`` already computed by the calculator,
so we pass a tiny stand-in dataclass; the gate only reads ``p95_usd``,
``p50_usd``, and ``cache_break_even_warning``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# Ensure the repo root is on sys.path so ``import server.gate`` resolves when
# tests are run without a ``pip install -e .`` step.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.flags import FlagEvent  # noqa: E402
from server.gate import (  # noqa: E402
    AppCaps,
    ApprovedDecision,
    DeniedDecision,
    DenialCode,
    GovernorMode,
    check_batch,
)
from server.upstash import UpstashUnavailable  # noqa: E402


# ─── Capturing flag sink ─────────────────────────────────────────────────────
class CapturingFlagSink:
    """Records emitted flags in memory so tests can assert on them."""

    def __init__(self) -> None:
        self.events: list[FlagEvent] = []

    def emit(self, event: FlagEvent) -> None:
        self.events.append(event)


# ─── Fakes ───────────────────────────────────────────────────────────────────
@dataclass
class FakeBatchProjection:
    """Stand-in for Stream A's BatchProjection — only the attributes the gate
    reads are populated."""

    p50_usd: float = 0.10
    p95_usd: float = 0.20
    p99_usd: float = 0.25
    cache_break_even_warning: bool = False
    per_request: list[Any] = field(default_factory=list)


@dataclass
class _Command:
    """Decoded pipeline command so assertions can read field names."""

    name: str
    args: tuple[Any, ...]


class FakeUpstash:
    """In-memory stand-in for :class:`UpstashClient`.

    We only implement the methods the gate actually calls: ``get`` and
    ``pipeline``. Pipelines are decoded into structured commands so tests can
    assert which keys the gate wrote to.
    """

    def __init__(
        self,
        *,
        store: dict[str, Any] | None = None,
        fail: bool = False,
    ) -> None:
        self.store: dict[str, Any] = dict(store or {})
        self.pipeline_calls: list[list[_Command]] = []
        self.fail = fail

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise UpstashUnavailable("fake upstash down")
        value = self.store.get(key)
        return None if value is None else str(value)

    async def pipeline(self, commands: list[list[Any]]) -> list[Any]:
        if self.fail:
            raise UpstashUnavailable("fake upstash down")
        decoded = [_Command(str(c[0]).upper(), tuple(c[1:])) for c in commands]
        self.pipeline_calls.append(decoded)
        results: list[Any] = []
        for cmd in decoded:
            if cmd.name == "HSET":
                results.append(1)
            elif cmd.name == "EXPIRE":
                results.append(1)
            elif cmd.name == "INCRBYFLOAT":
                key = str(cmd.args[0])
                amount = float(cmd.args[1])
                current = float(self.store.get(key, 0.0))
                self.store[key] = current + amount
                results.append(current + amount)
            elif cmd.name == "SET":
                self.store[str(cmd.args[0])] = cmd.args[1]
                results.append("OK")
            else:
                results.append(None)
        return results


class FakeLedger:
    """Stand-in for :class:`SupabaseLedger` that captures every write."""

    def __init__(self) -> None:
        self.decisions: list[Any] = []
        self.lifecycles: list[Any] = []

    async def write_decision(self, decision: Any) -> None:
        self.decisions.append(decision)

    async def write_batch_lifecycle(self, event: Any) -> None:
        self.lifecycles.append(event)


# ─── Helpers ─────────────────────────────────────────────────────────────────
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def _makeCaps(
    *,
    app: str = "example-rank-batch",
    max_batch_size: int = 40,
    per_submit_cap_usd: float = 5.00,
    daily_ceiling_usd: float = 25.00,
    monthly_ceiling_usd: float = 300.00,
    allowed_models: tuple[str, ...] = (DEFAULT_MODEL,),
) -> AppCaps:
    return AppCaps(
        app=app,
        max_batch_size=max_batch_size,
        per_submit_cap_usd=per_submit_cap_usd,
        daily_ceiling_usd=daily_ceiling_usd,
        monthly_ceiling_usd=monthly_ceiling_usd,
        allowed_models=allowed_models,
        allow_daemon=False,
        allow_large_batch=False,
    )


def _makeRequests(n: int, *, with_custom_id: bool = True) -> list[dict[str, Any]]:
    """Build N dummy batch requests, optionally with unique custom_ids."""
    requests: list[dict[str, Any]] = []
    for index in range(n):
        request: dict[str, Any] = {
            "params": {
                "model": DEFAULT_MODEL,
                "max_tokens": 100,
                "messages": [{"role": "user", "content": f"hi-{index}"}],
            },
        }
        if with_custom_id:
            request["custom_id"] = f"req-{index}"
        requests.append(request)
    return requests


NOW = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)


async def _runCheck(
    *,
    caps: AppCaps | None = None,
    requests: list[dict[str, Any]] | None = None,
    model: str = DEFAULT_MODEL,
    projection: FakeBatchProjection | None = None,
    fingerprint: str = "fp-default",
    upstash: FakeUpstash | None = None,
    ledger: FakeLedger | None = None,
    cache_mode: str = "none",
    mode: GovernorMode = GovernorMode.ENFORCING,
    flag_sink: CapturingFlagSink | None = None,
    now: datetime = NOW,
) -> Any:
    return await check_batch(
        app=(caps or _makeCaps()).app,
        requests=requests or _makeRequests(5),
        model=model,
        projection=projection or FakeBatchProjection(),  # type: ignore[arg-type]
        fingerprint=fingerprint,
        caps=caps or _makeCaps(),
        upstash=upstash or FakeUpstash(),  # type: ignore[arg-type]
        ledger=ledger or FakeLedger(),  # type: ignore[arg-type]
        cache_mode=cache_mode,
        mode=mode,
        flag_sink=flag_sink,  # type: ignore[arg-type]
        now=now,
    )


# ─── Happy path ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_approved_decision_on_clean_submit() -> None:
    upstash = FakeUpstash()
    ledger = FakeLedger()
    decision = await _runCheck(upstash=upstash, ledger=ledger)

    assert isinstance(decision, ApprovedDecision)
    assert decision.decision == "APPROVED"
    assert decision.p95_usd == pytest.approx(0.20)
    assert decision.warnings == []

    # Upstash must have received exactly one pipeline with the seven writes
    # documented in the gate: HSET approval, EXPIRE approval, INCRBYFLOAT
    # daily, EXPIRE daily, INCRBYFLOAT monthly, EXPIRE monthly, SET fingerprint.
    assert len(upstash.pipeline_calls) == 1
    names = [cmd.name for cmd in upstash.pipeline_calls[0]]
    assert names == [
        "HSET", "EXPIRE", "INCRBYFLOAT", "EXPIRE",
        "INCRBYFLOAT", "EXPIRE", "SET",
    ]

    # Audit row is APPROVED with the right counters.
    assert len(ledger.decisions) == 1
    assert ledger.decisions[0].decision == "APPROVED"
    assert ledger.decisions[0].approval_id == decision.approval_id
    assert ledger.decisions[0].projected_p95_usd == pytest.approx(0.20)


# ─── BATCH_SIZE_EXCEEDED ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_denies_when_batch_size_exceeds_cap() -> None:
    caps = _makeCaps(max_batch_size=40)
    requests = _makeRequests(200)

    decision = await _runCheck(caps=caps, requests=requests)

    assert isinstance(decision, DeniedDecision)
    codes = [reason.code for reason in decision.reasons]
    assert DenialCode.BATCH_SIZE_EXCEEDED in codes

    reason = next(
        r for r in decision.reasons
        if r.code == DenialCode.BATCH_SIZE_EXCEEDED
    )
    # Fix must include the configured max per the task spec.
    assert "40" in reason.fix


@pytest.mark.asyncio
async def test_denies_when_app_has_batches_disabled() -> None:
    caps = _makeCaps(app="example-extractor", max_batch_size=0)
    decision = await _runCheck(caps=caps, requests=_makeRequests(1))

    assert isinstance(decision, DeniedDecision)
    assert decision.reasons[0].code == DenialCode.BATCH_SIZE_EXCEEDED
    assert "example-extractor" in decision.reasons[0].detail


# ─── PER_SUBMIT_CAP ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_denies_when_per_submit_cap_exceeded() -> None:
    caps = _makeCaps(per_submit_cap_usd=5.00)
    projection = FakeBatchProjection(p50_usd=20.0, p95_usd=47.20)

    decision = await _runCheck(caps=caps, projection=projection)

    assert isinstance(decision, DeniedDecision)
    codes = [reason.code for reason in decision.reasons]
    assert DenialCode.PER_SUBMIT_CAP in codes
    reason = next(
        r for r in decision.reasons if r.code == DenialCode.PER_SUBMIT_CAP
    )
    assert "47.20" in reason.detail
    assert "5.00" in reason.detail


# ─── DAILY_CEILING ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_denies_when_daily_ceiling_would_exceed() -> None:
    caps = _makeCaps(daily_ceiling_usd=25.00)
    # Already reserved $20 today; a $10 projection would push us over.
    upstash = FakeUpstash(store={
        "gov:app:example-rank-batch:daily:20260417": 20.00,
    })
    projection = FakeBatchProjection(p50_usd=5.0, p95_usd=10.00)

    decision = await _runCheck(
        caps=caps, upstash=upstash, projection=projection
    )

    assert isinstance(decision, DeniedDecision)
    reason = next(
        r for r in decision.reasons if r.code == DenialCode.DAILY_CEILING
    )
    # Fix must include the current remaining budget (task spec requirement).
    assert "$5.00" in reason.detail
    assert "$5.00" in reason.fix or "remaining" in reason.detail.lower()


# ─── MONTHLY_CEILING ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_denies_when_monthly_ceiling_would_exceed() -> None:
    caps = _makeCaps(monthly_ceiling_usd=300.00, daily_ceiling_usd=1_000.0)
    upstash = FakeUpstash(store={
        "gov:app:example-rank-batch:monthly:202604": 295.00,
    })
    projection = FakeBatchProjection(p95_usd=10.00)

    decision = await _runCheck(
        caps=caps, upstash=upstash, projection=projection
    )

    assert isinstance(decision, DeniedDecision)
    codes = [reason.code for reason in decision.reasons]
    assert DenialCode.MONTHLY_CEILING in codes


# ─── MODEL_NOT_ALLOWED ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_denies_when_model_not_in_allowlist() -> None:
    caps = _makeCaps(allowed_models=("claude-haiku-4-5-20251001",))
    decision = await _runCheck(caps=caps, model="claude-sonnet-4-6")

    assert isinstance(decision, DeniedDecision)
    reason = next(
        r for r in decision.reasons if r.code == DenialCode.MODEL_NOT_ALLOWED
    )
    assert "claude-sonnet-4-6" in reason.detail
    assert "claude-haiku-4-5-20251001" in reason.fix


# ─── MISSING_CUSTOM_ID ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_denies_when_request_missing_custom_id() -> None:
    requests = _makeRequests(5, with_custom_id=True)
    requests[2].pop("custom_id")

    decision = await _runCheck(requests=requests)

    assert isinstance(decision, DeniedDecision)
    reason = next(
        r for r in decision.reasons if r.code == DenialCode.MISSING_CUSTOM_ID
    )
    assert "[2]" in reason.detail


@pytest.mark.asyncio
async def test_denies_when_custom_id_duplicated() -> None:
    requests = _makeRequests(3)
    requests[2]["custom_id"] = requests[0]["custom_id"]
    decision = await _runCheck(requests=requests)

    assert isinstance(decision, DeniedDecision)
    assert decision.reasons[0].code == DenialCode.MISSING_CUSTOM_ID
    assert "duplicate" in decision.reasons[0].detail.lower()


# ─── DUPLICATE_FINGERPRINT ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_denies_duplicate_fingerprint() -> None:
    upstash = FakeUpstash(store={
        "gov:fingerprint:fp-123": "batch_abc123",
    })
    decision = await _runCheck(upstash=upstash, fingerprint="fp-123")

    assert isinstance(decision, DeniedDecision)
    reason = next(
        r for r in decision.reasons
        if r.code == DenialCode.DUPLICATE_FINGERPRINT
    )
    assert "batch_abc123" in reason.detail


# ─── KILL_SWITCH_ACTIVE ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_denies_when_kill_switch_active() -> None:
    upstash = FakeUpstash(store={
        "gov:kill-switch": '{"reason": "investigating rogue deploy_agent"}',
    })
    decision = await _runCheck(upstash=upstash)

    assert isinstance(decision, DeniedDecision)
    # Kill-switch short-circuits — it is the only reason returned.
    assert len(decision.reasons) == 1
    reason = decision.reasons[0]
    assert reason.code == DenialCode.KILL_SWITCH_ACTIVE
    assert "investigating rogue deploy_agent" in reason.detail


# ─── CACHE_BREAK_EVEN_WARNING ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_attaches_cache_break_even_warning_on_small_1h_batch() -> None:
    decision = await _runCheck(
        requests=_makeRequests(2),
        cache_mode="1h",
    )

    # This is a warning, not a denial — APPROVED stays APPROVED.
    assert isinstance(decision, ApprovedDecision)
    assert len(decision.warnings) == 1
    assert decision.warnings[0].code.value == "CACHE_BREAK_EVEN_WARNING"


@pytest.mark.asyncio
async def test_no_cache_warning_for_5min_mode_or_large_1h_batch() -> None:
    approved_large = await _runCheck(
        requests=_makeRequests(10),
        cache_mode="1h",
    )
    approved_5min = await _runCheck(
        requests=_makeRequests(2),
        cache_mode="5min",
    )
    assert isinstance(approved_large, ApprovedDecision)
    assert approved_large.warnings == []
    assert isinstance(approved_5min, ApprovedDecision)
    assert approved_5min.warnings == []


# ─── STATE_UNAVAILABLE ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_denies_with_state_unavailable_when_upstash_is_down() -> None:
    upstash = FakeUpstash(fail=True)
    ledger = FakeLedger()

    decision = await _runCheck(upstash=upstash, ledger=ledger)

    assert isinstance(decision, DeniedDecision)
    assert decision.reasons[0].code == DenialCode.STATE_UNAVAILABLE
    # A DENIED audit row still lands so operators can see the outage later.
    assert ledger.decisions[0].decision == "DENIED"


# ─── Permissive mode ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_permissive_approves_and_flags_a_per_submit_would_deny() -> None:
    """A batch that enforcing would DENY on per-submit cap is APPROVED + flagged."""
    caps = _makeCaps(per_submit_cap_usd=5.00)
    projection = FakeBatchProjection(p50_usd=20.0, p95_usd=47.20)
    upstash = FakeUpstash()
    ledger = FakeLedger()
    sink = CapturingFlagSink()

    decision = await _runCheck(
        caps=caps,
        projection=projection,
        upstash=upstash,
        ledger=ledger,
        mode=GovernorMode.PERMISSIVE,
        flag_sink=sink,
    )

    # Auditor mode: APPROVED, not DENIED — budget is still reserved.
    assert isinstance(decision, ApprovedDecision)
    assert len(upstash.pipeline_calls) == 1

    # One would_deny flag captured, carrying the overage + would-be reason.
    assert len(sink.events) == 1
    flag = sink.events[0]
    assert flag.kind == "would_deny"
    assert flag.app == caps.app
    assert flag.projected_p95_usd == pytest.approx(47.20)
    # Overage = projected P95 − per-submit cap = 47.20 − 5.00.
    assert flag.overage_usd == pytest.approx(42.20)
    assert flag.would_deny_reasons is not None
    assert flag.would_deny_reasons[0]["code"] == "PER_SUBMIT_CAP"
    assert flag.budget_state is not None

    # The APPROVED audit row records the would-be denial reasons in Supabase.
    assert len(ledger.decisions) == 1
    row = ledger.decisions[0]
    assert row.decision == "APPROVED"
    assert row.reasons is not None
    assert row.reasons[0]["code"] == "PER_SUBMIT_CAP"


@pytest.mark.asyncio
async def test_permissive_flags_daily_ceiling_with_correct_overage() -> None:
    caps = _makeCaps(daily_ceiling_usd=25.00)
    # $20 already reserved today; a $10 projection would push $5 over the $5 left.
    upstash = FakeUpstash(store={
        "gov:app:example-rank-batch:daily:20260417": 20.00,
    })
    projection = FakeBatchProjection(p50_usd=5.0, p95_usd=10.00)
    sink = CapturingFlagSink()

    decision = await _runCheck(
        caps=caps,
        upstash=upstash,
        projection=projection,
        mode=GovernorMode.PERMISSIVE,
        flag_sink=sink,
    )

    assert isinstance(decision, ApprovedDecision)
    flag = sink.events[0]
    assert flag.kind == "would_deny"
    # Overage = projected P95 − remaining = 10.00 − (25.00 − 20.00) = 5.00.
    assert flag.overage_usd == pytest.approx(5.00)
    assert flag.budget_state is not None
    assert flag.budget_state["daily"]["remaining_usd"] == pytest.approx(5.00)


@pytest.mark.asyncio
async def test_permissive_still_denies_kill_switch() -> None:
    """The kill switch is an emergency halt — never auto-approved, even permissive."""
    upstash = FakeUpstash(store={
        "gov:kill-switch": '{"reason": "incident"}',
    })
    sink = CapturingFlagSink()

    decision = await _runCheck(
        upstash=upstash, mode=GovernorMode.PERMISSIVE, flag_sink=sink
    )

    assert isinstance(decision, DeniedDecision)
    assert decision.reasons[0].code == DenialCode.KILL_SWITCH_ACTIVE
    # No flag: the request was genuinely denied, not permissively approved.
    assert sink.events == []


@pytest.mark.asyncio
async def test_permissive_clean_submit_is_not_flagged() -> None:
    """A request that passes every rule approves normally with no flag."""
    sink = CapturingFlagSink()
    decision = await _runCheck(mode=GovernorMode.PERMISSIVE, flag_sink=sink)
    assert isinstance(decision, ApprovedDecision)
    assert sink.events == []


@pytest.mark.asyncio
async def test_enforcing_mode_never_flags_even_with_a_sink() -> None:
    """Enforcing behavior is unchanged: a would-deny DENIES and emits no flag."""
    caps = _makeCaps(per_submit_cap_usd=5.00)
    projection = FakeBatchProjection(p95_usd=47.20)
    sink = CapturingFlagSink()

    decision = await _runCheck(
        caps=caps,
        projection=projection,
        mode=GovernorMode.ENFORCING,
        flag_sink=sink,
    )

    assert isinstance(decision, DeniedDecision)
    assert sink.events == []
