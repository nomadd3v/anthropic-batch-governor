"""Drop-in Anthropic SDK subclasses that route batch creates through the governor.

Usage:

    from governor import Anthropic   # instead of: from anthropic import Anthropic

    client = Anthropic(app="example-app")
    batch = client.messages.batches.create(requests=[...])

Every non-batch call (``messages.create``, ``messages.count_tokens``, etc.)
passes straight through to the real SDK unchanged — the subclass only
wraps :meth:`messages.batches.create`. The override does three HTTP hops:

1. ``POST /calc/project-batch``  — ask the governor to price this batch.
2. ``POST /v1/submit``           — gate + submit atomically. The governor
   runs the full pre-submit rule engine, reserves budget, and then calls
   Anthropic's batches API itself. On APPROVED the response carries
   ``batch_id`` + ``approval_id``; on DENIED it carries the list of
   structured denial reasons the gate produced.
3. ``GET /v1/batch/{id}``        — (not wired yet) callers can poll via
   the raw SDK's ``retrieve`` since non-batch methods pass through.

The submit path is NOT driven from the governor's own ``/v1/check`` +
``/v1/submit`` sequence because that flow assumes the caller already
stashed the payload server-side. Stream D's contract with the gate is:
client ships the full payload to the governor, governor gates, governor
submits. ADR-004's call graph reflects that.

**Fail-open (ADR-005).** When the governor is unreachable — timeout,
connection refused, or 5xx — the wrapper:

* Logs a warning with a ``[BATCH-GOVERNOR-FALLBACK]`` prefix.
* Invokes the optional ``on_fallback`` callback (where Stream F will
  attach a real ``PushNotification`` on topic ``batch-governor-unreachable``).
* Falls through to ``super().messages.batches.create(...)`` so legitimate
  customer work does not block on a governor outage.

This mirrors the TypeScript sibling in ``clients/ts/governor/src/index.ts``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Final, Iterable

import httpx
from anthropic import Anthropic as _RawAnthropic
from anthropic import AsyncAnthropic as _RawAsyncAnthropic

from .exceptions import (
    DenialReasonDict,
    FallbackInfo,
    GovernorDenied,
    GovernorUnreachable,
)


# ─── Constants ──────────────────────────────────────────────────────────────
DEFAULT_GOVERNOR_BASE_URL: Final[str] = "http://127.0.0.1:5185"
GOVERNOR_BASE_URL_ENV: Final[str] = "GOVERNOR_BASE_URL"
GOVERNOR_ACTOR_ENV: Final[str] = "GOVERNOR_ACTOR"
GOVERNOR_TIMEOUT_SECONDS: Final[float] = 8.0
FALLBACK_LOG_PREFIX: Final[str] = "[BATCH-GOVERNOR-FALLBACK]"
PUSH_NOTIFICATION_TOPIC: Final[str] = "batch-governor-unreachable"

FallbackCallback = Callable[[FallbackInfo], None]


_logger = logging.getLogger("governor.client")


# ─── Helper: fingerprint ────────────────────────────────────────────────────
def buildBatchFingerprint(
    *, requests: list[dict[str, Any]], model: str
) -> str:
    """Return a stable sha256 of the request set + model.

    The fingerprint is used by the gate's ``_deny_if_duplicate_fingerprint``
    helper — see ``server/gate.py`` — so clients and server must agree on
    how it is built. We hash:

    1. Each request's ``custom_id`` sorted alphabetically.
    2. A SHA of each request's body (JSON-canonicalised).
    3. The model string.

    Two batches that submit the same work to the same model produce the
    same fingerprint, regardless of Python dict ordering.
    """
    entries: list[tuple[str, str]] = []
    for request in requests:
        custom_id = str(request.get("custom_id", ""))
        params = request.get("params", {})
        params_json = json.dumps(params, sort_keys=True, default=str)
        params_hash = hashlib.sha256(params_json.encode("utf-8")).hexdigest()
        entries.append((custom_id, params_hash))

    entries.sort(key=lambda pair: pair[0])
    payload = json.dumps(
        {"model": model, "entries": entries}, sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ─── Helper: actor resolution ───────────────────────────────────────────────
def resolveActor() -> str:
    """Return the caller identity for audit rows.

    Priority: explicit ``GOVERNOR_ACTOR`` env var, then ``$USER@hostname``.
    This mirrors how the bash CLI resolves the actor in `scripts/batch-governor`
    — keeping client- and human-driven audit rows comparable.
    """
    explicit = os.environ.get(GOVERNOR_ACTOR_ENV)
    if explicit:
        return explicit
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    try:
        host = socket.gethostname()
    except OSError:
        host = "unknown-host"
    return f"{user}@{host}"


# ─── Helper: fallback fire-and-log ──────────────────────────────────────────
def fireFallbackAlert(
    *,
    app: str,
    fingerprint: str,
    reason: str,
    callback: FallbackCallback | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Log the fallback + invoke the optional callback.

    Stream F will pass ``on_fallback=<lambda: PushNotification(...)>`` to
    fan this into the real ops channel. Until then the stderr log ensures
    every fallback is observable in CI + local runs.
    """
    extra_payload: dict[str, Any] = dict(extra) if extra else {}
    extra_payload.setdefault(
        "timestamp", datetime.now(timezone.utc).isoformat()
    )
    message = (
        f"{FALLBACK_LOG_PREFIX} app={app} fingerprint={fingerprint[:12]} "
        f"reason={reason}"
    )
    # BUSINESS RULE: every fallback must leave a trace — stderr in the
    # absence of a callback, so CI logs carry evidence even when no one
    # wired PushNotification yet (ADR-005).
    _logger.warning(message)
    if callback is None:
        print(message, file=sys.stderr, flush=True)
        return
    info: FallbackInfo = {
        "app": app,
        "fingerprint": fingerprint,
        "reason": reason,
        "topic": PUSH_NOTIFICATION_TOPIC,
        "extra": extra_payload,
    }
    try:
        callback(info)
    except Exception as exc:  # noqa: BLE001 — callback errors must not block
        _logger.warning(
            "%s on_fallback callback raised: %s", FALLBACK_LOG_PREFIX, exc
        )


# ─── Helper: governor HTTP transport (sync) ─────────────────────────────────
def _readDenialReasons(payload: Any) -> list[DenialReasonDict]:
    """Project the governor's DENIED JSON onto ``DenialReasonDict``."""
    raw_reasons = payload.get("reasons") if isinstance(payload, dict) else None
    if not isinstance(raw_reasons, list):
        return []
    reasons: list[DenialReasonDict] = []
    for entry in raw_reasons:
        if not isinstance(entry, dict):
            continue
        reasons.append({
            "code": str(entry.get("code", "UNKNOWN")),
            "detail": str(entry.get("detail", "")),
            "fix": str(entry.get("fix", "")),
        })
    return reasons


def _buildMessageBatch(
    *, batch_id: str, now_iso: str | None = None
) -> Any:
    """Return a ``MessageBatch``-shaped object the caller can treat normally.

    We intentionally do NOT import :class:`anthropic.types.messages.MessageBatch`
    in this module's top-level so the wrapper can be used in test rigs
    that stub out the SDK. The helper constructs the real type lazily
    and falls back to a lightweight dict when the type is not available
    (unit tests use the dict path).
    """
    try:
        from anthropic.types.messages import (  # noqa: PLC0415
            MessageBatch,
            MessageBatchRequestCounts,
        )
    except ImportError:
        return {
            "id": batch_id,
            "type": "message_batch",
            "processing_status": "in_progress",
            "created_at": now_iso,
            "expires_at": now_iso,
            "request_counts": {
                "processing": 0,
                "succeeded": 0,
                "errored": 0,
                "canceled": 0,
                "expired": 0,
            },
        }
    stamp = now_iso or datetime.now(timezone.utc).isoformat()
    return MessageBatch(
        id=batch_id,
        type="message_batch",
        processing_status="in_progress",
        created_at=stamp,  # type: ignore[arg-type]
        expires_at=stamp,  # type: ignore[arg-type]
        request_counts=MessageBatchRequestCounts(
            processing=0,
            succeeded=0,
            errored=0,
            canceled=0,
            expired=0,
        ),
    )


# ─── Helper: projection + submit HTTP calls ─────────────────────────────────
def _buildProjectionPayload(
    *, requests: list[dict[str, Any]], model: str
) -> dict[str, Any]:
    """Return the ``/calc/project-batch`` body.

    We approximate per-request input tokens using a 4-char-per-token
    heuristic — the calculator does its own real estimation internally,
    but the wrapper has no reason to pre-count accurately. The governor's
    /calc path will re-estimate if needed.
    """
    items = []
    for request in requests:
        params = request.get("params", {}) if isinstance(request, dict) else {}
        messages = params.get("messages", []) if isinstance(params, dict) else []
        approximate_chars = sum(
            len(str(message)) for message in messages if message is not None
        )
        approximate_tokens = max(1, approximate_chars // 4)
        items.append({"unique_input_tokens": approximate_tokens})
    return {
        "requests": items,
        "shared_system_tokens": 0,
        "cache_mode": "none",
        "model": model,
        "operation": "wrapper.batch.create",
    }


def _buildSubmitPayload(
    *,
    app: str,
    requests: list[dict[str, Any]],
    model: str,
    projection: dict[str, Any],
    fingerprint: str,
    actor: str,
) -> dict[str, Any]:
    """Build the ``POST /v1/submit`` body per Stream D contract.

    Note: the governor's ``/v1/submit`` route currently accepts ``approval_id``
    only — the wrapper/governor atomic-submit contract documented in ADR-004
    lands in a follow-up server patch. For now we piggyback on ``/v1/check``
    to get an approval, then call ``/v1/submit`` with that id. This helper
    just shapes the combined payload we carry internally between the two
    hops.
    """
    return {
        "app": app,
        "requests": requests,
        "model": model,
        "projection": projection,
        "fingerprint": fingerprint,
        "actor": actor,
    }


# ─── Internal: the actual gate + submit runner ──────────────────────────────
class _GovernorHttp:
    """Thin wrapper around the governor HTTP endpoints.

    Exposes both sync and async variants because callers might be using
    ``Anthropic`` (sync) or ``AsyncAnthropic`` (async). Each variant calls
    ``/calc/project-batch`` → ``/v1/check`` → ``/v1/submit`` in sequence.
    """

    def __init__(self, base_url: str, timeout: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # BUSINESS RULE: every HTTP failure to the governor triggers fail-open
    # (ADR-005). Raise :class:`GovernorUnreachable` here; the caller
    # catches it and falls through to the raw SDK.
    def _raiseUnreachable(self, exc: BaseException, step: str) -> None:
        raise GovernorUnreachable(
            f"{step} failed: {exc}", cause=exc
        ) from exc

    # ─── Sync flow ───────────────────────────────────────────────────────
    def projectAndSubmit(
        self,
        *,
        app: str,
        requests: list[dict[str, Any]],
        model: str,
        actor: str,
    ) -> dict[str, Any]:
        """Run the three-hop governor flow and return the submit response.

        Raises :class:`GovernorDenied` on DENIED, :class:`GovernorUnreachable`
        on network or 5xx failure.
        """
        fingerprint = buildBatchFingerprint(requests=requests, model=model)
        with httpx.Client(
            base_url=self._base_url, timeout=self._timeout
        ) as http:
            projection = self._postProjection(http, requests, model)
            approval_id = self._postCheck(
                http,
                app=app,
                requests=requests,
                model=model,
                projection=projection,
                fingerprint=fingerprint,
                actor=actor,
            )
            batch_id = self._postSubmit(http, approval_id=approval_id)
        return {
            "batch_id": batch_id,
            "approval_id": approval_id,
            "fingerprint": fingerprint,
        }

    def _postProjection(
        self,
        http: httpx.Client,
        requests: list[dict[str, Any]],
        model: str,
    ) -> dict[str, Any]:
        payload = _buildProjectionPayload(requests=requests, model=model)
        try:
            response = http.post("/calc/project-batch", json=payload)
        except httpx.HTTPError as exc:
            self._raiseUnreachable(exc, "calc/project-batch")
        if response.status_code >= 500:
            self._raiseUnreachable(
                RuntimeError(f"status={response.status_code}"),
                "calc/project-batch",
            )
        if response.status_code >= 400:
            raise GovernorUnreachable(
                f"projection rejected: {response.status_code} {response.text}"
            )
        parsed: dict[str, Any] = response.json()
        return parsed

    def _postCheck(
        self,
        http: httpx.Client,
        *,
        app: str,
        requests: list[dict[str, Any]],
        model: str,
        projection: dict[str, Any],
        fingerprint: str,
        actor: str,
    ) -> str:
        body = _buildSubmitPayload(
            app=app,
            requests=requests,
            model=model,
            projection=projection,
            fingerprint=fingerprint,
            actor=actor,
        )
        try:
            response = http.post("/v1/check", json=body)
        except httpx.HTTPError as exc:
            self._raiseUnreachable(exc, "v1/check")
        if response.status_code >= 500:
            self._raiseUnreachable(
                RuntimeError(f"status={response.status_code}"), "v1/check"
            )
        if response.status_code >= 400:
            raise GovernorUnreachable(
                f"check rejected: {response.status_code} {response.text}"
            )
        data = response.json()
        if data.get("decision") == "DENIED":
            raise GovernorDenied(
                _readDenialReasons(data), app=app, fingerprint=fingerprint
            )
        approval_id = data.get("approval_id")
        if not isinstance(approval_id, str):
            raise GovernorUnreachable(
                f"check returned APPROVED without approval_id: {data!r}"
            )
        return approval_id

    def _postSubmit(
        self, http: httpx.Client, *, approval_id: str
    ) -> str:
        try:
            response = http.post(
                "/v1/submit", json={"approval_id": approval_id}
            )
        except httpx.HTTPError as exc:
            self._raiseUnreachable(exc, "v1/submit")
        if response.status_code >= 500:
            self._raiseUnreachable(
                RuntimeError(f"status={response.status_code}"), "v1/submit"
            )
        if response.status_code >= 400:
            raise GovernorUnreachable(
                f"submit rejected: {response.status_code} {response.text}"
            )
        data = response.json()
        batch_id = data.get("batch_id")
        if not isinstance(batch_id, str):
            raise GovernorUnreachable(
                f"submit returned without batch_id: {data!r}"
            )
        return batch_id

    # ─── Async flow ─────────────────────────────────────────────────────
    async def projectAndSubmitAsync(
        self,
        *,
        app: str,
        requests: list[dict[str, Any]],
        model: str,
        actor: str,
    ) -> dict[str, Any]:
        """Async variant of :meth:`projectAndSubmit`."""
        fingerprint = buildBatchFingerprint(requests=requests, model=model)
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout
        ) as http:
            projection = await self._postProjectionAsync(http, requests, model)
            approval_id = await self._postCheckAsync(
                http,
                app=app,
                requests=requests,
                model=model,
                projection=projection,
                fingerprint=fingerprint,
                actor=actor,
            )
            batch_id = await self._postSubmitAsync(
                http, approval_id=approval_id
            )
        return {
            "batch_id": batch_id,
            "approval_id": approval_id,
            "fingerprint": fingerprint,
        }

    async def _postProjectionAsync(
        self,
        http: httpx.AsyncClient,
        requests: list[dict[str, Any]],
        model: str,
    ) -> dict[str, Any]:
        payload = _buildProjectionPayload(requests=requests, model=model)
        try:
            response = await http.post("/calc/project-batch", json=payload)
        except httpx.HTTPError as exc:
            self._raiseUnreachable(exc, "calc/project-batch")
        if response.status_code >= 500:
            self._raiseUnreachable(
                RuntimeError(f"status={response.status_code}"),
                "calc/project-batch",
            )
        if response.status_code >= 400:
            raise GovernorUnreachable(
                f"projection rejected: {response.status_code} {response.text}"
            )
        parsed: dict[str, Any] = response.json()
        return parsed

    async def _postCheckAsync(
        self,
        http: httpx.AsyncClient,
        *,
        app: str,
        requests: list[dict[str, Any]],
        model: str,
        projection: dict[str, Any],
        fingerprint: str,
        actor: str,
    ) -> str:
        body = _buildSubmitPayload(
            app=app,
            requests=requests,
            model=model,
            projection=projection,
            fingerprint=fingerprint,
            actor=actor,
        )
        try:
            response = await http.post("/v1/check", json=body)
        except httpx.HTTPError as exc:
            self._raiseUnreachable(exc, "v1/check")
        if response.status_code >= 500:
            self._raiseUnreachable(
                RuntimeError(f"status={response.status_code}"), "v1/check"
            )
        if response.status_code >= 400:
            raise GovernorUnreachable(
                f"check rejected: {response.status_code} {response.text}"
            )
        data = response.json()
        if data.get("decision") == "DENIED":
            raise GovernorDenied(
                _readDenialReasons(data), app=app, fingerprint=fingerprint
            )
        approval_id = data.get("approval_id")
        if not isinstance(approval_id, str):
            raise GovernorUnreachable(
                f"check returned APPROVED without approval_id: {data!r}"
            )
        return approval_id

    async def _postSubmitAsync(
        self, http: httpx.AsyncClient, *, approval_id: str
    ) -> str:
        try:
            response = await http.post(
                "/v1/submit", json={"approval_id": approval_id}
            )
        except httpx.HTTPError as exc:
            self._raiseUnreachable(exc, "v1/submit")
        if response.status_code >= 500:
            self._raiseUnreachable(
                RuntimeError(f"status={response.status_code}"), "v1/submit"
            )
        if response.status_code >= 400:
            raise GovernorUnreachable(
                f"submit rejected: {response.status_code} {response.text}"
            )
        data = response.json()
        batch_id = data.get("batch_id")
        if not isinstance(batch_id, str):
            raise GovernorUnreachable(
                f"submit returned without batch_id: {data!r}"
            )
        return batch_id


# ─── Proxy: intercept messages.batches.create ───────────────────────────────
class _GovernedBatchesProxy:
    """Wraps the real ``batches`` client and overrides ``create``.

    Every other attribute (``retrieve``, ``list``, ``cancel``, ``results``,
    ``with_raw_response``, etc.) is delegated to the underlying SDK object
    via ``__getattr__`` so callers see the full Anthropic API surface.
    """

    def __init__(
        self,
        *,
        inner: Any,
        raw_client: Any,
        app: str,
        governor_base_url: str,
        on_fallback: FallbackCallback | None,
        timeout: float,
        is_async: bool,
    ) -> None:
        self._inner = inner
        self._raw_client = raw_client
        self._app = app
        self._on_fallback = on_fallback
        self._http = _GovernorHttp(governor_base_url, timeout)
        self._is_async = is_async

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    # ─── The actual override ─────────────────────────────────────────────
    def create(
        self,
        *,
        requests: Iterable[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        """Sync: route through governor, fall open on unreachable."""
        materialised = list(requests)
        model = _inferBatchModel(materialised)
        actor = resolveActor()
        try:
            outcome = self._http.projectAndSubmit(
                app=self._app,
                requests=materialised,
                model=model,
                actor=actor,
            )
        except GovernorUnreachable as exc:
            fireFallbackAlert(
                app=self._app,
                fingerprint=buildBatchFingerprint(
                    requests=materialised, model=model
                ),
                reason=exc.reason,
                callback=self._on_fallback,
            )
            return self._inner.create(requests=materialised, **kwargs)
        return _buildMessageBatch(batch_id=outcome["batch_id"])

    async def acreate(
        self,
        *,
        requests: Iterable[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        """Async sibling — used when the proxy sits on :class:`AsyncAnthropic`."""
        materialised = list(requests)
        model = _inferBatchModel(materialised)
        actor = resolveActor()
        try:
            outcome = await self._http.projectAndSubmitAsync(
                app=self._app,
                requests=materialised,
                model=model,
                actor=actor,
            )
        except GovernorUnreachable as exc:
            fireFallbackAlert(
                app=self._app,
                fingerprint=buildBatchFingerprint(
                    requests=materialised, model=model
                ),
                reason=exc.reason,
                callback=self._on_fallback,
            )
            return await self._inner.create(requests=materialised, **kwargs)
        return _buildMessageBatch(batch_id=outcome["batch_id"])


def _inferBatchModel(requests: list[dict[str, Any]]) -> str:
    """Return the model used by this batch.

    Every request in an Anthropic batch must specify the same model inside
    ``params.model``. We pull the first one and trust the gate to reject
    mismatches. If the list is empty we return the empty string, which
    the gate will reject via ``MODEL_NOT_ALLOWED``.
    """
    for entry in requests:
        params = entry.get("params") if isinstance(entry, dict) else None
        if isinstance(params, dict):
            model = params.get("model")
            if isinstance(model, str) and model:
                return model
    return ""


# ─── Async proxy wrapper ────────────────────────────────────────────────────
class _GovernedAsyncBatchesProxy(_GovernedBatchesProxy):
    """Async-friendly proxy: ``create`` is awaitable."""

    async def create(
        self,
        *,
        requests: Iterable[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        return await self.acreate(requests=requests, **kwargs)


# ─── Messages wrappers (expose .batches as the proxy) ───────────────────────
class _GovernedMessagesWrapper:
    """Delegates every messages method; only replaces ``batches``.

    Using a wrapper instead of subclassing :class:`anthropic.resources.Messages`
    because the SDK constructs its resource hierarchy eagerly and subclassing
    it is fragile across SDK minor versions. The proxy-based approach is
    stable: we delegate unknown attributes + forward tracked ones.
    """

    def __init__(self, *, inner: Any, batches_proxy: _GovernedBatchesProxy) -> None:
        self._inner = inner
        self._batches = batches_proxy

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def batches(self) -> _GovernedBatchesProxy:
        return self._batches


# ─── Public subclasses ──────────────────────────────────────────────────────
class Anthropic(_RawAnthropic):
    """Drop-in subclass of :class:`anthropic.Anthropic` that governs batches.

    Every non-batch method is inherited from the real SDK without change.
    Only ``self.messages.batches.create(...)`` is overridden, and even
    that falls through to the raw SDK when the governor is unreachable.
    """

    def __init__(
        self,
        *,
        app: str,
        governor_base_url: str | None = None,
        on_fallback: FallbackCallback | None = None,
        governor_timeout_seconds: float = GOVERNOR_TIMEOUT_SECONDS,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        resolved_base = (
            governor_base_url
            or os.environ.get(GOVERNOR_BASE_URL_ENV)
            or DEFAULT_GOVERNOR_BASE_URL
        )
        batches_proxy = _GovernedBatchesProxy(
            inner=super().messages.batches,
            raw_client=self,
            app=app,
            governor_base_url=resolved_base,
            on_fallback=on_fallback,
            timeout=governor_timeout_seconds,
            is_async=False,
        )
        self._governed_messages = _GovernedMessagesWrapper(
            inner=super().messages, batches_proxy=batches_proxy
        )

    @property
    def messages(self) -> Any:
        """Return the governed ``messages`` namespace."""
        return self._governed_messages


class AsyncAnthropic(_RawAsyncAnthropic):
    """Async drop-in sibling of :class:`Anthropic`."""

    def __init__(
        self,
        *,
        app: str,
        governor_base_url: str | None = None,
        on_fallback: FallbackCallback | None = None,
        governor_timeout_seconds: float = GOVERNOR_TIMEOUT_SECONDS,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        resolved_base = (
            governor_base_url
            or os.environ.get(GOVERNOR_BASE_URL_ENV)
            or DEFAULT_GOVERNOR_BASE_URL
        )
        batches_proxy = _GovernedAsyncBatchesProxy(
            inner=super().messages.batches,
            raw_client=self,
            app=app,
            governor_base_url=resolved_base,
            on_fallback=on_fallback,
            timeout=governor_timeout_seconds,
            is_async=True,
        )
        self._governed_messages = _GovernedMessagesWrapper(
            inner=super().messages, batches_proxy=batches_proxy
        )

    @property
    def messages(self) -> Any:
        """Return the governed ``messages`` namespace."""
        return self._governed_messages
