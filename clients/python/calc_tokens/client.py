"""HTTP client for the ``/calc/*`` endpoints of the batch-governor service.

Why this is long: this module pairs one sync + one async method for each of
six endpoints (``estimate-input``, ``project-output``, ``project-cost``,
``project-batch``, ``calibrate``, ``pricing``) plus five response-casting
helpers. Splitting sync vs. async into separate files would hide the
symmetry and force callers to pick an import path; keeping them side-by-side
is worth the line count.

The client is intentionally thin: every method mirrors one HTTP endpoint,
and the server does the math. This matches ADR-001 (single source of truth
in one process).

Base URL resolution order:
1. Explicit ``base_url`` constructor argument.
2. ``GOVERNOR_BASE_URL`` environment variable.
3. ``http://127.0.0.1:5185`` default.
"""

from __future__ import annotations

import os
from typing import Any, Final

import httpx

from .types import (
    BatchCostProjectionDict,
    BatchRequestSpecDict,
    CalibrationRecordDict,
    CacheMode,
    CostProjectionDict,
    InputEstimateDict,
    ModelPricingDict,
    OutputProjectionDict,
)


DEFAULT_GOVERNOR_BASE_URL: Final[str] = "http://127.0.0.1:5185"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0


class CalcTokensClient:
    """Sync + async client for the token calculator endpoints.

    Construct once per process; the underlying ``httpx.Client`` /
    ``httpx.AsyncClient`` are reused across calls. Close via
    ``close()`` / ``aclose()`` on shutdown.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        resolved_base = (
            base_url
            or os.environ.get("GOVERNOR_BASE_URL")
            or DEFAULT_GOVERNOR_BASE_URL
        )
        self._base_url = resolved_base.rstrip("/")
        self._timeout = timeout_seconds
        self._sync: httpx.Client | None = None
        self._async: httpx.AsyncClient | None = None

    # ─── Lifecycle ───────────────────────────────────────────────────────
    def _syncClient(self) -> httpx.Client:
        if self._sync is None:
            self._sync = httpx.Client(
                base_url=self._base_url, timeout=self._timeout
            )
        return self._sync

    def _asyncClient(self) -> httpx.AsyncClient:
        if self._async is None:
            self._async = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout
            )
        return self._async

    def close(self) -> None:
        if self._sync is not None:
            self._sync.close()
            self._sync = None

    async def aclose(self) -> None:
        if self._async is not None:
            await self._async.aclose()
            self._async = None

    # ─── Sync Methods ────────────────────────────────────────────────────
    def estimateInputTokens(
        self,
        model: str,
        system: str | list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        use_count_tokens: bool = True,
    ) -> InputEstimateDict:
        """POST ``/calc/estimate-input``. Returns an ``InputEstimateDict``."""
        payload = {
            "model": model,
            "system": system,
            "messages": messages,
            "tools": tools,
            "use_count_tokens": use_count_tokens,
        }
        response = self._syncClient().post("/calc/estimate-input", json=payload)
        response.raise_for_status()
        return _castInputEstimate(response.json())

    def projectOutputDistribution(
        self,
        operation: str,
        model: str,
        fallback_p50: int,
        fallback_p95: int,
        fallback_p99: int | None = None,
    ) -> OutputProjectionDict:
        """POST ``/calc/project-output``. Returns an ``OutputProjectionDict``."""
        payload = {
            "operation": operation,
            "model": model,
            "fallback_p50": fallback_p50,
            "fallback_p95": fallback_p95,
            "fallback_p99": fallback_p99,
        }
        response = self._syncClient().post("/calc/project-output", json=payload)
        response.raise_for_status()
        return _castOutputProjection(response.json())

    def projectSingleRequestCost(
        self,
        model: str,
        input_tokens: int,
        output: OutputProjectionDict,
        cache_mode: CacheMode = "none",
        cached_input_tokens: int = 0,
    ) -> CostProjectionDict:
        """POST ``/calc/project-cost``. Returns a ``CostProjectionDict``."""
        payload = {
            "model": model,
            "input_tokens": input_tokens,
            "output": output,
            "cache_mode": cache_mode,
            "cached_input_tokens": cached_input_tokens,
        }
        response = self._syncClient().post("/calc/project-cost", json=payload)
        response.raise_for_status()
        return _castCostProjection(response.json())

    def projectBatchCostWithCacheAmortization(
        self,
        requests: list[BatchRequestSpecDict],
        shared_system_tokens: int,
        cache_mode: CacheMode,
        model: str,
        output: OutputProjectionDict,
    ) -> BatchCostProjectionDict:
        """POST ``/calc/project-batch``. Returns a ``BatchCostProjectionDict``."""
        payload = {
            "requests": requests,
            "shared_system_tokens": shared_system_tokens,
            "cache_mode": cache_mode,
            "model": model,
            "output": output,
        }
        response = self._syncClient().post("/calc/project-batch", json=payload)
        response.raise_for_status()
        return _castBatchCostProjection(response.json())

    def calibrateWithActualUsage(self, record: CalibrationRecordDict) -> None:
        """POST ``/calc/calibrate``. Fire-and-forget; raises on HTTP error."""
        response = self._syncClient().post("/calc/calibrate", json=record)
        response.raise_for_status()

    def getPricingTable(self) -> dict[str, ModelPricingDict]:
        """GET ``/calc/pricing``. Returns the full pricing table."""
        response = self._syncClient().get("/calc/pricing")
        response.raise_for_status()
        return _castPricingTable(response.json())

    # ─── Async Methods ───────────────────────────────────────────────────
    async def estimateInputTokensAsync(
        self,
        model: str,
        system: str | list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        use_count_tokens: bool = True,
    ) -> InputEstimateDict:
        """Async variant of :meth:`estimateInputTokens`."""
        payload = {
            "model": model,
            "system": system,
            "messages": messages,
            "tools": tools,
            "use_count_tokens": use_count_tokens,
        }
        response = await self._asyncClient().post(
            "/calc/estimate-input", json=payload
        )
        response.raise_for_status()
        return _castInputEstimate(response.json())

    async def projectOutputDistributionAsync(
        self,
        operation: str,
        model: str,
        fallback_p50: int,
        fallback_p95: int,
        fallback_p99: int | None = None,
    ) -> OutputProjectionDict:
        """Async variant of :meth:`projectOutputDistribution`."""
        payload = {
            "operation": operation,
            "model": model,
            "fallback_p50": fallback_p50,
            "fallback_p95": fallback_p95,
            "fallback_p99": fallback_p99,
        }
        response = await self._asyncClient().post(
            "/calc/project-output", json=payload
        )
        response.raise_for_status()
        return _castOutputProjection(response.json())

    async def projectSingleRequestCostAsync(
        self,
        model: str,
        input_tokens: int,
        output: OutputProjectionDict,
        cache_mode: CacheMode = "none",
        cached_input_tokens: int = 0,
    ) -> CostProjectionDict:
        """Async variant of :meth:`projectSingleRequestCost`."""
        payload = {
            "model": model,
            "input_tokens": input_tokens,
            "output": output,
            "cache_mode": cache_mode,
            "cached_input_tokens": cached_input_tokens,
        }
        response = await self._asyncClient().post(
            "/calc/project-cost", json=payload
        )
        response.raise_for_status()
        return _castCostProjection(response.json())

    async def projectBatchCostWithCacheAmortizationAsync(
        self,
        requests: list[BatchRequestSpecDict],
        shared_system_tokens: int,
        cache_mode: CacheMode,
        model: str,
        output: OutputProjectionDict,
    ) -> BatchCostProjectionDict:
        """Async variant of :meth:`projectBatchCostWithCacheAmortization`."""
        payload = {
            "requests": requests,
            "shared_system_tokens": shared_system_tokens,
            "cache_mode": cache_mode,
            "model": model,
            "output": output,
        }
        response = await self._asyncClient().post(
            "/calc/project-batch", json=payload
        )
        response.raise_for_status()
        return _castBatchCostProjection(response.json())

    async def calibrateWithActualUsageAsync(
        self, record: CalibrationRecordDict
    ) -> None:
        """Async variant of :meth:`calibrateWithActualUsage`."""
        response = await self._asyncClient().post("/calc/calibrate", json=record)
        response.raise_for_status()

    async def getPricingTableAsync(self) -> dict[str, ModelPricingDict]:
        """Async variant of :meth:`getPricingTable`."""
        response = await self._asyncClient().get("/calc/pricing")
        response.raise_for_status()
        return _castPricingTable(response.json())


# ─── Response Casting Helpers ────────────────────────────────────────────────
def _castInputEstimate(data: Any) -> InputEstimateDict:
    return {
        "tokens": int(data["tokens"]),
        "source": data["source"],
        "error": data.get("error"),
    }


def _castOutputProjection(data: Any) -> OutputProjectionDict:
    return {
        "p50": int(data["p50"]),
        "p95": int(data["p95"]),
        "p99": int(data["p99"]),
        "n_observations": int(data["n_observations"]),
        "source": data["source"],
    }


def _castCostProjection(data: Any) -> CostProjectionDict:
    return {
        "p50_usd": float(data["p50_usd"]),
        "p95_usd": float(data["p95_usd"]),
        "p99_usd": float(data["p99_usd"]),
        "breakdown_p95": {
            "input_cost_usd": float(data["breakdown_p95"]["input_cost_usd"]),
            "cache_read_cost_usd": float(
                data["breakdown_p95"]["cache_read_cost_usd"]
            ),
            "cache_write_cost_usd": float(
                data["breakdown_p95"]["cache_write_cost_usd"]
            ),
            "output_cost_usd": float(data["breakdown_p95"]["output_cost_usd"]),
            "total_usd": float(data["breakdown_p95"]["total_usd"]),
        },
        "model": str(data["model"]),
        "cache_mode": data["cache_mode"],
    }


def _castBatchCostProjection(data: Any) -> BatchCostProjectionDict:
    return {
        "n_requests": int(data["n_requests"]),
        "shared_system_tokens": int(data["shared_system_tokens"]),
        "cache_mode": data["cache_mode"],
        "model": str(data["model"]),
        "total_p50_usd": float(data["total_p50_usd"]),
        "total_p95_usd": float(data["total_p95_usd"]),
        "total_p99_usd": float(data["total_p99_usd"]),
        "per_request_p95_usd": float(data["per_request_p95_usd"]),
        "cache_break_even_warning": bool(data["cache_break_even_warning"]),
        "cache_break_even_min_n": int(data["cache_break_even_min_n"]),
        "break_even_detail": str(data["break_even_detail"]),
    }


def _castPricingTable(data: Any) -> dict[str, ModelPricingDict]:
    result: dict[str, ModelPricingDict] = {}
    for model_id, entry in data.items():
        result[model_id] = {
            "model_id": str(entry["model_id"]),
            "input_per_token": float(entry["input_per_token"]),
            "output_per_token": float(entry["output_per_token"]),
            "cache_read_per_token": float(entry["cache_read_per_token"]),
            "cache_write_5min_multiplier": float(
                entry["cache_write_5min_multiplier"]
            ),
            "cache_write_1h_multiplier": float(entry["cache_write_1h_multiplier"]),
        }
    return result
