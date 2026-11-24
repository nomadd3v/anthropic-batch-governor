"""Cache amortisation and break-even tests.

Pins the math from docs/business-rules/cache-break-even.md in code:

- Break-even N >= 1.28 for 5min (m_w=1.25)
- Break-even N >= 2.11 for 1h   (m_w=2.0)
- Worked Example 1: 200-row Sonnet with 1h cache, P95 ~ $7.52
- Worked Example 3: 2-row Sonnet with 1h cache, cache loses vs no-cache

If Anthropic changes a multiplier, these assertions will fail — that's a
feature, not a bug. Update the doc AND pricing.py together.
"""

from __future__ import annotations

import pytest

from server.calculator.batch import (
    BatchRequestSpec,
    CACHE_1H_BREAK_EVEN_MIN_N,
    computeCacheBreakEvenN,
    projectBatchCostWithCacheAmortization,
)
from server.calculator.calibration import OutputProjection


# ─── Break-even thresholds ───────────────────────────────────────────────────
def test_breakEvenN_5min() -> None:
    # m_w = 1.25 -> (1.25 - 0.1) / 0.9 ~= 1.2777...
    assert computeCacheBreakEvenN("5min") == pytest.approx(1.2777777, abs=1e-4)


def test_breakEvenN_1h() -> None:
    # m_w = 2.0  -> (2.0 - 0.1) / 0.9 ~= 2.1111...
    assert computeCacheBreakEvenN("1h") == pytest.approx(2.1111111, abs=1e-4)


def test_breakEvenN_noneIsZero() -> None:
    assert computeCacheBreakEvenN("none") == 0.0


def test_breakEvenConstantMatchesDoc() -> None:
    # docs/business-rules/cache-break-even.md: "need >= 3 requests in the hour"
    assert CACHE_1H_BREAK_EVEN_MIN_N == 3


# ─── Helper fixtures ─────────────────────────────────────────────────────────
def _fixedOutputProjection(value: int) -> OutputProjection:
    """Zero-variance output projection so we can check cache-input math exactly."""
    return OutputProjection(
        p50=value, p95=value, p99=value, n_observations=1000, source="reservoir"
    )


# ─── Worked Example 1 — 200-row Sonnet w/ 1h cache ───────────────────────────
def test_workedExample1_200rowSonnet1hCache() -> None:
    # N=200, S=5000, U_i=2000, O_i(P95)=2000, p_in=$3/M, p_out=$15/M, m_w=2.0
    N = 200
    S = 5000
    U = 2000
    O_P95 = 2000
    requests = [BatchRequestSpec(unique_input_tokens=U) for _ in range(N)]
    output = _fixedOutputProjection(O_P95)

    projection = projectBatchCostWithCacheAmortization(
        requests=requests,
        shared_system_tokens=S,
        cache_mode="1h",
        model="claude-sonnet-4-6",
        output=output,
    )

    # Per-request P95 from the doc: ~$0.0376 → Batch ~$7.52
    expected_batch_p95 = 200 * 0.0376
    assert projection.total_p95_usd == pytest.approx(expected_batch_p95, abs=0.01)

    # And must be cheaper than no-cache baseline of $10.20
    no_cache_baseline = 200 * (3 * 7000 + 15 * 2000) / 1_000_000
    assert projection.total_p95_usd < no_cache_baseline
    # Roughly 26% savings per the doc (allow a 3% band).
    savings_ratio = 1 - projection.total_p95_usd / no_cache_baseline
    assert savings_ratio == pytest.approx(0.26, abs=0.03)

    # No warning at N=200.
    assert projection.cache_break_even_warning is False


# ─── Worked Example 3 — N=2 Sonnet 1h cache LOSES ────────────────────────────
def test_workedExample3_N2SonnetCacheLoses() -> None:
    N = 2
    S = 5000
    U = 2000
    O_P95 = 2000
    requests = [BatchRequestSpec(unique_input_tokens=U) for _ in range(N)]
    output = _fixedOutputProjection(O_P95)

    projection = projectBatchCostWithCacheAmortization(
        requests=requests,
        shared_system_tokens=S,
        cache_mode="1h",
        model="claude-sonnet-4-6",
        output=output,
    )

    # Doc says per-request P95 ~$0.0518, batch ~$0.1036
    assert projection.total_p95_usd == pytest.approx(0.1036, abs=0.002)

    # And the no-cache baseline is ~$0.102 — cache loses (barely).
    no_cache_baseline = N * (3 * (S + U) + 15 * O_P95) / 1_000_000
    assert projection.total_p95_usd > no_cache_baseline

    # Warning MUST fire at N=2 with 1h cache.
    assert projection.cache_break_even_warning is True


# ─── No cache mode ───────────────────────────────────────────────────────────
def test_noCacheMode_matchesPlainMultiplication() -> None:
    N = 10
    U = 1000
    S = 5000
    requests = [BatchRequestSpec(unique_input_tokens=U) for _ in range(N)]
    output = _fixedOutputProjection(500)

    projection = projectBatchCostWithCacheAmortization(
        requests=requests,
        shared_system_tokens=S,
        cache_mode="none",
        model="claude-haiku-4-5-20251001",
        output=output,
    )

    # With no cache, shared_system_tokens is irrelevant — only unique input +
    # output cost matters at the batch level. (The calculator does NOT add
    # per-request shared-input charges when cache_mode="none" because in
    # that mode the caller presumably isn't sending the shared prefix.)
    expected_per_request = (
        U * (0.80 / 1_000_000) + 500 * (4.00 / 1_000_000)
    )
    assert projection.total_p95_usd == pytest.approx(N * expected_per_request)
    assert projection.cache_break_even_warning is False


# ─── 5min cache never triggers the N<3 warning ──────────────────────────────
def test_5minCacheDoesNotWarnAtSmallN() -> None:
    requests = [BatchRequestSpec(unique_input_tokens=100) for _ in range(2)]
    output = _fixedOutputProjection(200)
    projection = projectBatchCostWithCacheAmortization(
        requests=requests,
        shared_system_tokens=1000,
        cache_mode="5min",
        model="claude-haiku-4-5-20251001",
        output=output,
    )
    assert projection.cache_break_even_warning is False


# ─── 1h cache warning fires exactly for N<3 ─────────────────────────────────
@pytest.mark.parametrize("n,expected_warn", [(1, True), (2, True), (3, False), (10, False)])
def test_1hWarningThreshold(n: int, expected_warn: bool) -> None:
    requests = [BatchRequestSpec(unique_input_tokens=100) for _ in range(n)]
    output = _fixedOutputProjection(200)
    projection = projectBatchCostWithCacheAmortization(
        requests=requests,
        shared_system_tokens=1000,
        cache_mode="1h",
        model="claude-haiku-4-5-20251001",
        output=output,
    )
    assert projection.cache_break_even_warning is expected_warn


# ─── Empty batch returns zero cost ──────────────────────────────────────────
def test_emptyBatchReturnsZero() -> None:
    projection = projectBatchCostWithCacheAmortization(
        requests=[],
        shared_system_tokens=5000,
        cache_mode="1h",
        model="claude-sonnet-4-6",
        output=_fixedOutputProjection(1000),
    )
    assert projection.total_p50_usd == 0.0
    assert projection.total_p95_usd == 0.0
    assert projection.total_p99_usd == 0.0
    assert projection.cache_break_even_warning is False
