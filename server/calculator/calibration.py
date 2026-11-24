"""Per-operation output-token calibration backed by an Upstash reservoir.

Every app that calls Claude operates across a handful of "operations" —
e.g. ``pt_rank``, ``menu_extract``, ``modifier_synth``. Each operation has
a characteristic output-token distribution that shifts as prompts evolve.
Rather than hard-coding per-op guesses, we keep a reservoir of the last
N actual outputs in Upstash and compute P50/P95/P99 on the fly.

Reservoir pattern: ``LPUSH`` new samples, ``LTRIM`` to ``RESERVOIR_CAPACITY``.
``LRANGE 0 -1`` on read. O(N) reads are fine at 512 samples.
"""

from __future__ import annotations

import math
from typing import Final, Literal

from pydantic import BaseModel, Field

from server.upstash import UpstashClient


# ─── Reservoir Constants ─────────────────────────────────────────────────────
# BUSINESS RULE: 512 samples per operation gives 3-sig-fig P99 without
# blowing up Upstash memory at scale (~20 operations
# = ~10k values total). Raising this past 1024 would start to push the
# Upstash "max pipeline size" envelope.
RESERVOIR_CAPACITY: Final[int] = 512

# BUSINESS RULE: Any operation with fewer than this many observations falls
# back to caller-provided defaults. 20 samples is the minimum where P95 is
# meaningful (below that the tail is noise).
MIN_OBSERVATIONS_FOR_PROJECTION: Final[int] = 20

CalibrationSource = Literal["reservoir", "fallback"]


# ─── Output Schemas ──────────────────────────────────────────────────────────
class OutputProjection(BaseModel):
    """P50/P95/P99 output-token projection for an operation."""

    p50: int = Field(..., ge=0)
    p95: int = Field(..., ge=0)
    p99: int = Field(..., ge=0)
    n_observations: int = Field(..., ge=0)
    source: CalibrationSource


class CalibrationRecord(BaseModel):
    """A single calibration observation persisted to Upstash."""

    operation: str
    model: str
    actual_input: int = Field(..., ge=0)
    actual_output: int = Field(..., ge=0)
    actual_cost_usd: float = Field(..., ge=0.0)


# ─── Key Helpers ─────────────────────────────────────────────────────────────
def _reservoirKey(operation: str, model: str, dimension: str) -> str:
    return f"calc:reservoir:{operation}:{model}:{dimension}"


def _statsKey(operation: str, model: str) -> str:
    return f"calc:stats:{operation}:{model}"


def _costCounterKey(operation: str, model: str) -> str:
    # Kept separate from the HINCRBY stats hash because the real UpstashClient
    # only exposes ``incrbyfloat`` on standalone keys, not ``hincrbyfloat``.
    return f"calc:stats:{operation}:{model}:total_cost_usd"


# ─── Projection ──────────────────────────────────────────────────────────────
class CalibrationStore:
    """Upstash-backed calibration reservoir.

    Constructor accepts an ``UpstashClient`` dependency so tests can inject
    an in-memory fake. The client interface is whatever ``server.upstash``
    exposes — Stream B owns that file. All upstream calls are async since
    ``UpstashClient`` uses aiohttp-style async methods.
    """

    def __init__(self, upstash: UpstashClient) -> None:
        self._upstash = upstash

    async def projectOutputDistribution(
        self,
        operation: str,
        model: str,
        fallback_p50: int,
        fallback_p95: int,
        fallback_p99: int | None = None,
    ) -> OutputProjection:
        """Return P50/P95/P99 output tokens for ``(operation, model)``.

        If Upstash is unreachable or the reservoir has fewer than
        ``MIN_OBSERVATIONS_FOR_PROJECTION`` samples, returns the
        caller-supplied fallbacks and ``source="fallback"``. A missing
        ``fallback_p99`` is derived as ``round(fallback_p95 * 1.2)`` — a
        rough but safe buffer above P95.
        """
        effective_p99 = (
            fallback_p99 if fallback_p99 is not None else round(fallback_p95 * 1.2)
        )
        try:
            samples = await self._loadReservoir(operation, model, "out")
        except Exception:  # noqa: BLE001 — Upstash failure must degrade, not raise.
            return OutputProjection(
                p50=fallback_p50,
                p95=fallback_p95,
                p99=effective_p99,
                n_observations=0,
                source="fallback",
            )

        if len(samples) < MIN_OBSERVATIONS_FOR_PROJECTION:
            return OutputProjection(
                p50=fallback_p50,
                p95=fallback_p95,
                p99=effective_p99,
                n_observations=len(samples),
                source="fallback",
            )

        return OutputProjection(
            p50=computePercentile(samples, 50),
            p95=computePercentile(samples, 95),
            p99=computePercentile(samples, 99),
            n_observations=len(samples),
            source="reservoir",
        )

    async def calibrateWithActualUsage(self, record: CalibrationRecord) -> None:
        """Push actual observed usage into the reservoir.

        Writes to these keys:
        - ``calc:reservoir:{op}:{model}:out`` — LPUSH + LTRIM to cap
        - ``calc:reservoir:{op}:{model}:in``  — same
        - ``calc:stats:{op}:{model}``         — HINCRBY ``n_samples``
        - ``calc:stats:{op}:{model}:total_cost_usd`` — INCRBYFLOAT

        Side effects: ~6 Upstash writes. Fails silently on Upstash errors
        because a lost calibration sample is strictly less costly than
        breaking the caller's flow.
        """
        try:
            out_key = _reservoirKey(record.operation, record.model, "out")
            in_key = _reservoirKey(record.operation, record.model, "in")
            stats_key = _statsKey(record.operation, record.model)
            cost_key = _costCounterKey(record.operation, record.model)

            await self._upstash.lpush(out_key, str(record.actual_output))
            await self._upstash.ltrim(out_key, 0, RESERVOIR_CAPACITY - 1)
            await self._upstash.lpush(in_key, str(record.actual_input))
            await self._upstash.ltrim(in_key, 0, RESERVOIR_CAPACITY - 1)
            await self._upstash.hincrby(stats_key, "n_samples", 1)
            await self._upstash.incrbyfloat(cost_key, record.actual_cost_usd)
        except Exception:  # noqa: BLE001 — see docstring.
            return

    # ─── Internal ─────────────────────────────────────────────────────────
    async def _loadReservoir(
        self, operation: str, model: str, dimension: str
    ) -> list[int]:
        raw = await self._upstash.lrange(
            _reservoirKey(operation, model, dimension), 0, -1
        )
        samples: list[int] = []
        for item in raw:
            try:
                samples.append(int(item))
            except (TypeError, ValueError):
                continue
        return samples


# ─── Percentile Math ─────────────────────────────────────────────────────────
def computePercentile(samples: list[int], percentile: int) -> int:
    """Return the ``percentile``-th value (1-100) from an unordered list.

    Uses the "nearest-rank" method: rank = ceil(p/100 * N). Returns 0 for
    an empty list so callers don't have to pre-check. The result is clamped
    into the sample range so rounding never walks off the end.
    """
    if not samples:
        return 0
    if percentile <= 0:
        return min(samples)
    if percentile >= 100:
        return max(samples)
    ordered = sorted(samples)
    rank = max(1, math.ceil(percentile / 100.0 * len(ordered)))
    return int(ordered[rank - 1])
