"""FastAPI service hosting the batch-governor.

Single process on port 5185 exposing:

* ``/calc/*`` — standalone token + cost calculator (Stream A).
* ``/v1/*`` — pre-submit gate + governance actions (Stream B).
* ``/healthz`` + ``/metrics`` — liveness + Prometheus counters.

The service assembles its dependencies (Upstash client, Supabase
ledger, Anthropic wrapper, calibration store) once at startup and
shares them via module-level singletons — each endpoint reaches into
the bag rather than re-constructing clients per request. Tests swap
the singletons through :func:`get_state` before calling the endpoint.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Final, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Response
from pydantic import BaseModel

from server.anthropic_client import (
    AnthropicClient,
    AnthropicConfigError,
    AnthropicUnavailable,
)
from server.calculator.batch import (
    BatchCostProjection,
    BatchRequestSpec,
    projectBatchCostWithCacheAmortization,
)
from server.calculator.calibration import (
    CalibrationRecord,
    CalibrationStore,
    OutputProjection,
)
from server.calculator.cost import CostProjection, projectSingleRequestCost
from server.calculator.estimator import estimateInputTokens
from server.calculator.pricing import PRICING
from server.config import (
    AppsConfig,
    get_apps_config,
    reload_apps_config,
    resolve_governor_mode,
)
from server.flags import (
    PROJECTION_MISMATCH_TOLERANCE_RATIO,
    FlagEvent,
    FlagSink,
)
from server.gate import (
    UPSTASH_APPROVAL_KEY_PREFIX,
    UPSTASH_KILL_SWITCH_KEY,
    ApprovedDecision,
    DeniedDecision,
    GatewayDecision,
    GovernorMode,
    check_batch,
)
from server.models import (
    AppDailyStatus,
    AppMonthlyStatus,
    BatchProjectionPayload,
    CalibrateRequest,
    CalibrateResponse,
    CalibrationStatsResponse,
    CheckRequest,
    EstimateInputRequest,
    EstimateInputResponse,
    HealthResponse,
    KillResponse,
    KillSwitchRequest,
    OkResponse,
    PricingResponse,
    ProjectBatchRequest,
    ProjectCostRequest,
    ProjectOutputRequest,
    ReconcileRequest,
    ReconcileResponse,
    RegisterRequest,
    StatusResponse,
    SubmitRequest,
    SubmitResponse,
)
from server.supabase_ledger import (
    BatchLifecycleEvent,
    DecisionRecord,
    SupabaseLedger,
)
from server.upstash import UpstashClient, UpstashUnavailable


# ─── Constants ──────────────────────────────────────────────────────────────
SERVICE_VERSION: Final[str] = "0.1.0"
DEFAULT_SERVICE_PORT: Final[int] = 5185

UPSTASH_BATCH_KEY_PREFIX: Final[str] = "gov:batch:"
UPSTASH_APP_INFLIGHT_KEY_PREFIX: Final[str] = "gov:app:"
UPSTASH_MONITOR_HEARTBEAT_KEY: Final[str] = "gov:monitor:heartbeat"
UPSTASH_INFLIGHT_GLOBAL_KEY: Final[str] = "gov:inflight"

MONITOR_HEARTBEAT_STALE_SECONDS: Final[int] = 180

_logger = logging.getLogger(__name__)


# ─── Dependency container ───────────────────────────────────────────────────
@dataclass
class AppState:
    """Process-wide singletons. Built by :func:`build_app_state` at startup."""

    upstash: UpstashClient
    ledger: SupabaseLedger
    anthropic: AnthropicClient
    calibration: CalibrationStore
    config: AppsConfig
    metrics: "MetricsCounters" = field(default_factory=lambda: MetricsCounters())
    # Permissive-mode wiring. Default ENFORCING keeps historical behavior; the
    # flag sink is process-wide so ``/v1/status`` can report counts since start.
    mode: GovernorMode = GovernorMode.ENFORCING
    flags: FlagSink = field(default_factory=FlagSink.fromEnv)


class MetricsCounters(BaseModel):
    """Tiny in-memory Prometheus counters exposed by ``/metrics``."""

    decisions_total: dict[str, dict[str, int]] = {}
    submits_total: dict[str, dict[str, int]] = {}
    rogue_total: int = 0
    killed_total: int = 0

    def incrDecision(self, *, app: str, decision: str) -> None:
        self.decisions_total.setdefault(app, {})
        self.decisions_total[app][decision] = (
            self.decisions_total[app].get(decision, 0) + 1
        )

    def incrSubmit(self, *, app: str, status: str) -> None:
        self.submits_total.setdefault(app, {})
        self.submits_total[app][status] = (
            self.submits_total[app].get(status, 0) + 1
        )


_STATE: AppState | None = None


def build_app_state(
    *,
    upstash: UpstashClient | None = None,
    ledger: SupabaseLedger | None = None,
    anthropic: AnthropicClient | None = None,
    calibration: CalibrationStore | None = None,
    config: AppsConfig | None = None,
    mode: GovernorMode | None = None,
    flags: FlagSink | None = None,
) -> AppState:
    """Assemble dependencies, allowing tests to inject fakes for each.

    Real boot path (``lifespan``) calls ``*.fromEnv()`` for each
    singleton; tests construct an :class:`AppState` with fakes and pass
    it to :func:`set_state`.
    """
    resolved_upstash = upstash or UpstashClient.fromEnv()
    resolved_ledger = ledger or SupabaseLedger.fromEnv()
    resolved_anthropic = anthropic or AnthropicClient()
    resolved_calibration = calibration or CalibrationStore(resolved_upstash)
    resolved_config = config or get_apps_config()
    resolved_mode = mode or resolve_governor_mode(resolved_config)
    resolved_flags = flags or FlagSink.fromEnv()
    return AppState(
        upstash=resolved_upstash,
        ledger=resolved_ledger,
        anthropic=resolved_anthropic,
        calibration=resolved_calibration,
        config=resolved_config,
        mode=resolved_mode,
        flags=resolved_flags,
    )


def set_state(state: AppState) -> None:
    """Replace the process singleton. Used by tests and startup."""
    global _STATE
    _STATE = state


def get_state() -> AppState:
    """FastAPI dependency — returns the active :class:`AppState`."""
    if _STATE is None:
        raise HTTPException(
            status_code=503,
            detail="service not initialised (no AppState)",
        )
    return _STATE


# ─── Lifespan ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    """Build dependencies and replay the ledger queue before serving.

    # BUSINESS RULE: ``flush_queue`` runs on every startup so a Supabase
    # outage during the previous run does not permanently drop audit
    # rows.
    """
    del fastapi_app
    try:
        reload_apps_config()
        state = build_app_state()
        set_state(state)
        # BUSINESS RULE: Never silently permissive — every permissive startup
        # logs one loud line naming the flag stream so operators can never miss
        # that this node approves-and-flags instead of denying.
        if state.mode is GovernorMode.PERMISSIVE:
            _logger.warning(
                "⚠ GOVERNOR MODE=PERMISSIVE — requests that enforcing would DENY "
                "are being APPROVED and flagged for review (kill switch still "
                "denies). Flag stream → %s",
                state.flags.path,
            )
        else:
            _logger.info("governor mode=enforcing")
        replayed = await state.ledger.flush_queue()
        if replayed:
            _logger.info("ledger replay on startup: %d rows", replayed)
    except (RuntimeError, AnthropicConfigError) as exc:
        # The service can still serve calculator endpoints with a partial
        # state (the calculator never touches Upstash/Supabase on the hot
        # path), but we surface the mis-config loudly.
        _logger.warning("startup skipped full init: %s", exc)
    yield


app = FastAPI(
    title="batch-governor",
    version=SERVICE_VERSION,
    lifespan=lifespan,
)


# ─── Health & Metrics ───────────────────────────────────────────────────────
@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness check plus monitor heartbeat age.

    Stream E (monitor) writes ``gov:monitor:heartbeat`` with its
    current epoch timestamp. We compute the age here so operators can
    tell the monitor has stopped without another round-trip.

    Never 503s on missing state — that would make the endpoint useless
    for the very condition it needs to report.
    """
    if _STATE is None:
        return HealthResponse(
            status="degraded",
            version=SERVICE_VERSION,
            monitor_heartbeat_age_s=999999,
        )
    age_seconds = await _readMonitorHeartbeatAge(_STATE.upstash)
    status: Literal["ok", "degraded"] = (
        "ok" if age_seconds <= MONITOR_HEARTBEAT_STALE_SECONDS else "degraded"
    )
    return HealthResponse(
        status=status,
        version=SERVICE_VERSION,
        monitor_heartbeat_age_s=age_seconds,
        mode=_STATE.mode.value,
    )


async def _readMonitorHeartbeatAge(upstash: UpstashClient) -> int:
    """Return seconds since monitor last wrote its heartbeat. ``999999`` on miss."""
    try:
        raw = await upstash.get(UPSTASH_MONITOR_HEARTBEAT_KEY)
    except UpstashUnavailable:
        return 999999
    if raw is None:
        return 999999
    try:
        last = int(float(raw))
    except (TypeError, ValueError):
        return 999999
    return max(0, int(datetime.now(timezone.utc).timestamp()) - last)


@app.get("/metrics", response_class=Response)
async def metrics() -> Response:
    """Prometheus plaintext counters. No histograms yet — see Stream E.

    Returns an empty counter set when state is missing so Prometheus
    scrape targets don't flap while the service is still booting.
    """
    if _STATE is None:
        empty_body = (
            "# HELP governor_decisions_total Decisions emitted by /v1/check\n"
            "# TYPE governor_decisions_total counter\n"
            "# HELP governor_submits_total Batches submitted to Anthropic\n"
            "# TYPE governor_submits_total counter\n"
            "# HELP governor_rogue_total Rogue batches detected by monitor\n"
            "# TYPE governor_rogue_total counter\n"
            "governor_rogue_total 0\n"
            "# HELP governor_killed_total Batches killed by governor\n"
            "# TYPE governor_killed_total counter\n"
            "governor_killed_total 0\n"
        )
        return Response(content=empty_body, media_type="text/plain; version=0.0.4")
    state = _STATE
    lines: list[str] = []
    lines.append("# HELP governor_decisions_total Decisions emitted by /v1/check")
    lines.append("# TYPE governor_decisions_total counter")
    for app_name, decisions in state.metrics.decisions_total.items():
        for decision, value in decisions.items():
            lines.append(
                f'governor_decisions_total{{app="{app_name}",'
                f'decision="{decision}"}} {value}'
            )
    lines.append("# HELP governor_submits_total Batches submitted to Anthropic")
    lines.append("# TYPE governor_submits_total counter")
    for app_name, statuses in state.metrics.submits_total.items():
        for status, value in statuses.items():
            lines.append(
                f'governor_submits_total{{app="{app_name}",'
                f'status="{status}"}} {value}'
            )
    lines.append("# HELP governor_rogue_total Rogue batches detected by monitor")
    lines.append("# TYPE governor_rogue_total counter")
    lines.append(f"governor_rogue_total {state.metrics.rogue_total}")
    lines.append("# HELP governor_killed_total Batches killed by governor")
    lines.append("# TYPE governor_killed_total counter")
    lines.append(f"governor_killed_total {state.metrics.killed_total}")
    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4")


# ─── Calculator Endpoints ───────────────────────────────────────────────────
@app.post("/calc/estimate-input", response_model=EstimateInputResponse)
async def calc_estimate_input(
    body: EstimateInputRequest,
    state: AppState = Depends(get_state),
) -> EstimateInputResponse:
    """Estimate prompt input tokens via ``count_tokens`` with heuristic fallback."""
    # AI: delegates to the Anthropic SDK inside estimateInputTokens when
    # use_count_tokens is true. Failures fall back to the char heuristic.
    estimate = estimateInputTokens(
        model=body.model,
        system=body.system,
        messages=body.messages,
        tools=body.tools,
        use_count_tokens=body.use_count_tokens,
        anthropic_client=state.anthropic if body.use_count_tokens else None,
    )
    return EstimateInputResponse.fromEstimate(estimate)


@app.post("/calc/project-output", response_model=OutputProjection)
async def calc_project_output(
    body: ProjectOutputRequest,
    state: AppState = Depends(get_state),
) -> OutputProjection:
    """Project P50/P95/P99 output tokens for an operation."""
    return await state.calibration.projectOutputDistribution(
        operation=body.operation,
        model=body.model,
        fallback_p50=body.fallback_p50,
        fallback_p95=body.fallback_p95,
        fallback_p99=body.fallback_p99,
    )


@app.post("/calc/project-cost", response_model=CostProjection)
async def calc_project_cost(
    body: ProjectCostRequest,
) -> CostProjection:
    """Project single-request P50/P95/P99 cost."""
    try:
        return projectSingleRequestCost(
            model=body.model,
            input_tokens=body.input_tokens,
            output=body.output,
            cache_mode=body.cache_mode,
            cached_input_tokens=body.cached_input_tokens,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/calc/project-batch", response_model=BatchCostProjection)
async def calc_project_batch(
    body: ProjectBatchRequest,
    state: AppState = Depends(get_state),
) -> BatchCostProjection:
    """Project batch cost with cache amortisation. Looks up output
    projection via calibration when not supplied."""
    if body.output_override is not None:
        output = body.output_override
    else:
        output = await state.calibration.projectOutputDistribution(
            operation=body.operation,
            model=body.model,
            fallback_p50=body.fallback_p50,
            fallback_p95=body.fallback_p95,
            fallback_p99=body.fallback_p99,
        )
    try:
        return projectBatchCostWithCacheAmortization(
            requests=[
                BatchRequestSpec(unique_input_tokens=item.unique_input_tokens)
                for item in body.requests
            ],
            shared_system_tokens=body.shared_system_tokens,
            cache_mode=body.cache_mode,
            model=body.model,
            output=output,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/calc/calibrate", response_model=CalibrateResponse)
async def calc_calibrate(
    body: CalibrateRequest,
    state: AppState = Depends(get_state),
) -> CalibrateResponse:
    """Record an actual observation into the operation's reservoir."""
    await state.calibration.calibrateWithActualUsage(CalibrationRecord(
        operation=body.operation,
        model=body.model,
        actual_input=body.actual_input_tokens,
        actual_output=body.actual_output_tokens,
        actual_cost_usd=body.actual_cost_usd,
    ))
    stats_samples = await _readReservoirSize(
        state.upstash, operation=body.operation, model=body.model
    )
    return CalibrateResponse(ok=True, n_observations=stats_samples)


@app.get("/calc/pricing", response_model=PricingResponse)
async def calc_pricing() -> PricingResponse:
    """Return the full ``PRICING`` dict plus a content-hash version."""
    pricing_view: dict[str, dict[str, float | str]] = {}
    for model_id, entry in PRICING.items():
        pricing_view[model_id] = {
            "model_id": entry.model_id,
            "input_per_token": entry.input_per_token,
            "output_per_token": entry.output_per_token,
            "cache_read_per_token": entry.cache_read_per_token,
            "cache_write_5min_multiplier": entry.cache_write_5min_multiplier,
            "cache_write_1h_multiplier": entry.cache_write_1h_multiplier,
        }
    encoded = json.dumps(pricing_view, sort_keys=True, default=str).encode("utf-8")
    version_hash = hashlib.sha256(encoded).hexdigest()[:12]
    return PricingResponse(version=version_hash, pricing=pricing_view)


@app.get(
    "/calc/calibration/{operation}",
    response_model=CalibrationStatsResponse,
)
async def calc_calibration_stats(
    operation: str = Path(..., min_length=1),
    model: str = "claude-haiku-4-5-20251001",
    state: AppState = Depends(get_state),
) -> CalibrationStatsResponse:
    """Return the current P50/P95/P99 reservoir stats for an operation."""
    projection = await state.calibration.projectOutputDistribution(
        operation=operation,
        model=model,
        fallback_p50=0,
        fallback_p95=0,
        fallback_p99=0,
    )
    return CalibrationStatsResponse(
        operation=operation,
        model=model,
        n_observations=projection.n_observations,
        p50=projection.p50,
        p95=projection.p95,
        p99=projection.p99,
        source=projection.source,
    )


async def _readReservoirSize(
    upstash: UpstashClient, *, operation: str, model: str
) -> int:
    try:
        values = await upstash.lrange(
            f"calc:reservoir:{operation}:{model}:out", 0, -1
        )
    except UpstashUnavailable:
        return 0
    return len(values)


# ─── Governor Endpoints ─────────────────────────────────────────────────────
@app.post("/v1/check", response_model=GatewayDecision)
async def governor_check(
    body: CheckRequest,
    state: AppState = Depends(get_state),
) -> GatewayDecision:
    """Run the pre-submit gate and return ``APPROVED`` or ``DENIED``.

    On approval the gate reserves budget, writes an approval hash in
    Upstash with a 10-min TTL, and fires an APPROVED audit row. We
    then cache the raw request payload under the same approval id so
    ``/v1/submit`` can retrieve it.
    """
    caps_model = state.config.get_caps_for_app(body.app)
    caps = caps_model.to_gate_caps(app=body.app)
    decision = await check_batch(
        app=body.app,
        requests=body.requests,
        model=body.model,
        projection=body.projection,
        fingerprint=body.fingerprint,
        caps=caps,
        upstash=state.upstash,
        ledger=state.ledger,
        cache_mode=body.cache_mode,
        actor=body.actor,
        mode=state.mode,
        flag_sink=state.flags,
    )
    state.metrics.incrDecision(app=body.app, decision=decision.decision)

    # Cache the request payload on approval so /v1/submit can hand it
    # back to Anthropic without the caller resending it.
    if isinstance(decision, ApprovedDecision):
        await _stashApprovalPayload(
            state.upstash,
            approval_id=decision.approval_id,
            body=body,
        )
    return decision


@app.post("/v1/submit", response_model=SubmitResponse)
async def governor_submit(
    body: SubmitRequest,
    state: AppState = Depends(get_state),
) -> SubmitResponse:
    """Retrieve the stored approval and submit the batch to Anthropic.

    # BUSINESS RULE: The approval must be <600s old (gate sets
    # EXPIRE on the key); past that, the caller must re-check.
    """
    stashed = await _loadApprovalPayload(state.upstash, body.approval_id)
    if stashed is None:
        raise HTTPException(
            status_code=410,
            detail="approval expired or unknown — re-run /v1/check",
        )
    try:
        batch = await state.anthropic.create_batch(
            requests=stashed.requests,
            model=stashed.model,
        )
    except AnthropicUnavailable as exc:
        state.metrics.incrSubmit(app=stashed.app, status="error")
        raise HTTPException(
            status_code=503, detail=f"Anthropic unavailable: {exc}"
        ) from exc

    batch_id = getattr(batch, "id", None) or getattr(batch, "batch_id", None)
    if not isinstance(batch_id, str):
        raise HTTPException(
            status_code=502,
            detail="Anthropic returned a batch without an id",
        )

    await _registerBatchInflight(
        state.upstash,
        app=stashed.app,
        batch_id=batch_id,
        approval_id=body.approval_id,
        model=stashed.model,
    )
    await state.ledger.write_batch_lifecycle(BatchLifecycleEvent(
        batch_id=batch_id,
        app=stashed.app,
        approval_id=body.approval_id,
        model=stashed.model,
        status="submitted",
        submitted_at=datetime.now(timezone.utc),
    ))
    state.metrics.incrSubmit(app=stashed.app, status="submitted")
    return SubmitResponse(batch_id=batch_id, approval_id=body.approval_id)


@app.post("/v1/register", response_model=OkResponse)
async def governor_register(
    body: RegisterRequest,
    state: AppState = Depends(get_state),
) -> OkResponse:
    """Record a SDK-direct batch submission (fail-open wrapper path)."""
    stashed = await _loadApprovalPayload(state.upstash, body.approval_id)
    app_name = stashed.app if stashed is not None else "unknown"
    model = stashed.model if stashed is not None else None
    await _registerBatchInflight(
        state.upstash,
        app=app_name,
        batch_id=body.batch_id,
        approval_id=body.approval_id,
        model=model,
    )
    await state.ledger.write_batch_lifecycle(BatchLifecycleEvent(
        batch_id=body.batch_id,
        app=app_name,
        approval_id=body.approval_id,
        model=model,
        status="submitted",
        submitted_at=datetime.now(timezone.utc),
    ))
    return OkResponse(ok=True)


@app.get("/v1/status", response_model=StatusResponse)
async def governor_status(
    state: AppState = Depends(get_state),
) -> StatusResponse:
    """Return in-flight batches, per-app counters, and kill-switch state."""
    now = datetime.now(timezone.utc)
    try:
        in_flight = sorted(
            await state.upstash.smembers(UPSTASH_INFLIGHT_GLOBAL_KEY)
        )
    except UpstashUnavailable:
        in_flight = []

    daily: list[AppDailyStatus] = []
    monthly: list[AppMonthlyStatus] = []
    for app_name, caps in state.config.apps.items():
        daily_key = f"gov:app:{app_name}:daily:{now.strftime('%Y%m%d')}"
        monthly_key = f"gov:app:{app_name}:monthly:{now.strftime('%Y%m')}"
        daily_reserved = await _readFloat(state.upstash, daily_key)
        monthly_reserved = await _readFloat(state.upstash, monthly_key)
        daily.append(AppDailyStatus(
            app=app_name,
            reserved_usd=daily_reserved,
            ceiling_usd=caps.daily_ceiling_usd,
            remaining_usd=max(0.0, caps.daily_ceiling_usd - daily_reserved),
        ))
        monthly.append(AppMonthlyStatus(
            app=app_name,
            reserved_usd=monthly_reserved,
            ceiling_usd=caps.monthly_ceiling_usd,
            remaining_usd=max(0.0, caps.monthly_ceiling_usd - monthly_reserved),
        ))

    kill_raw = await _readValue(state.upstash, UPSTASH_KILL_SWITCH_KEY)
    monitor_age = await _readMonitorHeartbeatAge(state.upstash)
    return StatusResponse(
        in_flight=in_flight,
        daily=daily,
        monthly=monthly,
        kill_switch=kill_raw is not None,
        monitor_alive=monitor_age <= MONITOR_HEARTBEAT_STALE_SECONDS,
        mode=state.mode.value,
        flags_since_start=state.flags.counts(),
    )


@app.post("/v1/kill/{batch_id}", response_model=KillResponse)
async def governor_kill(
    batch_id: str = Path(..., min_length=1),
    state: AppState = Depends(get_state),
) -> KillResponse:
    """Cancel a batch at Anthropic and update ledger/state."""
    try:
        await state.anthropic.cancel_batch(batch_id)
    except AnthropicUnavailable as exc:
        raise HTTPException(
            status_code=503, detail=f"Anthropic unavailable: {exc}"
        ) from exc
    await _removeBatchInflight(state.upstash, batch_id=batch_id)
    await state.ledger.write_batch_lifecycle(BatchLifecycleEvent(
        batch_id=batch_id,
        app="governor",
        status="killed",
        ended_at=datetime.now(timezone.utc),
    ))
    state.metrics.killed_total += 1
    return KillResponse(canceled=True, batch_id=batch_id)


@app.post("/v1/kill-switch", response_model=OkResponse)
async def governor_kill_switch(
    body: KillSwitchRequest,
    state: AppState = Depends(get_state),
) -> OkResponse:
    """Activate or clear the global kill switch.

    # BUSINESS RULE: A DENIED audit row is written so the ledger shows
    # who flipped the switch and why.
    """
    if body.on:
        reason_payload = json.dumps({"reason": body.reason or "unspecified"})
        await state.upstash.set(UPSTASH_KILL_SWITCH_KEY, reason_payload)
        decision_text = "APPROVED"  # the write itself succeeded
    else:
        await state.upstash.delete(UPSTASH_KILL_SWITCH_KEY)
        decision_text = "APPROVED"

    await state.ledger.write_decision(DecisionRecord(
        app="governor",
        decision=decision_text,  # type: ignore[arg-type]
        reasons=[{
            "code": "KILL_SWITCH_FLIP",
            "detail": f"set to {body.on}",
            "fix": "n/a — operator action",
        }],
        fingerprint=None,
        items=None,
        model=None,
        projected_p50_usd=None,
        projected_p95_usd=None,
        approval_id=None,
        actor=body.reason,
    ))
    return OkResponse(ok=True, kill_switch=body.on)


@app.post(
    "/v1/reconcile/{batch_id}",
    response_model=ReconcileResponse,
)
async def governor_reconcile(
    body: ReconcileRequest,
    batch_id: str = Path(..., min_length=1),
    state: AppState = Depends(get_state),
) -> ReconcileResponse:
    """Record actual spend for a completed batch and compute variance."""
    await state.ledger.write_batch_lifecycle(BatchLifecycleEvent(
        batch_id=batch_id,
        app="governor",
        status="ended",
        actual_usd=body.actual_cost_usd,
        ended_at=datetime.now(timezone.utc),
    ))
    if body.operation is not None:
        # Use batch-level reconcile data to improve calibration.
        await state.calibration.calibrateWithActualUsage(CalibrationRecord(
            operation=body.operation,
            model=_inferReconcileModel(state.config),
            actual_input=body.actual_input_tokens,
            actual_output=body.actual_output_tokens,
            actual_cost_usd=body.actual_cost_usd,
        ))
    report = await state.ledger.compute_variance(batch_id)
    # Reconciliation is observation, not gating — a projection_mismatch is
    # flagged in BOTH modes, independent of GOVERNOR_MODE.
    await _maybeFlagProjectionMismatch(state, batch_id=batch_id, report=report)
    await _removeBatchInflight(state.upstash, batch_id=batch_id)
    return ReconcileResponse(variance_pct=report.variance_pct)


async def _maybeFlagProjectionMismatch(
    state: AppState,
    *,
    batch_id: str,
    report: Any,
) -> None:
    """Flag a ``projection_mismatch`` when reconciled variance exceeds tolerance.

    ``report`` is a :class:`server.supabase_ledger.VarianceReport`. We flag when
    both projection and actual are known and the signed ratio exceeds
    :data:`PROJECTION_MISMATCH_TOLERANCE_RATIO`.
    """
    projected = report.projected_p95_usd
    actual = report.actual_usd
    variance_pct = report.variance_pct
    if projected is None or actual is None or variance_pct is None:
        return
    ratio = variance_pct / 100.0
    if abs(ratio) <= PROJECTION_MISMATCH_TOLERANCE_RATIO:
        return
    app_name = await _readBatchApp(state.upstash, batch_id)
    state.flags.emit(FlagEvent(
        kind="projection_mismatch",
        app=app_name,
        batch_id=batch_id,
        projected_usd=projected,
        actual_usd=actual,
        delta_usd=round(actual - projected, 4),
        ratio=round(ratio, 4),
        tolerance=PROJECTION_MISMATCH_TOLERANCE_RATIO,
    ))


async def _readBatchApp(upstash: UpstashClient, batch_id: str) -> str:
    """Best-effort lookup of a batch's owning app. ``'unknown'`` on miss."""
    try:
        entry = await upstash.hgetall(f"{UPSTASH_BATCH_KEY_PREFIX}{batch_id}")
    except UpstashUnavailable:
        return "unknown"
    return entry.get("app") or "unknown"


def _inferReconcileModel(config: AppsConfig) -> str:
    """Best-effort default when the reconcile body omits the model.

    Picks the first allowed model of the ``default`` entry. This is a
    known-approximate fallback; callers should pass the explicit model
    when calibrating per-model, but the batch reconcile path works on
    aggregate spend and the model is primarily used as a reservoir key.
    """
    default = config.apps.get("default")
    if default and default.allowed_models:
        return default.allowed_models[0]
    return "claude-haiku-4-5-20251001"


# ─── Internal helpers: Upstash state ─────────────────────────────────────────
class _ApprovalStash(BaseModel):
    """Payload we stash alongside the approval id for /v1/submit."""

    app: str
    model: str
    requests: list[dict[str, Any]]


def _approvalPayloadKey(approval_id: str) -> str:
    return f"{UPSTASH_APPROVAL_KEY_PREFIX}{approval_id}:payload"


async def _stashApprovalPayload(
    upstash: UpstashClient,
    *,
    approval_id: str,
    body: CheckRequest,
) -> None:
    """Store the raw request list next to the gate's HSET approval hash.

    The gate writes its approval hash under
    ``gov:approval:{approval_id}``; we stash the full payload under
    ``gov:approval:{approval_id}:payload`` so ``/v1/submit`` can
    retrieve it in one round-trip. Same 600s TTL as the gate's hash.
    """
    stash = _ApprovalStash(
        app=body.app, model=body.model, requests=body.requests
    )
    try:
        await upstash.set(
            _approvalPayloadKey(approval_id),
            stash.model_dump_json(),
            ex=600,
        )
    except UpstashUnavailable:
        _logger.warning(
            "could not stash approval payload (Upstash down); "
            "/v1/submit will need the raw SDK fallback"
        )


async def _loadApprovalPayload(
    upstash: UpstashClient, approval_id: str
) -> _ApprovalStash | None:
    try:
        raw = await upstash.get(_approvalPayloadKey(approval_id))
    except UpstashUnavailable:
        return None
    if raw is None:
        return None
    try:
        return _ApprovalStash.model_validate_json(raw)
    except ValueError:
        return None


async def _registerBatchInflight(
    upstash: UpstashClient,
    *,
    app: str,
    batch_id: str,
    approval_id: str,
    model: str | None,
) -> None:
    """Write ``gov:batch:{id}`` + add to per-app and global in-flight sets."""
    try:
        await upstash.hset(
            f"{UPSTASH_BATCH_KEY_PREFIX}{batch_id}",
            {
                "app": app,
                "approval_id": approval_id,
                "model": model or "",
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        await upstash.sadd(
            f"{UPSTASH_APP_INFLIGHT_KEY_PREFIX}{app}:inflight", batch_id
        )
        await upstash.sadd(UPSTASH_INFLIGHT_GLOBAL_KEY, batch_id)
    except UpstashUnavailable as exc:
        _logger.warning("could not register in-flight batch %s: %s", batch_id, exc)


async def _removeBatchInflight(
    upstash: UpstashClient, *, batch_id: str
) -> None:
    """Remove a batch from both the global and per-app in-flight sets."""
    try:
        entry = await upstash.hgetall(f"{UPSTASH_BATCH_KEY_PREFIX}{batch_id}")
        app_name = entry.get("app")
        if app_name:
            await upstash.srem(
                f"{UPSTASH_APP_INFLIGHT_KEY_PREFIX}{app_name}:inflight",
                batch_id,
            )
        await upstash.srem(UPSTASH_INFLIGHT_GLOBAL_KEY, batch_id)
    except UpstashUnavailable as exc:
        _logger.warning("could not clean up batch %s: %s", batch_id, exc)


async def _readFloat(upstash: UpstashClient, key: str) -> float:
    try:
        raw = await upstash.get(key)
    except UpstashUnavailable:
        return 0.0
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


async def _readValue(upstash: UpstashClient, key: str) -> str | None:
    try:
        return await upstash.get(key)
    except UpstashUnavailable:
        return None


# ─── Expose decision union for OpenAPI ──────────────────────────────────────
# FastAPI needs the union members to be real pydantic models, which they
# already are in ``server.gate``. Re-export so OpenAPI shows them clearly.
__all__ = [
    "app",
    "AppState",
    "build_app_state",
    "get_state",
    "set_state",
    "ApprovedDecision",
    "DeniedDecision",
    "GatewayDecision",
]


# ─── Environment knobs for local dev ────────────────────────────────────────
def getServicePort() -> int:
    """Read ``GOVERNOR_PORT`` or fall back to the default port (5185)."""
    raw = os.environ.get("GOVERNOR_PORT")
    if not raw:
        return DEFAULT_SERVICE_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_SERVICE_PORT
