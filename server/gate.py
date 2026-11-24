"""Pre-submit gate — the core rule engine of the batch-governor.

Every ``POST /v1/check`` call lands in :func:`check_batch`. The function runs
one early-return helper per denial code (see the ``_deny_if_*`` block below)
and, if every check passes, reserves the app's projected P95 against the
Upstash daily/monthly counters and writes an APPROVED row to Supabase.

The layout is deliberately flat: one helper per business rule, each with a
``# BUSINESS RULE:`` comment that matches
``docs/business-rules/denial-rules.md`` word-for-word in intent. Adding a new
rule means adding a new helper, a new entry in :class:`DenialCode`, and a new
test case — nothing else.

Latency budget
--------------
The gate's target is ~50ms p95. Upstash round-trip dominates; we avoid:

* Chained awaits where a pipeline works (daily/monthly/approval/fingerprint
  writes on APPROVED are one pipeline call).
* Any Supabase work on the critical path — :class:`SupabaseLedger` is
  fire-and-forget.
* ``datetime.utcnow()`` allocations inside the hot loop — we pass a single
  ``now`` timestamp through.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .flags import FlagEvent, FlagSink
from .supabase_ledger import DecisionRecord, SupabaseLedger
from .upstash import UpstashClient, UpstashUnavailable

_logger = logging.getLogger(__name__)


# ─── Projection protocol ─────────────────────────────────────────────────────
# Stream A owns the real ``BatchCostProjection`` pydantic model in
# ``server/calculator/batch.py``. The gate only reads three attributes off it,
# so we declare a structural protocol instead of importing the concrete type.
# That keeps the gate decoupled from the calculator's internal shape and lets
# tests pass a lightweight stand-in without needing Stream A fully wired.
@runtime_checkable
class BatchProjection(Protocol):
    """Structural type the gate consumes. Any object with these attributes."""

    @property
    def p50_usd(self) -> float: ...

    @property
    def p95_usd(self) -> float: ...

    @property
    def cache_break_even_warning(self) -> bool: ...


# ─── Governor mode ───────────────────────────────────────────────────────────
class GovernorMode(str, Enum):
    """How the gate treats a request that fails a rule.

    * ``ENFORCING`` — historical gatekeeper: rule failures are DENIED. This is
      the in-code default, so behavior is unchanged unless a node is explicitly
      flipped to permissive.
    * ``PERMISSIVE`` — auditor: rule failures (except the emergency kill switch)
      are APPROVED and flagged for review instead of denied.
    """

    ENFORCING = "enforcing"
    PERMISSIVE = "permissive"

    @classmethod
    def from_str(cls, raw: str) -> "GovernorMode":
        """Parse a mode string, defaulting to ENFORCING (fail-safe) on garbage."""
        try:
            return cls(raw.strip().lower())
        except ValueError:
            _logger.warning(
                "unknown governor mode %r; defaulting to enforcing", raw
            )
            return cls.ENFORCING


# ─── Constants ───────────────────────────────────────────────────────────────
APPROVAL_TTL_SECONDS: Final[int] = 600            # 10 min
FINGERPRINT_TTL_SECONDS: Final[int] = 2 * 60 * 60  # 2 h
DAILY_COUNTER_TTL_SECONDS: Final[int] = 48 * 60 * 60
MONTHLY_COUNTER_TTL_SECONDS: Final[int] = 45 * 24 * 60 * 60
CACHE_1H_BREAK_EVEN_MIN_N: Final[int] = 3

UPSTASH_APPROVAL_KEY_PREFIX: Final[str] = "gov:approval:"
UPSTASH_DAILY_KEY_PREFIX: Final[str] = "gov:app:"
UPSTASH_MONTHLY_KEY_PREFIX: Final[str] = "gov:app:"
UPSTASH_FINGERPRINT_KEY_PREFIX: Final[str] = "gov:fingerprint:"
UPSTASH_KILL_SWITCH_KEY: Final[str] = "gov:kill-switch"


# ─── Denial / Decision types ─────────────────────────────────────────────────
class DenialCode(str, Enum):
    """Every code a gate denial can return. String enum so JSON is readable."""

    BATCH_SIZE_EXCEEDED = "BATCH_SIZE_EXCEEDED"
    PER_SUBMIT_CAP = "PER_SUBMIT_CAP"
    DAILY_CEILING = "DAILY_CEILING"
    MONTHLY_CEILING = "MONTHLY_CEILING"
    MODEL_NOT_ALLOWED = "MODEL_NOT_ALLOWED"
    MISSING_CUSTOM_ID = "MISSING_CUSTOM_ID"
    DUPLICATE_FINGERPRINT = "DUPLICATE_FINGERPRINT"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    STATE_UNAVAILABLE = "STATE_UNAVAILABLE"


class DenialReason(BaseModel):
    """Structured denial reason. ``fix`` is specific enough to resolve blind."""

    code: DenialCode
    detail: str
    fix: str


class WarningCode(str, Enum):
    """Non-blocking warnings attached to APPROVED decisions."""

    CACHE_BREAK_EVEN_WARNING = "CACHE_BREAK_EVEN_WARNING"


class DecisionWarning(BaseModel):
    """Non-blocking warning on an APPROVED decision."""

    code: WarningCode
    detail: str


class ApprovedDecision(BaseModel):
    """Emitted when every denial helper passes."""

    decision: Literal["APPROVED"] = "APPROVED"
    approval_id: str
    p95_usd: float
    expires_at: datetime
    warnings: list[DecisionWarning] = Field(default_factory=list)


class DeniedDecision(BaseModel):
    """Emitted when any denial helper returns a reason."""

    decision: Literal["DENIED"] = "DENIED"
    reasons: list[DenialReason]


GatewayDecision = ApprovedDecision | DeniedDecision


# ─── App-config view the gate consumes ───────────────────────────────────────
@dataclass(frozen=True)
class AppCaps:
    """Slice of ``config/apps.yaml`` the gate actually needs.

    Stream C owns the full YAML loader (``server/config.py``). That loader
    returns an ``AppCaps`` per app name and the FastAPI layer passes it in to
    :func:`check_batch`. Keeping this type here rather than in ``config.py``
    means the gate's unit tests do not need a YAML file on disk.
    """

    app: str
    max_batch_size: int
    per_submit_cap_usd: float
    daily_ceiling_usd: float
    monthly_ceiling_usd: float
    allowed_models: tuple[str, ...]
    allow_daemon: bool
    allow_large_batch: bool


# ─── Internal helpers (one per rule) ─────────────────────────────────────────
async def _deny_if_kill_switch_active(
    *, upstash: UpstashClient
) -> DenialReason | None:
    # BUSINESS RULE: When `POST /v1/kill-switch on` has been called, the gate
    # denies all submits until `POST /v1/kill-switch off`. Emergency halt.
    raw = await upstash.get(UPSTASH_KILL_SWITCH_KEY)
    if raw is None:
        return None
    try:
        payload = json.loads(raw) if raw.startswith("{") else {"reason": raw}
    except json.JSONDecodeError:
        payload = {"reason": raw}
    reason_text = str(payload.get("reason", "unspecified"))
    return DenialReason(
        code=DenialCode.KILL_SWITCH_ACTIVE,
        detail=f"kill switch active: {reason_text}",
        fix=(
            "clear via `POST /v1/kill-switch off` or "
            "`batch-governor kill-switch off` after verifying the "
            "underlying incident is resolved"
        ),
    )


def _deny_if_batch_size_exceeded(
    *, caps: AppCaps, request_count: int
) -> DenialReason | None:
    # BUSINESS RULE: An app may not submit a batch with more requests than its
    # configured `max_batch_size`. max_batch_size: 0 means no batches at all.
    if request_count <= caps.max_batch_size and caps.max_batch_size > 0:
        return None
    if caps.max_batch_size == 0:
        return DenialReason(
            code=DenialCode.BATCH_SIZE_EXCEEDED,
            detail=(
                f"app:{caps.app} has max_batch_size=0 — batches are "
                f"disabled for this app (single-shot only per ADR-006)"
            ),
            fix=(
                f"use single-shot `messages.create` instead, or set "
                f"`max_batch_size` > 0 in config/apps.yaml for app:"
                f"{caps.app} with an ADR entry"
            ),
        )
    return DenialReason(
        code=DenialCode.BATCH_SIZE_EXCEEDED,
        detail=(
            f"batch size {request_count} exceeds "
            f"app:{caps.app} max {caps.max_batch_size}"
        ),
        fix=(
            f"split into batches of {caps.max_batch_size} or set "
            f"`allow_large_batch: true` in config/apps.yaml"
        ),
    )


def _deny_if_model_not_allowed(
    *, caps: AppCaps, model: str
) -> DenialReason | None:
    # BUSINESS RULE: An app may only submit batches using models in its
    # `allowed_models` list. Sonnet/Opus require explicit opt-in per app.
    if model in caps.allowed_models:
        return None
    return DenialReason(
        code=DenialCode.MODEL_NOT_ALLOWED,
        detail=(
            f"{model} not in app:{caps.app} allowlist "
            f"{list(caps.allowed_models)}"
        ),
        fix=(
            f"use one of {list(caps.allowed_models)} or add "
            f"{model} to `allowed_models` for app:{caps.app} in "
            f"config/apps.yaml"
        ),
    )


def _deny_if_missing_custom_id(
    *, requests: list[dict[str, Any]]
) -> DenialReason | None:
    # BUSINESS RULE: Every request within a batch must have a unique
    # `custom_id`. Reconciliation matches Anthropic's response back to the
    # original request by custom_id; without it, reconciliation breaks.
    seen: set[str] = set()
    for index, request in enumerate(requests):
        custom_id = request.get("custom_id")
        if not custom_id:
            return DenialReason(
                code=DenialCode.MISSING_CUSTOM_ID,
                detail=(
                    f"request[{index}] missing custom_id "
                    f"(required for reconciliation)"
                ),
                fix="add a unique custom_id (a UUID is fine) to each request",
            )
        if custom_id in seen:
            return DenialReason(
                code=DenialCode.MISSING_CUSTOM_ID,
                detail=(
                    f"request[{index}] duplicates custom_id "
                    f"{custom_id!r}; ids must be unique within a batch"
                ),
                fix=(
                    "regenerate custom_ids so each request in the batch "
                    "has a unique identifier"
                ),
            )
        seen.add(custom_id)
    return None


async def _deny_if_duplicate_fingerprint(
    *, upstash: UpstashClient, fingerprint: str
) -> DenialReason | None:
    # BUSINESS RULE: If the identical request set has been submitted within
    # the last 2 hours, refuse. Crash-recovery safety against daemons that
    # restart with the same queue and double-submit.
    key = f"{UPSTASH_FINGERPRINT_KEY_PREFIX}{fingerprint}"
    existing = await upstash.get(key)
    if existing is None:
        return None
    return DenialReason(
        code=DenialCode.DUPLICATE_FINGERPRINT,
        detail=(
            f"identical request set previously submitted as {existing} "
            f"within the last {FINGERPRINT_TTL_SECONDS // 3600}h"
        ),
        fix=(
            "drain the previous batch via `GET /v1/status` before "
            "retrying; if you genuinely need to re-run identical work, "
            "wait for the fingerprint TTL to expire"
        ),
    )


def _deny_if_per_submit_cap_exceeded(
    *, caps: AppCaps, projection_p95_usd: float
) -> DenialReason | None:
    # BUSINESS RULE: A single batch's projected P95 cost may not exceed the
    # app's `per_submit_cap_usd`. Caps blast radius for any one submit.
    if projection_p95_usd <= caps.per_submit_cap_usd:
        return None
    return DenialReason(
        code=DenialCode.PER_SUBMIT_CAP,
        detail=(
            f"projected P95 ${projection_p95_usd:.2f} exceeds "
            f"per-submit cap ${caps.per_submit_cap_usd:.2f} for "
            f"app:{caps.app}"
        ),
        fix=(
            "split into smaller batches, switch to a cheaper allowed "
            "model, or raise `per_submit_cap_usd` for this app in "
            "config/apps.yaml with an ADR entry"
        ),
    )


async def _deny_if_daily_ceiling_would_exceed(
    *,
    upstash: UpstashClient,
    caps: AppCaps,
    projection_p95_usd: float,
    now: datetime,
) -> DenialReason | None:
    # BUSINESS RULE: An app's cumulative daily spend (projected + actual) may
    # not exceed `daily_ceiling_usd`. Counter resets at midnight UTC.
    key = _buildDailyKey(app=caps.app, now=now)
    current_raw = await upstash.get(key)
    current = float(current_raw) if current_raw is not None else 0.0
    remaining = caps.daily_ceiling_usd - current
    if projection_p95_usd <= remaining:
        return None
    return DenialReason(
        code=DenialCode.DAILY_CEILING,
        detail=(
            f"app:{caps.app} has ${remaining:.2f} remaining today; "
            f"this submit would reserve ${projection_p95_usd:.2f} "
            f"against ${caps.daily_ceiling_usd:.2f} ceiling "
            f"(${current:.2f} already reserved)"
        ),
        fix=(
            f"retry after midnight UTC, submit a smaller batch that "
            f"fits within ${remaining:.2f}, or raise "
            f"`daily_ceiling_usd` for app:{caps.app} in "
            f"config/apps.yaml with an ADR entry"
        ),
    )


async def _deny_if_monthly_ceiling_would_exceed(
    *,
    upstash: UpstashClient,
    caps: AppCaps,
    projection_p95_usd: float,
    now: datetime,
) -> DenialReason | None:
    # BUSINESS RULE: An app's cumulative monthly spend may not exceed
    # `monthly_ceiling_usd`. Counter resets at the start of the calendar
    # month.
    key = _buildMonthlyKey(app=caps.app, now=now)
    current_raw = await upstash.get(key)
    current = float(current_raw) if current_raw is not None else 0.0
    remaining = caps.monthly_ceiling_usd - current
    if projection_p95_usd <= remaining:
        return None
    return DenialReason(
        code=DenialCode.MONTHLY_CEILING,
        detail=(
            f"app:{caps.app} has ${remaining:.2f} remaining this month; "
            f"this submit would reserve ${projection_p95_usd:.2f} "
            f"against ${caps.monthly_ceiling_usd:.2f} ceiling "
            f"(${current:.2f} already reserved)"
        ),
        fix=(
            "wait for the monthly reset, split the work across months, "
            "or raise `monthly_ceiling_usd` in config/apps.yaml with "
            "an ADR entry"
        ),
    )


def _warn_if_cache_break_even(
    *,
    request_count: int,
    cache_mode: str,
    projection_cache_break_even_warning: bool,
) -> DecisionWarning | None:
    # BUSINESS RULE: When `cache_mode="1h"` and batch N < 3, the 1h write
    # multiplier (2.0x) costs more than no cache. Warning, not denial —
    # pre-warming larger follow-up batches is a legitimate use.
    if not projection_cache_break_even_warning and not (
        cache_mode == "1h" and request_count < CACHE_1H_BREAK_EVEN_MIN_N
    ):
        return None
    return DecisionWarning(
        code=WarningCode.CACHE_BREAK_EVEN_WARNING,
        detail=(
            f"cache_mode={cache_mode!r} at N={request_count} costs more "
            f"than no cache; the 1h write multiplier (2.0x) only "
            f"amortises favourably at N>={CACHE_1H_BREAK_EVEN_MIN_N}"
        ),
    )


# ─── Key builders ────────────────────────────────────────────────────────────
def _buildDailyKey(*, app: str, now: datetime) -> str:
    return (
        f"{UPSTASH_DAILY_KEY_PREFIX}{app}:daily:"
        f"{now.strftime('%Y%m%d')}"
    )


def _buildMonthlyKey(*, app: str, now: datetime) -> str:
    return (
        f"{UPSTASH_MONTHLY_KEY_PREFIX}{app}:monthly:"
        f"{now.strftime('%Y%m')}"
    )


# ─── Public entry point ──────────────────────────────────────────────────────
async def check_batch(
    *,
    app: str,
    requests: list[dict[str, Any]],
    model: str,
    projection: BatchProjection,
    fingerprint: str,
    caps: AppCaps,
    upstash: UpstashClient,
    ledger: SupabaseLedger,
    cache_mode: str = "none",
    actor: str | None = None,
    mode: GovernorMode = GovernorMode.ENFORCING,
    flag_sink: FlagSink | None = None,
    now: datetime | None = None,
) -> GatewayDecision:
    """Run the full denial pipeline and return APPROVED or DENIED.

    On APPROVED: reserves the app's P95 against daily + monthly counters,
    writes the approval hash with a 10-min TTL, claims the fingerprint key,
    and fires an async audit row. On DENIED: writes an audit row with the
    structured reasons and returns them to the caller.

    ``mode`` defaults to ENFORCING — the historical behavior. In PERMISSIVE
    mode a request that would be denied (for any reason except the emergency
    kill switch) is instead APPROVED, flagged via ``flag_sink``, and the
    would-be denial reasons are recorded on the APPROVED audit row.
    """
    resolved_now = now or datetime.now(timezone.utc)

    # Upstash unavailable means we cannot trust any counter; the gate fails
    # closed at this layer (ADR-005 says the wrapper is fail-open, but the
    # gate itself fails closed — those are compatible because an unreachable
    # gate also triggers the wrapper's fallback).
    try:
        reasons = await _collectDenialReasons(
            app=app,
            requests=requests,
            model=model,
            projection=projection,
            fingerprint=fingerprint,
            caps=caps,
            upstash=upstash,
            now=resolved_now,
        )
    except UpstashUnavailable as exc:
        denial = DenialReason(
            code=DenialCode.STATE_UNAVAILABLE,
            detail=f"governor state store unreachable: {exc}",
            fix=(
                "retry shortly; if Upstash is down the wrapper will "
                "fail-open to the raw SDK after a short backoff (see "
                "ADR-005)"
            ),
        )
        decision = DeniedDecision(reasons=[denial])
        await _writeDenialAuditRow(
            ledger=ledger,
            app=app,
            requests=requests,
            model=model,
            projection=projection,
            fingerprint=fingerprint,
            reasons=[denial],
            actor=actor,
        )
        return decision

    if reasons:
        # BUSINESS RULE: In permissive mode the gate becomes an auditor — a
        # request that enforcing would deny is APPROVED and flagged instead,
        # EXCEPT the emergency kill switch, which is never auto-approved.
        if mode is GovernorMode.PERMISSIVE and _isPermissivelyApprovable(reasons):
            flag = await _buildWouldDenyFlag(
                app=app,
                reasons=reasons,
                projection=projection,
                caps=caps,
                upstash=upstash,
                actor=actor,
                now=resolved_now,
            )
            if flag_sink is not None:
                flag_sink.emit(flag)
            return await _approve(
                app=app,
                requests=requests,
                model=model,
                projection=projection,
                fingerprint=fingerprint,
                caps=caps,
                upstash=upstash,
                ledger=ledger,
                cache_mode=cache_mode,
                actor=actor,
                now=resolved_now,
                flagged_reasons=[
                    reason.model_dump(mode="json") for reason in reasons
                ],
            )
        decision = DeniedDecision(reasons=reasons)
        await _writeDenialAuditRow(
            ledger=ledger,
            app=app,
            requests=requests,
            model=model,
            projection=projection,
            fingerprint=fingerprint,
            reasons=reasons,
            actor=actor,
        )
        return decision

    return await _approve(
        app=app,
        requests=requests,
        model=model,
        projection=projection,
        fingerprint=fingerprint,
        caps=caps,
        upstash=upstash,
        ledger=ledger,
        cache_mode=cache_mode,
        actor=actor,
        now=resolved_now,
    )


async def _collectDenialReasons(
    *,
    app: str,
    requests: list[dict[str, Any]],
    model: str,
    projection: BatchProjection,
    fingerprint: str,
    caps: AppCaps,
    upstash: UpstashClient,
    now: datetime,
) -> list[DenialReason]:
    """Run every rule helper, collecting the first denial each yields.

    We collect everything rather than early-return on the first denial so the
    caller sees every problem in one pass. Agents that violate multiple rules
    get actionable output instead of a painful one-at-a-time debugging loop.
    """
    del app  # reserved for future per-app overrides wired outside caps
    reasons: list[DenialReason] = []

    kill = await _deny_if_kill_switch_active(upstash=upstash)
    if kill is not None:
        # Kill switch short-circuits — every other rule is moot while halted.
        return [kill]

    size_reason = _deny_if_batch_size_exceeded(
        caps=caps, request_count=len(requests)
    )
    if size_reason is not None:
        reasons.append(size_reason)

    model_reason = _deny_if_model_not_allowed(caps=caps, model=model)
    if model_reason is not None:
        reasons.append(model_reason)

    custom_id_reason = _deny_if_missing_custom_id(requests=requests)
    if custom_id_reason is not None:
        reasons.append(custom_id_reason)

    fingerprint_reason = await _deny_if_duplicate_fingerprint(
        upstash=upstash, fingerprint=fingerprint
    )
    if fingerprint_reason is not None:
        reasons.append(fingerprint_reason)

    per_submit_reason = _deny_if_per_submit_cap_exceeded(
        caps=caps, projection_p95_usd=projection.p95_usd
    )
    if per_submit_reason is not None:
        reasons.append(per_submit_reason)

    daily_reason = await _deny_if_daily_ceiling_would_exceed(
        upstash=upstash,
        caps=caps,
        projection_p95_usd=projection.p95_usd,
        now=now,
    )
    if daily_reason is not None:
        reasons.append(daily_reason)

    monthly_reason = await _deny_if_monthly_ceiling_would_exceed(
        upstash=upstash,
        caps=caps,
        projection_p95_usd=projection.p95_usd,
        now=now,
    )
    if monthly_reason is not None:
        reasons.append(monthly_reason)

    return reasons


async def _approve(
    *,
    app: str,
    requests: list[dict[str, Any]],
    model: str,
    projection: BatchProjection,
    fingerprint: str,
    caps: AppCaps,
    upstash: UpstashClient,
    ledger: SupabaseLedger,
    cache_mode: str,
    actor: str | None,
    now: datetime,
    flagged_reasons: list[dict[str, Any]] | None = None,
) -> ApprovedDecision:
    """Reserve the budget, write the approval, and return the decision.

    ``flagged_reasons`` is set only on the permissive-mode would-deny path: the
    would-be denial reasons are recorded on the APPROVED audit row's ``reasons``
    column so the ledger shows what enforcing mode would have blocked. In
    enforcing mode this is ``None`` and the row is byte-for-byte as before.
    """
    approval_id = str(uuid.uuid4())
    expires_at = now + timedelta(seconds=APPROVAL_TTL_SECONDS)
    daily_key = _buildDailyKey(app=app, now=now)
    monthly_key = _buildMonthlyKey(app=app, now=now)
    approval_key = f"{UPSTASH_APPROVAL_KEY_PREFIX}{approval_id}"
    fingerprint_key = f"{UPSTASH_FINGERPRINT_KEY_PREFIX}{fingerprint}"

    # BUSINESS RULE: Budget reservation must be atomic against other submits.
    # We push everything into one Upstash pipeline so the daily/monthly
    # counters move in lockstep with the approval hash and fingerprint claim.
    await upstash.pipeline([
        ["HSET", approval_key,
            "app", app,
            "model", model,
            "fingerprint", fingerprint,
            "p95_usd", f"{projection.p95_usd:.4f}",
            "requests", str(len(requests)),
            "issued_at", now.isoformat(),
            "expires_at", expires_at.isoformat()],
        ["EXPIRE", approval_key, APPROVAL_TTL_SECONDS],
        ["INCRBYFLOAT", daily_key, f"{projection.p95_usd:.4f}"],
        ["EXPIRE", daily_key, DAILY_COUNTER_TTL_SECONDS],
        ["INCRBYFLOAT", monthly_key, f"{projection.p95_usd:.4f}"],
        ["EXPIRE", monthly_key, MONTHLY_COUNTER_TTL_SECONDS],
        ["SET", fingerprint_key, approval_id,
            "EX", FINGERPRINT_TTL_SECONDS, "NX"],
    ])

    warning = _warn_if_cache_break_even(
        request_count=len(requests),
        cache_mode=cache_mode,
        projection_cache_break_even_warning=getattr(
            projection, "cache_break_even_warning", False
        ),
    )
    warnings = [warning] if warning is not None else []

    await ledger.write_decision(DecisionRecord(
        app=app,
        decision="APPROVED",
        reasons=flagged_reasons,
        fingerprint=fingerprint,
        items=len(requests),
        model=model,
        projected_p50_usd=getattr(projection, "p50_usd", None),
        projected_p95_usd=projection.p95_usd,
        approval_id=approval_id,
        actor=actor,
    ))

    # Spend `caps` so mypy does not mark it unused in this function — the
    # approval path does not need anything off the caps object beyond what
    # the denial helpers already validated.
    del caps

    return ApprovedDecision(
        approval_id=approval_id,
        p95_usd=projection.p95_usd,
        expires_at=expires_at,
        warnings=warnings,
    )


async def _writeDenialAuditRow(
    *,
    ledger: SupabaseLedger,
    app: str,
    requests: list[dict[str, Any]],
    model: str,
    projection: BatchProjection,
    fingerprint: str,
    reasons: list[DenialReason],
    actor: str | None,
) -> None:
    """Fire-and-forget DENIED audit write."""
    await ledger.write_decision(DecisionRecord(
        app=app,
        decision="DENIED",
        reasons=[reason.model_dump(mode="json") for reason in reasons],
        fingerprint=fingerprint,
        items=len(requests),
        model=model,
        projected_p50_usd=getattr(projection, "p50_usd", None),
        projected_p95_usd=getattr(projection, "p95_usd", None),
        approval_id=None,
        actor=actor,
    ))


# ─── Permissive-mode flag helpers ────────────────────────────────────────────
def _isPermissivelyApprovable(reasons: list[DenialReason]) -> bool:
    # BUSINESS RULE: Permissive mode approves+flags budget/policy denials, but
    # the kill switch is an operator emergency halt — it is NEVER auto-approved.
    # (The kill switch short-circuits ``_collectDenialReasons`` to a lone
    # KILL_SWITCH_ACTIVE reason, so this correctly denies it.)
    return all(
        reason.code is not DenialCode.KILL_SWITCH_ACTIVE for reason in reasons
    )


async def _buildWouldDenyFlag(
    *,
    app: str,
    reasons: list[DenialReason],
    projection: BatchProjection,
    caps: AppCaps,
    upstash: UpstashClient,
    actor: str | None,
    now: datetime,
) -> FlagEvent:
    """Assemble a ``would_deny`` flag: projection + budget state + overage.

    Re-reads the daily/monthly counters so the flag captures the quota state at
    decision time. Reads are guarded — an outage yields a zero-budget snapshot
    rather than aborting the flag.
    """
    daily_reserved = await _readFloatSafe(
        upstash, _buildDailyKey(app=app, now=now)
    )
    monthly_reserved = await _readFloatSafe(
        upstash, _buildMonthlyKey(app=app, now=now)
    )
    projection_p95 = projection.p95_usd
    budget_state: dict[str, Any] = {
        "daily": {
            "reserved_usd": round(daily_reserved, 4),
            "ceiling_usd": caps.daily_ceiling_usd,
            "remaining_usd": round(caps.daily_ceiling_usd - daily_reserved, 4),
        },
        "monthly": {
            "reserved_usd": round(monthly_reserved, 4),
            "ceiling_usd": caps.monthly_ceiling_usd,
            "remaining_usd": round(
                caps.monthly_ceiling_usd - monthly_reserved, 4
            ),
        },
    }
    overage_usd = _computeOverage(
        reasons=reasons,
        projection_p95=projection_p95,
        caps=caps,
        daily_reserved=daily_reserved,
        monthly_reserved=monthly_reserved,
    )
    return FlagEvent(
        kind="would_deny",
        ts=now,
        app=app,
        actor=actor,
        projected_p50_usd=getattr(projection, "p50_usd", None),
        projected_p95_usd=projection_p95,
        budget_state=budget_state,
        overage_usd=overage_usd,
        would_deny_reasons=[reason.model_dump(mode="json") for reason in reasons],
    )


def _computeOverage(
    *,
    reasons: list[DenialReason],
    projection_p95: float,
    caps: AppCaps,
    daily_reserved: float,
    monthly_reserved: float,
) -> float | None:
    """Return the largest amount by which the projection exceeds a cap/ceiling.

    Only spend-cap denials contribute (per-submit, daily, monthly). Structural
    denials (batch size, model, custom_id, duplicate) have no dollar overage, so
    a flag driven purely by those returns ``None``.
    """
    candidates: list[float] = []
    for reason in reasons:
        if reason.code is DenialCode.PER_SUBMIT_CAP:
            candidates.append(projection_p95 - caps.per_submit_cap_usd)
        elif reason.code is DenialCode.DAILY_CEILING:
            candidates.append(
                projection_p95 - (caps.daily_ceiling_usd - daily_reserved)
            )
        elif reason.code is DenialCode.MONTHLY_CEILING:
            candidates.append(
                projection_p95 - (caps.monthly_ceiling_usd - monthly_reserved)
            )
    positive = [round(value, 4) for value in candidates if value > 0]
    if not positive:
        return None
    return max(positive)


async def _readFloatSafe(upstash: UpstashClient, key: str) -> float:
    """Read a float counter, degrading to 0.0 on miss or Upstash outage."""
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
