"""Batch cost projection with prompt-cache amortisation.

The formula implemented here is the core of the 2026-04-15 post-mortem. For
a batch of ``N`` requests sharing a system prompt of ``S`` tokens with
``cache_control`` and per-request unique input ``U_i``:

    C_in(i) = p_in * (m_w * S) / N              # amortized write
            + r_read * S * (N - 1) / N           # reads on N-1 requests
            + p_in * U_i                         # unique portion full price

See docs/business-rules/cache-break-even.md for the derivation of the
break-even ``N`` (5min: N>=1.28, 1h: N>=2.11) and the worked examples.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from server.calculator.calibration import OutputProjection
from server.calculator.pricing import getCacheWriteMultiplier, getModelPricing


CacheMode = Literal["none", "5min", "1h"]


# ─── Break-even thresholds ───────────────────────────────────────────────────
# BUSINESS RULE: Warn when a 1h cache is used at N < 3. At N=2 the cache
# loses (see cache-break-even.md Example 3). At N=1 it loses by more. The
# gate does NOT deny — some callers pre-warm caches intentionally — but the
# warning is visible and logged.
CACHE_1H_BREAK_EVEN_MIN_N: int = 3


# ─── Input Schemas ───────────────────────────────────────────────────────────
class BatchRequestSpec(BaseModel):
    """Per-request inputs for batch cost projection.

    ``unique_input_tokens`` is the U_i in the formula — the request's own
    prompt portion that is NOT shared across the batch.
    """

    unique_input_tokens: int = Field(..., ge=0)


# ─── Output Schemas ──────────────────────────────────────────────────────────
class BatchRequestCost(BaseModel):
    """Per-request projected cost within a batch."""

    amortized_write_usd: float = Field(..., ge=0.0)
    cache_read_usd: float = Field(..., ge=0.0)
    unique_input_usd: float = Field(..., ge=0.0)
    output_p50_usd: float = Field(..., ge=0.0)
    output_p95_usd: float = Field(..., ge=0.0)
    output_p99_usd: float = Field(..., ge=0.0)
    total_p50_usd: float = Field(..., ge=0.0)
    total_p95_usd: float = Field(..., ge=0.0)
    total_p99_usd: float = Field(..., ge=0.0)


class BatchCostProjection(BaseModel):
    """Batch-level cost projection with cache amortisation applied.

    Also satisfies the ``BatchProjection`` Protocol the gate consumes
    (``p50_usd`` / ``p95_usd`` / ``p99_usd`` aliases for the ``total_*`` fields).
    """

    n_requests: int = Field(..., ge=0)
    shared_system_tokens: int = Field(..., ge=0)
    cache_mode: CacheMode
    model: str
    total_p50_usd: float = Field(..., ge=0.0)
    total_p95_usd: float = Field(..., ge=0.0)
    total_p99_usd: float = Field(..., ge=0.0)
    per_request_p95_usd: float = Field(..., ge=0.0)
    cache_break_even_warning: bool
    cache_break_even_min_n: int
    break_even_detail: str

    # ─── Gate Protocol aliases ───────────────────────────────────────────────
    # BUSINESS RULE: The gate (server/gate.py) reads projections via a
    # ``BatchProjection`` Protocol whose fields are named ``p50_usd`` /
    # ``p95_usd`` / ``p99_usd``. Expose those names here so the same
    # concrete projection instance satisfies the Protocol without an adapter.
    @property
    def p50_usd(self) -> float:
        return self.total_p50_usd

    @property
    def p95_usd(self) -> float:
        return self.total_p95_usd

    @property
    def p99_usd(self) -> float:
        return self.total_p99_usd


# ─── Batch Projection ────────────────────────────────────────────────────────
def projectBatchCostWithCacheAmortization(
    requests: list[BatchRequestSpec],
    shared_system_tokens: int,
    cache_mode: CacheMode,
    model: str,
    output: OutputProjection,
) -> BatchCostProjection:
    # Why this is long: a single batch projection must compute per-request
    # cache amortisation, per-request unique input, per-percentile output
    # cost, AND the break-even warning all in one pass — splitting into
    # helpers obscures the formula. The function stays linear and matches
    # docs/business-rules/cache-break-even.md term-for-term.
    """Project total batch cost using prompt-cache amortisation math.

    Returns a ``BatchCostProjection`` including a ``cache_break_even_warning``
    flag that callers (including the governor gate) can surface to the user.

    Side effects: none.

    Arguments:
        requests: per-request specs (unique input only).
        shared_system_tokens: S — the cacheable portion shared across all N.
        cache_mode: "none" | "5min" | "1h".
        model: Anthropic model id from ``PRICING``.
        output: P50/P95/P99 output-token projection (from calibration).
    """
    if shared_system_tokens < 0:
        raise ValueError("shared_system_tokens must be non-negative")

    pricing = getModelPricing(model)
    n_requests = len(requests)
    write_multiplier = getCacheWriteMultiplier(cache_mode)

    # BUSINESS RULE: 1h cache at small N loses money. Flag N < 3 explicitly.
    # See docs/business-rules/cache-break-even.md (Example 3). The 5min cache
    # breaks even at N >= 1.28, so any N >= 2 already wins — we only warn for
    # the 1h case here.
    cache_break_even_warning = cache_mode == "1h" and n_requests < CACHE_1H_BREAK_EVEN_MIN_N
    break_even_detail = _buildBreakEvenDetail(cache_mode, n_requests)

    if n_requests == 0:
        return BatchCostProjection(
            n_requests=0,
            shared_system_tokens=shared_system_tokens,
            cache_mode=cache_mode,
            model=model,
            total_p50_usd=0.0,
            total_p95_usd=0.0,
            total_p99_usd=0.0,
            per_request_p95_usd=0.0,
            cache_break_even_warning=False,
            cache_break_even_min_n=CACHE_1H_BREAK_EVEN_MIN_N,
            break_even_detail=break_even_detail,
        )

    # Per-request cache terms. When cache_mode == "none" both collapse to 0.
    shared_write_cost_total = (
        shared_system_tokens * pricing.input_per_token * write_multiplier
        if cache_mode != "none"
        else 0.0
    )
    amortized_write_per_request = shared_write_cost_total / n_requests

    # Reads are charged on all but the first request. Divided by N gives the
    # per-request share of the read total — this matches the formula
    # r_read * S * (N - 1) / N written in the business-rules doc.
    if cache_mode != "none" and n_requests > 1:
        cache_read_per_request = (
            pricing.cache_read_per_token
            * shared_system_tokens
            * (n_requests - 1)
            / n_requests
        )
    else:
        cache_read_per_request = 0.0

    total_p50 = 0.0
    total_p95 = 0.0
    total_p99 = 0.0
    per_request_p95_sample = 0.0

    for i, request in enumerate(requests):
        unique_input_cost = request.unique_input_tokens * pricing.input_per_token
        output_p50_cost = output.p50 * pricing.output_per_token
        output_p95_cost = output.p95 * pricing.output_per_token
        output_p99_cost = output.p99 * pricing.output_per_token

        base_input = amortized_write_per_request + cache_read_per_request + unique_input_cost
        per_request_total_p50 = base_input + output_p50_cost
        per_request_total_p95 = base_input + output_p95_cost
        per_request_total_p99 = base_input + output_p99_cost

        total_p50 += per_request_total_p50
        total_p95 += per_request_total_p95
        total_p99 += per_request_total_p99

        if i == 0:
            per_request_p95_sample = per_request_total_p95

    return BatchCostProjection(
        n_requests=n_requests,
        shared_system_tokens=shared_system_tokens,
        cache_mode=cache_mode,
        model=model,
        total_p50_usd=total_p50,
        total_p95_usd=total_p95,
        total_p99_usd=total_p99,
        per_request_p95_usd=per_request_p95_sample,
        cache_break_even_warning=cache_break_even_warning,
        cache_break_even_min_n=CACHE_1H_BREAK_EVEN_MIN_N,
        break_even_detail=break_even_detail,
    )


# ─── Break-Even Helpers ──────────────────────────────────────────────────────
def computeCacheBreakEvenN(cache_mode: CacheMode) -> float:
    """Return the break-even ``N`` for a cache mode.

    Derived in docs/business-rules/cache-break-even.md:
        N >= (m_w - 0.1) / (1 - 0.1)

    For 5min (m_w=1.25) this is ~1.28; for 1h (m_w=2.0) it is ~2.11.
    Returns 0.0 for ``cache_mode == "none"`` since there is no break-even
    in that case.
    """
    if cache_mode == "none":
        return 0.0
    multiplier = getCacheWriteMultiplier(cache_mode)
    # BUSINESS RULE: formula uses r_read = 0.1 * p_in (cache-read discount).
    return (multiplier - 0.1) / (1.0 - 0.1)


def _buildBreakEvenDetail(cache_mode: CacheMode, n: int) -> str:
    if cache_mode == "none":
        return "No cache in use; break-even not applicable."
    threshold = computeCacheBreakEvenN(cache_mode)
    verdict = "above break-even" if n >= threshold else "BELOW break-even"
    return (
        f"cache_mode={cache_mode}, N={n}, break_even_N={threshold:.2f} ({verdict})"
    )
