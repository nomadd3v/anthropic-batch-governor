"""Single-request cost projection.

Given a model, input tokens, and an ``OutputProjection`` from the calibration
reservoir, produce a ``CostProjection`` at P50/P95/P99 with a line-item
breakdown. This is the calculator's answer for "how much will *one* request
cost me?" — batch cache amortisation lives in ``batch.py``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from server.calculator.calibration import OutputProjection
from server.calculator.pricing import getCacheWriteMultiplier, getModelPricing


CacheMode = Literal["none", "5min", "1h"]


# ─── Output Schemas ──────────────────────────────────────────────────────────
class CostBreakdown(BaseModel):
    """Line-items for one percentile of a cost projection."""

    input_cost_usd: float = Field(..., ge=0.0)
    cache_read_cost_usd: float = Field(..., ge=0.0)
    cache_write_cost_usd: float = Field(..., ge=0.0)
    output_cost_usd: float = Field(..., ge=0.0)
    total_usd: float = Field(..., ge=0.0)


class CostProjection(BaseModel):
    """P50/P95/P99 cost projection for a single request."""

    p50_usd: float = Field(..., ge=0.0)
    p95_usd: float = Field(..., ge=0.0)
    p99_usd: float = Field(..., ge=0.0)
    breakdown_p95: CostBreakdown
    model: str
    cache_mode: CacheMode


# ─── Single-Request Projection ───────────────────────────────────────────────
def projectSingleRequestCost(
    model: str,
    input_tokens: int,
    output: OutputProjection,
    cache_mode: CacheMode = "none",
    cached_input_tokens: int = 0,
) -> CostProjection:
    """Project total USD cost for one request at P50/P95/P99.

    ``input_tokens`` is the TOTAL prompt size (shared + unique). When
    ``cache_mode`` is not ``"none"`` the caller passes ``cached_input_tokens``
    for the portion that would be served from cache; the first request in a
    cache window also incurs a write cost, which ``projectBatchCost`` handles
    separately — at the single-request level we assume a cache read (i.e. the
    cache is already warm). Callers who want the "first-write" cost should
    use ``batch.py`` instead.

    Returns a ``CostProjection`` whose ``breakdown_p95`` shows the line-items
    at the P95 output level.
    """
    if cached_input_tokens < 0:
        raise ValueError("cached_input_tokens must be non-negative")
    if cached_input_tokens > input_tokens:
        raise ValueError(
            "cached_input_tokens cannot exceed total input_tokens; got "
            f"{cached_input_tokens} > {input_tokens}"
        )

    pricing = getModelPricing(model)
    uncached_input_tokens = input_tokens - cached_input_tokens

    input_cost = uncached_input_tokens * pricing.input_per_token
    cache_read_cost = (
        cached_input_tokens * pricing.cache_read_per_token
        if cache_mode != "none"
        else 0.0
    )
    # BUSINESS RULE: single-request projection assumes the cache is warm —
    # no write cost. ``batch.py`` amortises the write across N requests; at
    # N=1 the wrapper there adds the write explicitly.
    cache_write_cost = 0.0

    def _outputCostFor(output_tokens: int) -> float:
        return output_tokens * pricing.output_per_token

    breakdown_p95 = CostBreakdown(
        input_cost_usd=input_cost,
        cache_read_cost_usd=cache_read_cost,
        cache_write_cost_usd=cache_write_cost,
        output_cost_usd=_outputCostFor(output.p95),
        total_usd=input_cost
        + cache_read_cost
        + cache_write_cost
        + _outputCostFor(output.p95),
    )

    base_non_output = input_cost + cache_read_cost + cache_write_cost

    return CostProjection(
        p50_usd=base_non_output + _outputCostFor(output.p50),
        p95_usd=base_non_output + _outputCostFor(output.p95),
        p99_usd=base_non_output + _outputCostFor(output.p99),
        breakdown_p95=breakdown_p95,
        model=model,
        cache_mode=cache_mode,
    )


# ─── Helper: first-write cost for N=1 cache priming ──────────────────────────
def computeCacheWriteCostForTokens(
    model: str, tokens: int, cache_mode: CacheMode
) -> float:
    """Return the USD cost to write ``tokens`` to the cache under ``cache_mode``.

    Used by ``batch.py`` and by cache-pre-warm tooling. Returns 0.0 for
    ``cache_mode == "none"``.
    """
    if cache_mode == "none":
        return 0.0
    pricing = getModelPricing(model)
    multiplier = getCacheWriteMultiplier(cache_mode)
    return tokens * pricing.input_per_token * multiplier
