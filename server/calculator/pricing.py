"""Model pricing constants and per-model rate lookup.

Source of truth for all Anthropic pricing used by the calculator. Every app in
callers read its rates from here (or via the HTTP ``/calc/pricing``
endpoint that re-exports these) so we can never drift apart again — that drift
was one of the 2026-04-15 incident root causes.

Units: all prices are USD per single token. Divide the public "$/M" numbers
by 1_000_000 to land here.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, Field


# ─── Cache Amortization Constants ────────────────────────────────────────────
# BUSINESS RULE: Anthropic charges 1.25x the base input rate for a 5-minute
# cache write and 2.0x the base rate for a 1-hour cache write. Cache reads
# are billed at 10% of the base input rate. These multipliers drive every
# break-even calculation in ``batch.py``. See
# docs/business-rules/cache-break-even.md for the full derivation.
CACHE_5MIN_WRITE_MULTIPLIER: Final[float] = 1.25
CACHE_1H_WRITE_MULTIPLIER: Final[float] = 2.0
CACHE_READ_DISCOUNT_RATIO: Final[float] = 0.1

# USD → per-token divisor. All public pricing is quoted "per million tokens".
TOKENS_PER_MILLION: Final[int] = 1_000_000


# ─── Model Pricing Schema ────────────────────────────────────────────────────
class ModelPricing(BaseModel):
    """Per-token USD pricing for a single Claude model.

    ``cache_read_per_token`` is pre-computed as
    ``input_per_token * CACHE_READ_DISCOUNT_RATIO`` so callers never have to
    re-derive it and it cannot accidentally drift from the discount ratio.
    """

    model_id: str = Field(..., description="Anthropic model identifier")
    input_per_token: float = Field(..., gt=0.0)
    output_per_token: float = Field(..., gt=0.0)
    cache_read_per_token: float = Field(..., gt=0.0)
    cache_write_5min_multiplier: float = Field(..., gt=1.0)
    cache_write_1h_multiplier: float = Field(..., gt=1.0)


def buildModelPricing(
    model_id: str,
    input_per_million_usd: float,
    output_per_million_usd: float,
) -> ModelPricing:
    """Build a ``ModelPricing`` record from the headline per-million numbers.

    Keeps the ``PRICING`` table below readable as a price list and guarantees
    the cache-read and multiplier fields stay consistent with the constants.
    """
    input_per_token = input_per_million_usd / TOKENS_PER_MILLION
    output_per_token = output_per_million_usd / TOKENS_PER_MILLION
    return ModelPricing(
        model_id=model_id,
        input_per_token=input_per_token,
        output_per_token=output_per_token,
        cache_read_per_token=input_per_token * CACHE_READ_DISCOUNT_RATIO,
        cache_write_5min_multiplier=CACHE_5MIN_WRITE_MULTIPLIER,
        cache_write_1h_multiplier=CACHE_1H_WRITE_MULTIPLIER,
    )


# ─── Per-Model Price Table ───────────────────────────────────────────────────
# BUSINESS RULE: These are the Anthropic public list prices as of 2026-04-17.
# If Anthropic changes any multiplier or rate, update BOTH this table AND
# docs/business-rules/cache-break-even.md in the same commit. The break-even
# table in that doc is derived from these numbers.
PRICING: Final[dict[str, ModelPricing]] = {
    "claude-haiku-4-5-20251001": buildModelPricing(
        model_id="claude-haiku-4-5-20251001",
        input_per_million_usd=0.80,
        output_per_million_usd=4.00,
    ),
    "claude-sonnet-4-6": buildModelPricing(
        model_id="claude-sonnet-4-6",
        input_per_million_usd=3.00,
        output_per_million_usd=15.00,
    ),
    "claude-opus-4-7": buildModelPricing(
        model_id="claude-opus-4-7",
        input_per_million_usd=15.00,
        output_per_million_usd=75.00,
    ),
}


def getModelPricing(model: str) -> ModelPricing:
    """Return the ``ModelPricing`` record for ``model`` or raise ``KeyError``.

    Raises instead of returning ``None`` so mis-typed model names fail loud at
    the caller rather than silently computing $0 costs.
    """
    pricing = PRICING.get(model)
    if pricing is None:
        raise KeyError(
            f"Unknown model {model!r}. Known models: {sorted(PRICING.keys())}"
        )
    return pricing


def getCacheWriteMultiplier(cache_mode: str) -> float:
    """Return the write-cost multiplier for a cache mode (``5min`` or ``1h``).

    ``cache_mode == "none"`` returns 0.0 so callers can multiply through
    without branching and get no cache-write cost.
    """
    if cache_mode == "5min":
        return CACHE_5MIN_WRITE_MULTIPLIER
    if cache_mode == "1h":
        return CACHE_1H_WRITE_MULTIPLIER
    if cache_mode == "none":
        return 0.0
    raise ValueError(
        f"Unknown cache_mode {cache_mode!r}; expected 5min, 1h, or none"
    )
