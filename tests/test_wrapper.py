"""Tests for the Python drop-in ``governor`` wrapper.

Covers the three paths the wrapper promises:

1. **Happy path** — governor approves + returns a ``batch_id``; the raw SDK's
   ``messages.batches.create`` is NOT called.
2. **Denial path** — governor returns ``decision=DENIED``; the wrapper raises
   :class:`GovernorDenied` carrying the structured reasons; raw SDK not called.
3. **Unreachable path** — httpx raises a connection error; the wrapper logs a
   ``[BATCH-GOVERNOR-FALLBACK]`` warning, fires the ``on_fallback`` callback,
   and falls through to the raw SDK.

``httpx.Client`` is monkeypatched at the module level inside
:mod:`clients.python.governor.client` so the real SDK object never leaves the
process. The Anthropic SDK is loaded but its network path is never exercised
— we subclass + inject a fake ``batches`` object before calling.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clients.python.governor import (  # noqa: E402
    Anthropic,
    FallbackInfo,
    GovernorDenied,
    GovernorUnreachable,
)
from clients.python.governor import client as client_module  # noqa: E402


# ─── Fake httpx transport ────────────────────────────────────────────────────
class _FakeResponse:
    """Minimal stand-in for ``httpx.Response`` used by the wrapper."""

    def __init__(self, status_code: int, json_body: Any, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_body
        self.text = text or str(json_body)

    def json(self) -> Any:
        return self._json


class _FakeHttpxClient:
    """Records every ``.post`` call and replays scripted responses.

    The sequence of responses is keyed by URL path, so tests can prime
    ``/calc/project-batch``, ``/v1/check``, and ``/v1/submit`` independently
    without caring about call order.
    """

    def __init__(
        self,
        *,
        responses: dict[str, list[Any]] | None = None,
        raise_on: set[str] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.raise_on = raise_on or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> "_FakeHttpxClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def post(self, path: str, *, json: dict[str, Any]) -> _FakeResponse:
        self.calls.append((path, json))
        if path in self.raise_on:
            import httpx as _httpx

            raise _httpx.ConnectError(f"simulated-unreachable {path}")
        queue = self.responses.get(path, [])
        if not queue:
            raise AssertionError(f"no scripted response for {path}")
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _installFakeHttpxClient(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeHttpxClient
) -> None:
    """Patch ``httpx.Client`` inside the wrapper module to return our fake."""
    monkeypatch.setattr(
        client_module.httpx,
        "Client",
        lambda *args, **kwargs: fake,
    )


# ─── Fake SDK batches object ─────────────────────────────────────────────────
class _FakeBatchesCreate:
    """Replaces the real SDK's ``messages.batches.create``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, *, requests: list[dict[str, Any]], **kwargs: Any) -> Any:
        payload = {"requests": list(requests), **kwargs}
        self.calls.append(payload)
        return {
            "id": "batch_raw_sdk_passthrough",
            "type": "message_batch",
            "processing_status": "in_progress",
            "via": "raw-sdk-fallback",
        }


def _buildGovernedClient(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_batches: _FakeBatchesCreate,
    on_fallback: Callable[[FallbackInfo], None] | None = None,
) -> Anthropic:
    """Construct an :class:`Anthropic` wrapper with the SDK network-free.

    The real ``anthropic.Anthropic.__init__`` requires an API key; we supply a
    placeholder + monkey-patch the underlying ``messages.batches`` slot with
    our fake so no HTTP call leaves the test.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.delenv("GOVERNOR_BASE_URL", raising=False)
    monkeypatch.setenv("GOVERNOR_ACTOR", "test-actor@unit")

    wrapper = Anthropic(
        app="example-app",
        governor_base_url="http://127.0.0.1:5185",
        on_fallback=on_fallback,
    )
    # Replace the wrapper's internal batches proxy source with the fake — the
    # proxy delegates ``__getattr__`` to this inner, so unknown methods hit
    # our fake and ``create`` fallback triggers it too.
    # mypy: accessing internals is intentional for testing.
    wrapper._governed_messages._batches._inner = fake_batches  # type: ignore[attr-defined]
    return wrapper


# ─── Test 1: happy path ──────────────────────────────────────────────────────
def test_happy_path_submits_through_governor_and_skips_raw_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """APPROVED + submitted → wrapper returns governor batch, raw SDK untouched."""
    fake_sdk_batches = _FakeBatchesCreate()
    fake_http = _FakeHttpxClient(
        responses={
            "/calc/project-batch": [
                _FakeResponse(
                    200,
                    {"p50_usd": 0.10, "p95_usd": 0.20, "cache_break_even_warning": False},
                ),
            ],
            "/v1/check": [
                _FakeResponse(
                    200,
                    {
                        "decision": "APPROVED",
                        "approval_id": "appr_123",
                        "p95_usd": 0.20,
                        "expires_at": "2099-01-01T00:00:00+00:00",
                        "warnings": [],
                    },
                ),
            ],
            "/v1/submit": [
                _FakeResponse(
                    200,
                    {"batch_id": "batch_gov_abc", "approval_id": "appr_123"},
                ),
            ],
        }
    )
    _installFakeHttpxClient(monkeypatch, fake_http)

    wrapper = _buildGovernedClient(monkeypatch, fake_batches=fake_sdk_batches)

    result = wrapper.messages.batches.create(
        requests=[
            {
                "custom_id": "req-1",
                "params": {
                    "model": "claude-haiku-4-5-20251001",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            },
        ],
    )

    # The wrapper must have hit all three governor endpoints in order.
    paths_called = [path for path, _body in fake_http.calls]
    assert paths_called == [
        "/calc/project-batch",
        "/v1/check",
        "/v1/submit",
    ]
    # Raw SDK must NOT have been called — governor handled the submit.
    assert fake_sdk_batches.calls == []
    # The returned object carries the governor's batch id.
    returned_id = getattr(result, "id", None) or (
        result.get("id") if isinstance(result, dict) else None
    )
    assert returned_id == "batch_gov_abc"

    # The /v1/check body should have included the app + actor + fingerprint.
    _path, check_body = next(
        entry for entry in fake_http.calls if entry[0] == "/v1/check"
    )
    assert check_body["app"] == "example-app"
    assert check_body["actor"] == "test-actor@unit"
    assert isinstance(check_body["fingerprint"], str)
    assert len(check_body["fingerprint"]) == 64  # sha256 hex


# ─── Test 2: denial path ─────────────────────────────────────────────────────
def test_denial_path_raises_governor_denied_with_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DENIED → wrapper raises :class:`GovernorDenied`, SDK untouched."""
    fake_sdk_batches = _FakeBatchesCreate()
    fake_http = _FakeHttpxClient(
        responses={
            "/calc/project-batch": [
                _FakeResponse(
                    200,
                    {"p50_usd": 0.10, "p95_usd": 0.20, "cache_break_even_warning": False},
                ),
            ],
            "/v1/check": [
                _FakeResponse(
                    200,
                    {
                        "decision": "DENIED",
                        "reasons": [
                            {
                                "code": "DAILY_CEILING",
                                "detail": "example-app has $0.00 remaining today",
                                "fix": "retry after midnight UTC",
                            },
                            {
                                "code": "MODEL_NOT_ALLOWED",
                                "detail": "sonnet not in allowlist",
                                "fix": "use haiku",
                            },
                        ],
                    },
                ),
            ],
        }
    )
    _installFakeHttpxClient(monkeypatch, fake_http)

    wrapper = _buildGovernedClient(monkeypatch, fake_batches=fake_sdk_batches)

    with pytest.raises(GovernorDenied) as exc_info:
        wrapper.messages.batches.create(
            requests=[
                {
                    "custom_id": "req-1",
                    "params": {
                        "model": "claude-haiku-4-5-20251001",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                },
            ],
        )

    # Structured reasons must come through verbatim.
    reasons = exc_info.value.reasons
    assert [reason["code"] for reason in reasons] == [
        "DAILY_CEILING",
        "MODEL_NOT_ALLOWED",
    ]
    assert exc_info.value.app == "example-app"
    assert exc_info.value.fingerprint is not None

    # Raw SDK must NOT have been called — denial is blocking.
    assert fake_sdk_batches.calls == []
    # /v1/submit must NOT have been called.
    paths_called = [path for path, _body in fake_http.calls]
    assert "/v1/submit" not in paths_called


# ─── Test 3: unreachable → fail-open with alert ──────────────────────────────
def test_unreachable_path_logs_warning_fires_callback_and_falls_through(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """httpx connect error → fallback to raw SDK, alert fires, warning logged."""
    fake_sdk_batches = _FakeBatchesCreate()
    # The first call (/calc/project-batch) raises; the wrapper must NOT
    # proceed to /v1/check or /v1/submit and must fall through.
    fake_http = _FakeHttpxClient(
        raise_on={"/calc/project-batch"},
    )
    _installFakeHttpxClient(monkeypatch, fake_http)

    fallback_events: list[FallbackInfo] = []

    def record_fallback(info: FallbackInfo) -> None:
        fallback_events.append(info)

    wrapper = _buildGovernedClient(
        monkeypatch,
        fake_batches=fake_sdk_batches,
        on_fallback=record_fallback,
    )

    caplog.set_level("WARNING", logger="governor.client")

    result = wrapper.messages.batches.create(
        requests=[
            {
                "custom_id": "req-1",
                "params": {
                    "model": "claude-haiku-4-5-20251001",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            },
        ],
    )

    # Raw SDK MUST have been called with the original request set.
    assert len(fake_sdk_batches.calls) == 1
    assert fake_sdk_batches.calls[0]["requests"][0]["custom_id"] == "req-1"

    # The fallback batch object should come from the raw SDK stub.
    assert isinstance(result, dict)
    assert result["via"] == "raw-sdk-fallback"

    # on_fallback should have fired exactly once, carrying app + fingerprint.
    assert len(fallback_events) == 1
    event = fallback_events[0]
    assert event["app"] == "example-app"
    assert event["topic"] == "batch-governor-unreachable"
    assert isinstance(event["fingerprint"], str)
    assert len(event["fingerprint"]) == 64
    assert "calc/project-batch" in event["reason"]

    # Warning log must carry the fallback prefix.
    fallback_records = [
        record for record in caplog.records
        if "[BATCH-GOVERNOR-FALLBACK]" in record.getMessage()
    ]
    assert fallback_records, (
        "no [BATCH-GOVERNOR-FALLBACK] log captured; fallback should "
        "always leave a warning trace per ADR-005"
    )


# ─── Test 4: unreachable on /v1/check also fails open ───────────────────────
def test_unreachable_on_v1_check_also_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Governor reachable for projection but dead on /v1/check → still fallback."""
    fake_sdk_batches = _FakeBatchesCreate()
    fake_http = _FakeHttpxClient(
        responses={
            "/calc/project-batch": [
                _FakeResponse(
                    200,
                    {"p50_usd": 0.10, "p95_usd": 0.20, "cache_break_even_warning": False},
                ),
            ],
        },
        raise_on={"/v1/check"},
    )
    _installFakeHttpxClient(monkeypatch, fake_http)
    wrapper = _buildGovernedClient(monkeypatch, fake_batches=fake_sdk_batches)

    result = wrapper.messages.batches.create(
        requests=[
            {
                "custom_id": "req-1",
                "params": {
                    "model": "claude-haiku-4-5-20251001",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            },
        ],
    )
    # Raw SDK must have been reached.
    assert len(fake_sdk_batches.calls) == 1
    # Result should be the raw SDK stub.
    assert isinstance(result, dict)
    assert result["via"] == "raw-sdk-fallback"


# ─── Test 5: fingerprint is stable across dict ordering ─────────────────────
def test_fingerprint_is_stable_across_dict_ordering() -> None:
    """Same logical batch with different key orderings → same fingerprint."""
    requests_a = [
        {
            "custom_id": "c-1",
            "params": {
                "model": "claude-haiku-4-5-20251001",
                "messages": [{"role": "user", "content": "x"}],
            },
        },
        {
            "custom_id": "c-2",
            "params": {
                "model": "claude-haiku-4-5-20251001",
                "messages": [{"role": "user", "content": "y"}],
            },
        },
    ]
    # Reorder list + reorder the ``params`` dict keys — output must still match.
    requests_b = [
        {
            "params": {
                "messages": [{"role": "user", "content": "y"}],
                "model": "claude-haiku-4-5-20251001",
            },
            "custom_id": "c-2",
        },
        {
            "params": {
                "messages": [{"role": "user", "content": "x"}],
                "model": "claude-haiku-4-5-20251001",
            },
            "custom_id": "c-1",
        },
    ]
    fp_a = client_module.buildBatchFingerprint(
        requests=requests_a, model="claude-haiku-4-5-20251001"
    )
    fp_b = client_module.buildBatchFingerprint(
        requests=requests_b, model="claude-haiku-4-5-20251001"
    )
    assert fp_a == fp_b


# ─── Test 6: actor falls back to USER@hostname ──────────────────────────────
def test_resolve_actor_falls_back_to_user_and_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GOVERNOR_ACTOR unset → USER@hostname composition."""
    monkeypatch.delenv("GOVERNOR_ACTOR", raising=False)
    monkeypatch.setenv("USER", "alice")
    actor = client_module.resolveActor()
    assert actor.startswith("alice@")
    assert "@" in actor
