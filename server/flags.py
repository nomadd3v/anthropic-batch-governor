"""Flag stream for permissive-mode audit events.

The governor can run in two modes (see :class:`server.gate.GovernorMode`):

* ``enforcing`` — the historical gatekeeper. Requests that break a rule are
  DENIED. Behavior is byte-for-byte unchanged from before this module existed.
* ``permissive`` — auditor mode. Requests that *would* be denied are instead
  APPROVED and **flagged for review**. The flag stream is a durable log that
  feeds the operator's mining pipeline.

Two flag kinds share one shape (:class:`FlagEvent`):

1. ``would_deny`` — a request approved under permissive mode that enforcing
   would have denied. Records the projection, the app's budget/quota state, the
   overage amount, and the denial reasons it *would* have given.
2. ``projection_mismatch`` — reconciliation found actual spend diverging from
   the projection beyond the existing tolerance. Records projection, actual,
   delta, and ratio. This fires in BOTH modes: reconciliation is observation,
   not gating.

Sinks (both, for every flag):

* **Append-only JSONL** at :data:`DEFAULT_FLAGS_PATH` (override with
  ``GOVERNOR_FLAGS_PATH``). This file is what gets rsynced to the mining box, so
  the path is stable and the format is one JSON object per line.
* **The Supabase ``governor`` audit ledger** — handled by the callers via the
  established insert pattern (``write_decision`` embeds the ``would_deny``
  reasons on the APPROVED row's ``reasons`` jsonb column; the reconcile paths
  already write ``variance_pct`` to ``governor.batches``). This module owns only
  the JSONL sink and the in-memory counters; keeping the Supabase writes in the
  callers means they stay on the same fire-and-forget path as the current audit
  writes (never on the gate's hot path).

The JSONL sink is resilient: a write failure logs a warning and never raises, so
flagging can never break the request path.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, Field


# ─── Constants ───────────────────────────────────────────────────────────────
FLAGS_PATH_ENV: Final[str] = "GOVERNOR_FLAGS_PATH"
DEFAULT_FLAGS_PATH: Final[str] = "~/.governor/flags.jsonl"

# The reconciliation tolerance beyond which a projection/actual divergence is a
# flaggable ``projection_mismatch``. Mirrors
# ``server.monitor.VARIANCE_STALE_THRESHOLD`` (0.10) — the variance threshold
# that surfaced the 2026-04-15 miscalibration. Kept here as the flag domain's
# canonical value so ``server.app``'s reconcile path does not import the monitor.
PROJECTION_MISMATCH_TOLERANCE_RATIO: Final[float] = 0.10

FlagKind = Literal["would_deny", "projection_mismatch"]

_logger = logging.getLogger(__name__)


# ─── Flag event ──────────────────────────────────────────────────────────────
class FlagEvent(BaseModel):
    """One flag record. Both kinds share this shape; unused fields stay ``None``.

    Serialized to JSONL with ``exclude_none=True`` so each line only carries the
    fields relevant to its ``kind``.
    """

    kind: FlagKind
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    app: str
    actor: str | None = None

    # ─── would_deny fields ───────────────────────────────────────────────────
    projected_p50_usd: float | None = None
    projected_p95_usd: float | None = None
    budget_state: dict[str, Any] | None = None
    overage_usd: float | None = None
    would_deny_reasons: list[dict[str, Any]] | None = None

    # ─── projection_mismatch fields ──────────────────────────────────────────
    batch_id: str | None = None
    projected_usd: float | None = None
    actual_usd: float | None = None
    delta_usd: float | None = None
    ratio: float | None = None
    tolerance: float | None = None

    def to_jsonl_line(self) -> str:
        """Render this event as one compact JSON line (no trailing newline)."""
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True), default=str
        )


# ─── Flag sink ───────────────────────────────────────────────────────────────
class FlagSink:
    """Append-only JSONL sink plus in-memory per-kind counters.

    One instance lives on the service's ``AppState`` (and one in the monitor
    process). ``emit`` bumps the counter and appends a line; the counters back
    the ``flags_since_start`` field the status endpoint reports so an operator
    can never miss that the node is running permissive.
    """

    def __init__(self, *, path: Path) -> None:
        self._path = path
        self._counts: dict[str, int] = {
            "would_deny": 0,
            "projection_mismatch": 0,
        }
        self._ensure_parent()

    @classmethod
    def fromEnv(cls) -> "FlagSink":
        """Build from ``GOVERNOR_FLAGS_PATH`` (default ``~/.governor/flags.jsonl``)."""
        raw = os.environ.get(FLAGS_PATH_ENV, DEFAULT_FLAGS_PATH)
        return cls(path=Path(raw).expanduser())

    def _ensure_parent(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _logger.warning(
                "could not create flag dir %s: %s", self._path.parent, exc
            )

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, event: FlagEvent) -> None:
        """Count and append a flag. Never raises — a broken sink must not break
        the request path."""
        self._counts[event.kind] = self._counts.get(event.kind, 0) + 1
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(event.to_jsonl_line() + "\n")
        except OSError as exc:
            _logger.warning("could not append flag to %s: %s", self._path, exc)

    def counts(self) -> dict[str, int]:
        """Return a copy of the per-kind counters since process start."""
        return dict(self._counts)

    @property
    def total(self) -> int:
        return sum(self._counts.values())
