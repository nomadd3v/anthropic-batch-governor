"""Drop-in Anthropic SDK wrappers that route batches through the governor.

Import this package instead of :mod:`anthropic` in any code that creates
batches. The public surface is identical to :mod:`anthropic` for every
non-batch path; only ``messages.batches.create`` is overridden to first
consult the governor at ``GOVERNOR_BASE_URL``.

Typical usage::

    from governor import Anthropic

    client = Anthropic(app="example-app")
    batch = client.messages.batches.create(requests=[...])

Fail-open behaviour (ADR-005): when the governor is unreachable, the
wrapper logs a ``[BATCH-GOVERNOR-FALLBACK]`` warning, fires an optional
``on_fallback`` callback, and falls through to the raw Anthropic SDK.
The monitor (Stream E) will still catch rogue batches in the common case.
"""

from __future__ import annotations

from .client import (
    Anthropic,
    AsyncAnthropic,
    resolveActor,
    buildBatchFingerprint,
    fireFallbackAlert,
    DEFAULT_GOVERNOR_BASE_URL,
    GOVERNOR_BASE_URL_ENV,
    PUSH_NOTIFICATION_TOPIC,
)
from .exceptions import (
    DenialReasonDict,
    FallbackInfo,
    GovernorDenied,
    GovernorUnreachable,
)

__all__ = [
    "Anthropic",
    "AsyncAnthropic",
    "GovernorDenied",
    "GovernorUnreachable",
    "DenialReasonDict",
    "FallbackInfo",
    "resolveActor",
    "buildBatchFingerprint",
    "fireFallbackAlert",
    "DEFAULT_GOVERNOR_BASE_URL",
    "GOVERNOR_BASE_URL_ENV",
    "PUSH_NOTIFICATION_TOPIC",
]
