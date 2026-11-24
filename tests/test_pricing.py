"""Pricing constants sanity checks.

These tests pin the public-facing prices and make sure cache-read / cache-write
derivations never drift from the constants. If any of these fail, update
BOTH ``server/calculator/pricing.py`` AND
``docs/business-rules/cache-break-even.md`` in the same commit.
"""

from __future__ import annotations

import pytest

from server.calculator.pricing import (
    CACHE_1H_WRITE_MULTIPLIER,
    CACHE_5MIN_WRITE_MULTIPLIER,
    CACHE_READ_DISCOUNT_RATIO,
    PRICING,
    TOKENS_PER_MILLION,
    getCacheWriteMultiplier,
    getModelPricing,
)


def test_cacheMultiplierConstants() -> None:
    assert CACHE_5MIN_WRITE_MULTIPLIER == 1.25
    assert CACHE_1H_WRITE_MULTIPLIER == 2.0
    assert CACHE_READ_DISCOUNT_RATIO == 0.1


def test_haikuHeadlinePrices() -> None:
    haiku = PRICING["claude-haiku-4-5-20251001"]
    assert haiku.input_per_token == pytest.approx(0.80 / TOKENS_PER_MILLION)
    assert haiku.output_per_token == pytest.approx(4.00 / TOKENS_PER_MILLION)


def test_sonnetHeadlinePrices() -> None:
    sonnet = PRICING["claude-sonnet-4-6"]
    assert sonnet.input_per_token == pytest.approx(3.00 / TOKENS_PER_MILLION)
    assert sonnet.output_per_token == pytest.approx(15.00 / TOKENS_PER_MILLION)


def test_opusHeadlinePrices() -> None:
    opus = PRICING["claude-opus-4-7"]
    assert opus.input_per_token == pytest.approx(15.00 / TOKENS_PER_MILLION)
    assert opus.output_per_token == pytest.approx(75.00 / TOKENS_PER_MILLION)


def test_cacheReadDerivedFromDiscount() -> None:
    for pricing in PRICING.values():
        expected_read = pricing.input_per_token * CACHE_READ_DISCOUNT_RATIO
        assert pricing.cache_read_per_token == pytest.approx(expected_read)


def test_cacheWriteMultipliersMatchConstants() -> None:
    for pricing in PRICING.values():
        assert pricing.cache_write_5min_multiplier == CACHE_5MIN_WRITE_MULTIPLIER
        assert pricing.cache_write_1h_multiplier == CACHE_1H_WRITE_MULTIPLIER


def test_getModelPricingRaisesOnUnknown() -> None:
    with pytest.raises(KeyError):
        getModelPricing("claude-super-mega-ultra-42")


def test_getCacheWriteMultiplierSupportsAllModes() -> None:
    assert getCacheWriteMultiplier("none") == 0.0
    assert getCacheWriteMultiplier("5min") == 1.25
    assert getCacheWriteMultiplier("1h") == 2.0


def test_getCacheWriteMultiplierRejectsUnknown() -> None:
    with pytest.raises(ValueError):
        getCacheWriteMultiplier("forever")
