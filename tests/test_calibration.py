"""Calibration reservoir + percentile math tests.

We inject a fake ``UpstashClient`` (in-memory, async) so these tests run
offline. The real ``server/upstash.py`` ships an aiohttp-style async client;
this fake matches that shape method-for-method. Methods used by calibration:

    async lpush(key, value) -> int
    async ltrim(key, start, stop) -> bool
    async lrange(key, start, stop) -> list[str]
    async hincrby(key, field, amount) -> int
    async incrbyfloat(key, amount) -> float
"""

from __future__ import annotations

from typing import Iterable

import pytest


# ─── Async in-memory Upstash stand-in ───────────────────────────────────────
class _FakeUpstash:
    """Minimal in-memory Upstash stand-in for tests. Matches the async shape
    of the real ``server.upstash.UpstashClient``.
    """

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, float]] = {}
        self.floats: dict[str, float] = {}
        self.fail_mode: bool = False

    async def lpush(self, key: str, value: str) -> int:
        if self.fail_mode:
            raise RuntimeError("simulated upstash failure")
        self.lists.setdefault(key, []).insert(0, str(value))
        return len(self.lists[key])

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        if self.fail_mode:
            raise RuntimeError("simulated upstash failure")
        lst = self.lists.get(key)
        if lst is None:
            return True
        if stop < 0:
            stop = len(lst) + stop
        self.lists[key] = lst[start : stop + 1]
        return True

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        if self.fail_mode:
            raise RuntimeError("simulated upstash failure")
        lst = self.lists.get(key, [])
        if stop == -1:
            return list(lst[start:])
        return list(lst[start : stop + 1])

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        if self.fail_mode:
            raise RuntimeError("simulated upstash failure")
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = float(bucket.get(field, 0.0)) + amount
        return int(bucket[field])

    async def incrbyfloat(self, key: str, amount: float) -> float:
        if self.fail_mode:
            raise RuntimeError("simulated upstash failure")
        self.floats[key] = float(self.floats.get(key, 0.0)) + amount
        return self.floats[key]


from server.calculator.calibration import (
    CalibrationRecord,
    CalibrationStore,
    MIN_OBSERVATIONS_FOR_PROJECTION,
    RESERVOIR_CAPACITY,
    computePercentile,
)


# ─── Percentile math ─────────────────────────────────────────────────────────
def test_percentileEmptyReturnsZero() -> None:
    assert computePercentile([], 50) == 0


def test_percentileSingleValue() -> None:
    assert computePercentile([42], 50) == 42
    assert computePercentile([42], 99) == 42


def test_percentileNearestRankExact() -> None:
    # Samples 1..100 — nearest-rank says P50 = value at rank 50 = 50.
    samples = list(range(1, 101))
    assert computePercentile(samples, 50) == 50
    assert computePercentile(samples, 95) == 95
    assert computePercentile(samples, 99) == 99
    assert computePercentile(samples, 100) == 100


def test_percentileIsOrderInsensitive() -> None:
    shuffled = [50, 1, 99, 42, 7, 83, 14, 66, 100, 23]
    ordered = sorted(shuffled)
    assert computePercentile(shuffled, 50) == computePercentile(ordered, 50)
    assert computePercentile(shuffled, 95) == computePercentile(ordered, 95)


# ─── CalibrationStore with fixed reservoir ──────────────────────────────────
def _seedReservoir(
    upstash: _FakeUpstash,
    operation: str,
    model: str,
    dimension: str,
    values: Iterable[int],
) -> None:
    key = f"calc:reservoir:{operation}:{model}:{dimension}"
    # Push in order so the list matches whatever order the store returns.
    upstash.lists[key] = [str(v) for v in values]


@pytest.mark.asyncio
async def test_projectOutputFromSeededReservoir() -> None:
    upstash = _FakeUpstash()
    # Hand-seeded reservoir of 100 output-token counts = 1..100.
    _seedReservoir(upstash, "pt_rank", "claude-haiku-4-5-20251001", "out", range(1, 101))

    store = CalibrationStore(upstash=upstash)  # type: ignore[arg-type]
    projection = await store.projectOutputDistribution(
        operation="pt_rank",
        model="claude-haiku-4-5-20251001",
        fallback_p50=500,
        fallback_p95=1500,
        fallback_p99=1800,
    )

    assert projection.source == "reservoir"
    assert projection.n_observations == 100
    assert projection.p50 == 50
    assert projection.p95 == 95
    assert projection.p99 == 99


@pytest.mark.asyncio
async def test_projectOutputFallsBackBelowMinObservations() -> None:
    upstash = _FakeUpstash()
    _seedReservoir(upstash, "small_op", "claude-haiku-4-5-20251001", "out", [100, 200, 300, 400, 500])

    store = CalibrationStore(upstash=upstash)  # type: ignore[arg-type]
    projection = await store.projectOutputDistribution(
        operation="small_op",
        model="claude-haiku-4-5-20251001",
        fallback_p50=500,
        fallback_p95=1500,
        fallback_p99=1800,
    )
    assert projection.source == "fallback"
    assert projection.p50 == 500
    assert projection.p95 == 1500
    assert projection.p99 == 1800
    assert projection.n_observations == 5


@pytest.mark.asyncio
async def test_projectOutputFallsBackOnUpstashFailure() -> None:
    upstash = _FakeUpstash()
    upstash.fail_mode = True
    store = CalibrationStore(upstash=upstash)  # type: ignore[arg-type]
    projection = await store.projectOutputDistribution(
        operation="pt_rank",
        model="claude-sonnet-4-6",
        fallback_p50=1000,
        fallback_p95=2000,
    )
    assert projection.source == "fallback"
    assert projection.p50 == 1000
    assert projection.p95 == 2000
    # With no explicit P99, derived as round(P95 * 1.2) = 2400.
    assert projection.p99 == 2400
    assert projection.n_observations == 0


@pytest.mark.asyncio
async def test_projectOutputReservoirEdgeAtMinObservations() -> None:
    upstash = _FakeUpstash()
    _seedReservoir(
        upstash,
        "edge",
        "claude-haiku-4-5-20251001",
        "out",
        range(1, MIN_OBSERVATIONS_FOR_PROJECTION + 1),
    )
    store = CalibrationStore(upstash=upstash)  # type: ignore[arg-type]
    projection = await store.projectOutputDistribution(
        operation="edge",
        model="claude-haiku-4-5-20251001",
        fallback_p50=999,
        fallback_p95=9999,
    )
    assert projection.source == "reservoir"
    assert projection.n_observations == MIN_OBSERVATIONS_FOR_PROJECTION


@pytest.mark.asyncio
async def test_calibrateWithActualUsagePushesAndTrims() -> None:
    upstash = _FakeUpstash()
    store = CalibrationStore(upstash=upstash)  # type: ignore[arg-type]

    for i in range(RESERVOIR_CAPACITY + 50):
        await store.calibrateWithActualUsage(
            CalibrationRecord(
                operation="pt_rank",
                model="claude-haiku-4-5-20251001",
                actual_input=1000,
                actual_output=i,
                actual_cost_usd=0.001,
            )
        )

    out_key = "calc:reservoir:pt_rank:claude-haiku-4-5-20251001:out"
    in_key = "calc:reservoir:pt_rank:claude-haiku-4-5-20251001:in"
    stats_key = "calc:stats:pt_rank:claude-haiku-4-5-20251001"
    cost_key = "calc:stats:pt_rank:claude-haiku-4-5-20251001:total_cost_usd"

    assert len(upstash.lists[out_key]) == RESERVOIR_CAPACITY
    assert len(upstash.lists[in_key]) == RESERVOIR_CAPACITY
    assert upstash.hashes[stats_key]["n_samples"] == pytest.approx(
        RESERVOIR_CAPACITY + 50
    )
    assert upstash.floats[cost_key] == pytest.approx(
        (RESERVOIR_CAPACITY + 50) * 0.001, rel=1e-4
    )


@pytest.mark.asyncio
async def test_calibrateSwallowsUpstashFailure() -> None:
    upstash = _FakeUpstash()
    upstash.fail_mode = True
    store = CalibrationStore(upstash=upstash)  # type: ignore[arg-type]
    # Must NOT raise — a dropped calibration sample is strictly less costly
    # than breaking the caller.
    await store.calibrateWithActualUsage(
        CalibrationRecord(
            operation="op",
            model="claude-haiku-4-5-20251001",
            actual_input=100,
            actual_output=200,
            actual_cost_usd=0.001,
        )
    )
