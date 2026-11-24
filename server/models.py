"""Pydantic request/response schemas for all HTTP endpoints.

Every ``server/app.py`` route declares its body using a model in this
file. Keeping the schemas in one place means clients (bash CLI, Python
wrapper, TS wrapper) have a single surface to introspect — and FastAPI's
auto-generated OpenAPI document reflects the real contract.

No business logic here. Each model is a transport-shape declaration
plus docstrings explaining what the caller supplies and what comes
back. Validation that needs cross-field awareness lives in the route
handlers, not in pydantic validators, so the error surface stays
consistent with the ``DenialReason`` shape from the gate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from server.calculator.batch import BatchCostProjection, CacheMode
from server.calculator.calibration import CalibrationSource, OutputProjection
from server.calculator.cost import CostProjection
from server.calculator.estimator import EstimationSource, InputEstimate


# ─── Calculator — /calc/estimate-input ──────────────────────────────────────
class EstimateInputRequest(BaseModel):
    """Body for ``POST /calc/estimate-input``.

    Matches the Anthropic SDK's ``messages.count_tokens`` shape so
    callers can forward their in-flight request payload directly.
    """

    model: str
    system: str | list[dict[str, Any]] | None = None
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    use_count_tokens: bool = True


class EstimateInputResponse(BaseModel):
    """Response for ``POST /calc/estimate-input``."""

    tokens: int = Field(..., ge=0)
    source: EstimationSource
    error: str | None = None

    @classmethod
    def fromEstimate(cls, estimate: InputEstimate) -> "EstimateInputResponse":
        """Project the internal ``InputEstimate`` onto the HTTP response."""
        return cls(
            tokens=estimate.tokens,
            source=estimate.source,
            error=estimate.error,
        )


# ─── Calculator — /calc/project-output ──────────────────────────────────────
class ProjectOutputRequest(BaseModel):
    """Body for ``POST /calc/project-output``.

    ``fallback_*`` fields are used when the calibration reservoir has
    fewer than ``MIN_OBSERVATIONS_FOR_PROJECTION`` samples — see
    ``server/calculator/calibration.py`` for the threshold.
    """

    operation: str
    model: str
    fallback_p50: int = Field(default=500, ge=0)
    fallback_p95: int = Field(default=1500, ge=0)
    fallback_p99: int | None = Field(default=None, ge=0)


# ─── Calculator — /calc/project-cost ────────────────────────────────────────
class ProjectCostRequest(BaseModel):
    """Body for ``POST /calc/project-cost``.

    ``output`` comes from an ``/calc/project-output`` call; callers can
    forward the full :class:`OutputProjection` verbatim.
    """

    model: str
    input_tokens: int = Field(..., ge=0)
    output: OutputProjection
    cache_mode: CacheMode = "none"
    cached_input_tokens: int = Field(default=0, ge=0)


# ─── Calculator — /calc/project-batch ───────────────────────────────────────
class ProjectBatchRequestItem(BaseModel):
    """One request spec inside a batch projection body."""

    unique_input_tokens: int = Field(..., ge=0)


class ProjectBatchRequest(BaseModel):
    """Body for ``POST /calc/project-batch``.

    ``operation`` keys into the calibration reservoir so the service
    can look up P50/P95/P99 before calling the batch cost projector.
    Callers who already have an :class:`OutputProjection` can override
    by passing ``output`` directly — used by tests and the CLI.
    """

    requests: list[ProjectBatchRequestItem]
    shared_system_tokens: int = Field(..., ge=0)
    cache_mode: CacheMode
    model: str
    operation: str
    fallback_p50: int = Field(default=500, ge=0)
    fallback_p95: int = Field(default=1500, ge=0)
    fallback_p99: int | None = Field(default=None, ge=0)
    output_override: OutputProjection | None = None


# ─── Calculator — /calc/calibrate ───────────────────────────────────────────
class CalibrateRequest(BaseModel):
    """Body for ``POST /calc/calibrate``."""

    operation: str
    model: str
    actual_input_tokens: int = Field(..., ge=0)
    actual_output_tokens: int = Field(..., ge=0)
    actual_cost_usd: float = Field(default=0.0, ge=0.0)


class CalibrateResponse(BaseModel):
    """Response for ``POST /calc/calibrate``."""

    ok: bool
    n_observations: int


# ─── Calculator — /calc/pricing & calibration stats ─────────────────────────
class PricingResponse(BaseModel):
    """Response for ``GET /calc/pricing`` — full ``PRICING`` dict + version."""

    version: str
    pricing: dict[str, dict[str, float | str]]


class CalibrationStatsResponse(BaseModel):
    """Response for ``GET /calc/calibration/{operation}``."""

    operation: str
    model: str
    n_observations: int
    p50: int
    p95: int
    p99: int
    source: CalibrationSource


# ─── Governor — /v1/check ───────────────────────────────────────────────────
class BatchProjectionPayload(BaseModel):
    """Minimal projection payload the caller ships with ``/v1/check``.

    The gate only reads ``p50_usd``, ``p95_usd``, and
    ``cache_break_even_warning`` off it. Callers normally pipe the full
    :class:`BatchCostProjection` from ``/calc/project-batch``; we accept
    either shape and project onto the three fields the gate needs.
    """

    p50_usd: float = Field(..., ge=0.0)
    p95_usd: float = Field(..., ge=0.0)
    cache_break_even_warning: bool = False


class CheckRequest(BaseModel):
    """Body for ``POST /v1/check``.

    ``projection`` may be a full :class:`BatchCostProjection` (returned by
    ``/calc/project-batch``) or a :class:`BatchProjectionPayload`; both
    expose the three attributes the gate's ``BatchProjection`` Protocol
    needs.
    """

    app: str
    requests: list[dict[str, Any]]
    model: str
    projection: BatchCostProjection | BatchProjectionPayload
    fingerprint: str
    cache_mode: CacheMode = "none"
    actor: str | None = None


# ─── Governor — /v1/submit ──────────────────────────────────────────────────
class SubmitRequest(BaseModel):
    """Body for ``POST /v1/submit``.

    Callers pass an ``approval_id`` previously returned by ``/v1/check``.
    The service looks up the cached request payload in Upstash, hands it
    to ``AnthropicClient.create_batch``, and registers the resulting
    batch id in the in-flight set.
    """

    approval_id: str


class SubmitResponse(BaseModel):
    """Response for ``POST /v1/submit``."""

    batch_id: str
    approval_id: str


# ─── Governor — /v1/register ────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    """Body for ``POST /v1/register``.

    For apps that call ``messages.batches.create`` via the SDK directly
    but still want the governor to track the batch — the wrapper
    (Stream D) uses this on fail-open so the monitor can observe.
    """

    approval_id: str
    batch_id: str


class OkResponse(BaseModel):
    """Generic ``{ok: true}`` response used by register / kill-switch."""

    ok: bool
    kill_switch: bool | None = None


# ─── Governor — /v1/status ──────────────────────────────────────────────────
class AppDailyStatus(BaseModel):
    """Per-app daily spend summary."""

    app: str
    reserved_usd: float
    ceiling_usd: float
    remaining_usd: float


class AppMonthlyStatus(BaseModel):
    """Per-app monthly spend summary."""

    app: str
    reserved_usd: float
    ceiling_usd: float
    remaining_usd: float


class StatusResponse(BaseModel):
    """Response for ``GET /v1/status``."""

    in_flight: list[str]
    daily: list[AppDailyStatus]
    monthly: list[AppMonthlyStatus]
    kill_switch: bool
    monitor_alive: bool
    # Permissive-mode visibility: the active mode and per-kind flag counts since
    # this process started, so dashboards can never miss a permissive node.
    mode: str = "enforcing"
    flags_since_start: dict[str, int] = Field(default_factory=dict)


# ─── Governor — /v1/kill ────────────────────────────────────────────────────
class KillResponse(BaseModel):
    """Response for ``POST /v1/kill/{batch_id}``."""

    canceled: bool
    batch_id: str


class KillSwitchRequest(BaseModel):
    """Body for ``POST /v1/kill-switch``."""

    on: bool
    reason: str | None = None


# ─── Governor — /v1/reconcile ───────────────────────────────────────────────
class ReconcileRequest(BaseModel):
    """Body for ``POST /v1/reconcile/{batch_id}``."""

    actual_input_tokens: int = Field(..., ge=0)
    actual_output_tokens: int = Field(..., ge=0)
    actual_cost_usd: float = Field(..., ge=0.0)
    operation: str | None = None


class ReconcileResponse(BaseModel):
    """Response for ``POST /v1/reconcile/{batch_id}``."""

    variance_pct: float | None


# ─── Service — /healthz ─────────────────────────────────────────────────────
class HealthResponse(BaseModel):
    """Response for ``GET /healthz``."""

    status: Literal["ok", "degraded"] = "ok"
    version: str
    monitor_heartbeat_age_s: int
    # Active governor mode so dashboards can flag permissive nodes at a glance.
    mode: str = "enforcing"
