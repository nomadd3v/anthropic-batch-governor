"""TypedDict mirrors of the server calculator schemas.

These live in the client so callers don't have to pull in pydantic to talk
to the service. Keep the field set identical to the server pydantic models
in ``server/calculator/*.py`` — drift here is a silent bug.
"""

from __future__ import annotations

from typing import Literal, TypedDict

CacheMode = Literal["none", "5min", "1h"]
EstimationSource = Literal["api", "heuristic"]
CalibrationSource = Literal["reservoir", "fallback"]


class InputEstimateDict(TypedDict):
    tokens: int
    source: EstimationSource
    error: str | None


class OutputProjectionDict(TypedDict):
    p50: int
    p95: int
    p99: int
    n_observations: int
    source: CalibrationSource


class CostBreakdownDict(TypedDict):
    input_cost_usd: float
    cache_read_cost_usd: float
    cache_write_cost_usd: float
    output_cost_usd: float
    total_usd: float


class CostProjectionDict(TypedDict):
    p50_usd: float
    p95_usd: float
    p99_usd: float
    breakdown_p95: CostBreakdownDict
    model: str
    cache_mode: CacheMode


class BatchRequestSpecDict(TypedDict):
    unique_input_tokens: int


class BatchCostProjectionDict(TypedDict):
    n_requests: int
    shared_system_tokens: int
    cache_mode: CacheMode
    model: str
    total_p50_usd: float
    total_p95_usd: float
    total_p99_usd: float
    per_request_p95_usd: float
    cache_break_even_warning: bool
    cache_break_even_min_n: int
    break_even_detail: str


class ModelPricingDict(TypedDict):
    model_id: str
    input_per_token: float
    output_per_token: float
    cache_read_per_token: float
    cache_write_5min_multiplier: float
    cache_write_1h_multiplier: float


class CalibrationRecordDict(TypedDict):
    operation: str
    model: str
    actual_input: int
    actual_output: int
    actual_cost_usd: float
