"""Thin wrapper around the Anthropic Python SDK.

Only the governor service itself imports this module. App code never calls
Anthropic directly — it goes through the wrapper libraries under
``clients/python`` and ``clients/ts``. This wrapper is where the governor
actually reaches Anthropic for:

* ``list_batches`` — used by the monitor to find rogue batches.
* ``retrieve_batch`` — used by the monitor to poll progress.
* ``cancel_batch`` — used by monitor cost-cap breach and operator kill.
* ``create_batch`` — called ONLY from ``POST /v1/submit`` after the gate has
  returned APPROVED. Never called from calculator or monitor paths.
* ``count_tokens`` — used by the calculator for its estimator.

# AI: Every method below makes a real Anthropic API call. Failures are
# retried with exponential backoff on 5xx; 4xx errors surface immediately so
# the caller can map them to a specific denial or alert.
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import Any, Final

from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
)


# ─── Constants ───────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY_ENV: Final[str] = "ANTHROPIC_API_KEY"
ANTHROPIC_MAX_RETRIES: Final[int] = 4
ANTHROPIC_INITIAL_BACKOFF_SECONDS: Final[float] = 0.5
ANTHROPIC_MAX_BACKOFF_SECONDS: Final[float] = 8.0
ANTHROPIC_BATCH_LIST_DEFAULT_LIMIT: Final[int] = 100


# ─── Exceptions ──────────────────────────────────────────────────────────────
class AnthropicConfigError(RuntimeError):
    """Raised when ``ANTHROPIC_API_KEY`` is missing at construction time."""


class AnthropicUnavailable(RuntimeError):
    """Raised when Anthropic repeatedly returns 5xx or times out."""


# ─── Retry helper ────────────────────────────────────────────────────────────
async def _runWithRetry(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run an awaitable with exponential-backoff retry on 5xx / timeouts.

    Only retries ``APIStatusError`` with status >= 500, ``APIConnectionError``,
    and ``APITimeoutError``. 4xx errors surface immediately because they are
    not transient — a 400 batch is still a 400 after five seconds.
    """
    delay = ANTHROPIC_INITIAL_BACKOFF_SECONDS
    last_exc: Exception | None = None
    for attempt in range(ANTHROPIC_MAX_RETRIES + 1):
        try:
            return await func(*args, **kwargs)
        except APIStatusError as exc:
            if exc.status_code < 500:
                raise
            last_exc = exc
        except (APIConnectionError, APITimeoutError) as exc:
            last_exc = exc

        if attempt == ANTHROPIC_MAX_RETRIES:
            break
        jitter = random.uniform(0.0, 0.25 * delay)
        await asyncio.sleep(min(delay + jitter, ANTHROPIC_MAX_BACKOFF_SECONDS))
        delay = min(delay * 2, ANTHROPIC_MAX_BACKOFF_SECONDS)

    raise AnthropicUnavailable(
        f"Anthropic unreachable after {ANTHROPIC_MAX_RETRIES + 1} attempts: "
        f"{last_exc!r}"
    )


# ─── Client ──────────────────────────────────────────────────────────────────
class AnthropicClient:
    """Wrapper around ``anthropic.AsyncAnthropic`` exposing only governor needs.

    The wrapper intentionally does not expose ``messages.create`` — batches
    only, per ADR-006. Single-shot callers in apps use the raw SDK directly
    because single-shot cost is bounded by per-request limits, not by batch
    orchestration.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        sdk_client: AsyncAnthropic | None = None,
    ) -> None:
        resolved_key = api_key if api_key is not None else os.environ.get(
            ANTHROPIC_API_KEY_ENV, ""
        )
        if sdk_client is None and not resolved_key:
            raise AnthropicConfigError(
                f"AnthropicClient requires ${ANTHROPIC_API_KEY_ENV} to be set."
            )
        self._sdk = sdk_client or AsyncAnthropic(api_key=resolved_key)

    # ─── Batch lifecycle ─────────────────────────────────────────────────────
    async def list_batches(
        self,
        limit: int = ANTHROPIC_BATCH_LIST_DEFAULT_LIMIT,
    ) -> list[Any]:
        """List recent Message Batches. Used by the monitor's rogue sweep.

        Returns the SDK's native batch objects so the caller can read both
        ``id`` and ``processing_status`` without a second round-trip.
        """
        page = await _runWithRetry(
            self._sdk.messages.batches.list, limit=limit
        )
        return list(page.data)

    async def retrieve_batch(self, batch_id: str) -> Any:
        """Retrieve a single batch by id."""
        return await _runWithRetry(
            self._sdk.messages.batches.retrieve, batch_id
        )

    async def cancel_batch(self, batch_id: str) -> Any:
        """Cancel a batch. Called by monitor cost-cap and operator kill."""
        return await _runWithRetry(
            self._sdk.messages.batches.cancel, batch_id
        )

    async def create_batch(
        self,
        *,
        requests: list[dict[str, Any]],
        model: str,
    ) -> Any:
        """Submit a batch. ONLY called from POST /v1/submit post-approval.

        ``model`` is accepted as a sibling parameter even though it is already
        inside each request; this signature matches what the gate validates
        and lets the governor log the model alongside the batch id on
        lifecycle writes.
        """
        del model  # BUSINESS RULE: model lives inside each request; sibling
        # parameter is retained for logging/validation only and is not
        # forwarded to the SDK — the SDK rejects unknown top-level keys.
        return await _runWithRetry(
            self._sdk.messages.batches.create, requests=requests
        )

    # ─── Token counting ──────────────────────────────────────────────────────
    async def count_tokens(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
    ) -> int:
        """Return Anthropic's authoritative token count for a message set.

        This is the calculator's source of truth. The calculator falls back to
        a character heuristic only when this call fails; see ADR-005.
        """
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if system is not None:
            kwargs["system"] = system
        response = await _runWithRetry(
            self._sdk.messages.count_tokens, **kwargs
        )
        return int(response.input_tokens)
