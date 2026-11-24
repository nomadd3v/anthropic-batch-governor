"""Custom exceptions raised by the ``governor`` drop-in SDK wrapper.

Two shapes matter to callers:

* :class:`GovernorDenied` — the pre-submit gate refused the batch. The
  caller can inspect ``reasons`` to see every rule that failed. This is a
  blocking error: the batch was **not** submitted to Anthropic.
* :class:`GovernorUnreachable` — the governor service itself is down or
  unreachable. The wrapper catches this internally and falls through to
  the raw SDK per ADR-005 (fail-open). Callers generally do not see
  this; it is exported so tests and custom callbacks can introspect it.

The shapes are intentionally thin: Stream B owns the authoritative
``DenialReason`` pydantic model in :mod:`server.gate`; we mirror its
wire shape as a ``TypedDict`` so clients do not need a pydantic
dependency.
"""

from __future__ import annotations

from typing import Any, TypedDict


class DenialReasonDict(TypedDict):
    """Wire-shape mirror of :class:`server.gate.DenialReason`.

    Matches the JSON returned by ``POST /v1/check`` when the decision is
    ``DENIED``. The fields are copied verbatim from the server model —
    see ``server/gate.py`` for the source of truth.
    """

    code: str
    detail: str
    fix: str


class GovernorDenied(Exception):
    """Raised when the governor's pre-submit gate denies a batch.

    Inspect :attr:`reasons` to see every rule that failed; each reason
    carries ``code``, ``detail``, and a ``fix`` hint written for a human
    reader. The batch was **not** submitted to Anthropic.
    """

    def __init__(
        self,
        reasons: list[DenialReasonDict],
        *,
        app: str | None = None,
        fingerprint: str | None = None,
    ) -> None:
        self.reasons: list[DenialReasonDict] = list(reasons)
        self.app = app
        self.fingerprint = fingerprint
        detail = "; ".join(
            f"{reason.get('code', 'UNKNOWN')}: {reason.get('detail', '')}"
            for reason in reasons
        )
        super().__init__(
            f"batch-governor denied submit (app={app}): {detail}"
        )


class GovernorUnreachable(Exception):
    """Raised when the governor's HTTP service is unreachable.

    The wrapper catches this internally per ADR-005 and falls through to
    the raw Anthropic SDK. Exposed publicly so tests and the optional
    ``on_fallback`` callback can pattern-match on the cause.
    """

    def __init__(self, reason: str, *, cause: BaseException | None = None) -> None:
        self.reason = reason
        self.cause = cause
        super().__init__(f"batch-governor unreachable: {reason}")


class FallbackInfo(TypedDict):
    """Payload handed to ``on_fallback`` when the wrapper fails open.

    Contains just enough information for a PushNotification payload —
    which is what Stream F will wire this to.
    """

    app: str
    fingerprint: str
    reason: str
    topic: str
    extra: dict[str, Any]
