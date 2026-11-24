"""Endpoint tests for :mod:`server.app`.

Uses FastAPI's ``TestClient`` plus fakes for every external dependency:

* ``FakeUpstash``   — in-memory Redis stand-in matching ``UpstashClient``.
* ``FakeLedger``    — collects decision + lifecycle rows.
* ``FakeAnthropic`` — records ``create_batch`` / ``cancel_batch`` calls.

Each test wires a fresh :class:`AppState` via :func:`set_state` so the
tests don't depend on env vars (``UPSTASH_*``, ``ANTHROPIC_API_KEY``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import (  # noqa: E402
    AppState,
    MetricsCounters,
    app,
    set_state,
)
from server.calculator.calibration import CalibrationStore  # noqa: E402
from server.config import load_apps_config  # noqa: E402
from server.flags import FlagSink  # noqa: E402
from server.gate import GovernorMode  # noqa: E402
from server.upstash import UpstashUnavailable  # noqa: E402


# ─── Fakes ───────────────────────────────────────────────────────────────────
class FakeUpstash:
    """In-memory Upstash stand-in. Implements the subset app.py needs."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.floats: dict[str, float] = {}
        self.fail: bool = False

    def _check(self) -> None:
        if self.fail:
            raise UpstashUnavailable("fake failure")

    async def get(self, key: str) -> str | None:
        self._check()
        if key in self.strings:
            return self.strings[key]
        if key in self.floats:
            return str(self.floats[key])
        return None

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        self._check()
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        return True

    async def delete(self, key: str) -> int:
        self._check()
        removed = 0
        for bucket in (self.strings, self.floats, self.hashes, self.sets, self.lists):
            if key in bucket:
                del bucket[key]  # type: ignore[arg-type]
                removed = 1
        return removed

    async def incrbyfloat(self, key: str, amount: float) -> float:
        self._check()
        self.floats[key] = float(self.floats.get(key, 0.0)) + float(amount)
        return self.floats[key]

    async def expire(self, key: str, seconds: int) -> bool:
        del seconds
        self._check()
        return True

    async def hset(self, key: str, mapping: dict[str, Any]) -> int:
        self._check()
        bucket = self.hashes.setdefault(key, {})
        for field_name, value in mapping.items():
            bucket[field_name] = str(value)
        return len(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        self._check()
        return dict(self.hashes.get(key, {}))

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        self._check()
        bucket = self.hashes.setdefault(key, {})
        cur = int(float(bucket.get(field, "0"))) + amount
        bucket[field] = str(cur)
        return cur

    async def sadd(self, key: str, value: str) -> int:
        self._check()
        bucket = self.sets.setdefault(key, set())
        if value in bucket:
            return 0
        bucket.add(value)
        return 1

    async def smembers(self, key: str) -> set[str]:
        self._check()
        return set(self.sets.get(key, set()))

    async def srem(self, key: str, value: str) -> int:
        self._check()
        bucket = self.sets.get(key)
        if bucket and value in bucket:
            bucket.remove(value)
            return 1
        return 0

    async def lpush(self, key: str, value: str) -> int:
        self._check()
        lst = self.lists.setdefault(key, [])
        lst.insert(0, str(value))
        return len(lst)

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        self._check()
        lst = self.lists.get(key)
        if lst is None:
            return True
        if stop < 0:
            stop = len(lst) + stop
        self.lists[key] = lst[start : stop + 1]
        return True

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        self._check()
        lst = self.lists.get(key, [])
        if stop == -1:
            return list(lst[start:])
        return list(lst[start : stop + 1])

    async def exists(self, key: str) -> bool:
        self._check()
        return (
            key in self.strings
            or key in self.hashes
            or key in self.sets
            or key in self.lists
            or key in self.floats
        )

    async def expireat(self, key: str, ts: int) -> bool:
        del key, ts
        self._check()
        return True

    async def pipeline(self, commands: list[list[Any]]) -> list[Any]:
        """Replay the gate's approval pipeline against in-memory state."""
        self._check()
        results: list[Any] = []
        for cmd in commands:
            name = str(cmd[0]).upper()
            if name == "HSET":
                bucket = self.hashes.setdefault(str(cmd[1]), {})
                for index in range(2, len(cmd), 2):
                    bucket[str(cmd[index])] = str(cmd[index + 1])
                results.append(len(bucket))
            elif name == "EXPIRE":
                results.append(1)
            elif name == "INCRBYFLOAT":
                key = str(cmd[1])
                amount = float(cmd[2])
                self.floats[key] = float(self.floats.get(key, 0.0)) + amount
                results.append(self.floats[key])
            elif name == "SET":
                # SET key value EX n NX
                key = str(cmd[1])
                value = str(cmd[2])
                self.strings[key] = value
                results.append("OK")
            else:
                results.append(None)
        return results


class FakeLedger:
    """In-memory stand-in for :class:`SupabaseLedger`."""

    def __init__(self) -> None:
        self.decisions: list[Any] = []
        self.lifecycles: list[Any] = []
        self.variance_pct: float | None = 12.5

    async def write_decision(self, record: Any) -> None:
        self.decisions.append(record)

    async def write_batch_lifecycle(self, event: Any) -> None:
        self.lifecycles.append(event)

    async def flush_queue(self) -> int:
        return 0

    async def compute_variance(self, batch_id: str) -> Any:
        from server.supabase_ledger import VarianceReport
        return VarianceReport(
            batch_id=batch_id,
            projected_p95_usd=0.20,
            actual_usd=0.225,
            variance_pct=self.variance_pct,
        )


class _FakeBatch:
    def __init__(self, batch_id: str) -> None:
        self.id = batch_id


class FakeAnthropic:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.canceled: list[str] = []
        self.fail_create: bool = False
        self.messages = _FakeMessages(self)

    async def create_batch(self, *, requests: list[dict[str, Any]], model: str) -> Any:
        if self.fail_create:
            from server.anthropic_client import AnthropicUnavailable
            raise AnthropicUnavailable("simulated outage")
        self.created.append({"requests": requests, "model": model})
        return _FakeBatch(batch_id=f"batch_{len(self.created):03d}")

    async def cancel_batch(self, batch_id: str) -> Any:
        self.canceled.append(batch_id)
        return _FakeBatch(batch_id=batch_id)

    async def count_tokens(
        self, *, model: str, messages: list[dict[str, Any]],
        system: Any = None,
    ) -> int:
        del model, system
        return 100 * max(1, len(messages))


class _FakeMessagesInner:
    """Supports ``anthropic.messages.count_tokens(model=..., ...)``."""

    def __init__(self, parent: "FakeAnthropic") -> None:
        self._parent = parent

    def count_tokens(
        self, *, model: str, messages: list[dict[str, Any]],
        system: Any = None, tools: Any = None,
    ) -> Any:
        del model, system, tools

        class _Resp:
            def __init__(self, tokens: int) -> None:
                self.input_tokens = tokens

        return _Resp(tokens=123 * max(1, len(messages)))


class _FakeMessages:
    def __init__(self, parent: "FakeAnthropic") -> None:
        self.messages = _FakeMessagesInner(parent)


# ─── Fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture
def app_state(tmp_path: Path) -> AppState:
    """Build a fresh :class:`AppState` with fakes and a minimal YAML."""
    yaml_path = tmp_path / "apps.yaml"
    yaml_path.write_text(
        "defaults: &defaults\n"
        "  max_batch_size: 40\n"
        "  per_submit_cap_usd: 5.00\n"
        "  daily_ceiling_usd: 25.00\n"
        "  monthly_ceiling_usd: 300.00\n"
        "  allowed_models:\n"
        "    - claude-haiku-4-5-20251001\n"
        "  allow_daemon: false\n"
        "  allow_large_batch: false\n"
        "apps:\n"
        "  default:\n"
        "    <<: *defaults\n"
        "  example-rank-batch:\n"
        "    <<: *defaults\n"
        "    per_submit_cap_usd: 3.00\n",
        encoding="utf-8",
    )
    config = load_apps_config(yaml_path)
    upstash = FakeUpstash()
    ledger = FakeLedger()
    anthropic = FakeAnthropic()
    calibration = CalibrationStore(upstash)  # type: ignore[arg-type]
    state = AppState(
        upstash=upstash,  # type: ignore[arg-type]
        ledger=ledger,  # type: ignore[arg-type]
        anthropic=anthropic,  # type: ignore[arg-type]
        calibration=calibration,
        config=config,
        metrics=MetricsCounters(),
        # Hermetic flag sink under tmp_path — never touches ~/.governor.
        flags=FlagSink(path=tmp_path / "flags.jsonl"),
    )
    set_state(state)
    return state


@pytest.fixture
def client(app_state: AppState) -> TestClient:
    del app_state
    return TestClient(app)


# ─── Health & Metrics ───────────────────────────────────────────────────────
def test_healthz_returns_degraded_when_no_monitor(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "0.1.0"
    # No heartbeat written yet — the endpoint reports a very stale age and
    # therefore status=degraded. That's the expected behavior on first boot.
    assert body["status"] == "degraded"
    assert body["monitor_heartbeat_age_s"] > 0


def test_metrics_exposes_prometheus_format(
    client: TestClient, app_state: AppState
) -> None:
    app_state.metrics.incrDecision(app="example-rank-batch", decision="APPROVED")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "governor_decisions_total" in body
    assert "governor_rogue_total" in body
    assert "governor_killed_total" in body
    assert 'app="example-rank-batch"' in body


# ─── Calculator: pricing ────────────────────────────────────────────────────
def test_calc_pricing_returns_all_models(client: TestClient) -> None:
    resp = client.get("/calc/pricing")
    assert resp.status_code == 200
    body = resp.json()
    assert "version" in body
    assert "claude-haiku-4-5-20251001" in body["pricing"]
    assert "claude-sonnet-4-6" in body["pricing"]
    haiku = body["pricing"]["claude-haiku-4-5-20251001"]
    assert haiku["input_per_token"] > 0
    assert haiku["output_per_token"] > 0


# ─── Calculator: estimate-input ─────────────────────────────────────────────
def test_calc_estimate_input_falls_back_to_heuristic(client: TestClient) -> None:
    resp = client.post("/calc/estimate-input", json={
        "model": "claude-haiku-4-5-20251001",
        "messages": [{"role": "user", "content": "hi" * 100}],
        "use_count_tokens": False,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "heuristic"
    assert body["tokens"] > 0


# ─── Calculator: project-output + calibrate ─────────────────────────────────
def test_project_output_uses_fallback_without_calibration(
    client: TestClient,
) -> None:
    resp = client.post("/calc/project-output", json={
        "operation": "brand-new-op",
        "model": "claude-haiku-4-5-20251001",
        "fallback_p50": 100,
        "fallback_p95": 200,
        "fallback_p99": 250,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "fallback"
    assert body["p50"] == 100
    assert body["p95"] == 200


def test_calibrate_then_fetch_stats(client: TestClient) -> None:
    for output_tokens in range(20, 45):
        resp = client.post("/calc/calibrate", json={
            "operation": "op-under-test",
            "model": "claude-haiku-4-5-20251001",
            "actual_input_tokens": 1000,
            "actual_output_tokens": output_tokens,
            "actual_cost_usd": 0.001,
        })
        assert resp.status_code == 200
    stats = client.get("/calc/calibration/op-under-test").json()
    assert stats["n_observations"] >= 20
    assert stats["source"] == "reservoir"


# ─── Calculator: project-cost + project-batch ───────────────────────────────
def test_project_cost_rejects_bad_tokens(client: TestClient) -> None:
    resp = client.post("/calc/project-cost", json={
        "model": "claude-haiku-4-5-20251001",
        "input_tokens": 100,
        "output": {
            "p50": 500, "p95": 1500, "p99": 1800,
            "n_observations": 0, "source": "fallback",
        },
        "cache_mode": "none",
        "cached_input_tokens": 999,  # > input_tokens → 400
    })
    assert resp.status_code == 400


def test_project_batch_with_output_override(client: TestClient) -> None:
    resp = client.post("/calc/project-batch", json={
        "requests": [{"unique_input_tokens": 200}] * 5,
        "shared_system_tokens": 1000,
        "cache_mode": "5min",
        "model": "claude-haiku-4-5-20251001",
        "operation": "batch-op",
        "output_override": {
            "p50": 500, "p95": 1500, "p99": 1800,
            "n_observations": 100, "source": "reservoir",
        },
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_requests"] == 5
    assert body["total_p95_usd"] > 0


# ─── Governor: /v1/check happy path ─────────────────────────────────────────
def _buildCheckBody(
    *,
    app_name: str = "example-rank-batch",
    n_requests: int = 3,
    p95_usd: float = 0.20,
    fingerprint: str = "fp-test-1",
) -> dict[str, Any]:
    return {
        "app": app_name,
        "requests": [
            {"custom_id": f"req-{index}", "params": {"model": "x"}}
            for index in range(n_requests)
        ],
        "model": "claude-haiku-4-5-20251001",
        "projection": {
            "p50_usd": 0.10,
            "p95_usd": p95_usd,
            "cache_break_even_warning": False,
        },
        "fingerprint": fingerprint,
        "cache_mode": "none",
    }


def test_check_approves_clean_submit(
    client: TestClient, app_state: AppState
) -> None:
    resp = client.post("/v1/check", json=_buildCheckBody())
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "APPROVED"
    assert "approval_id" in body
    # Metrics counter moved.
    assert app_state.metrics.decisions_total["example-rank-batch"]["APPROVED"] == 1


def test_check_denies_batch_size(client: TestClient) -> None:
    body = _buildCheckBody(n_requests=500)
    resp = client.post("/v1/check", json=body)
    assert resp.status_code == 200
    response_body = resp.json()
    assert response_body["decision"] == "DENIED"
    codes = {reason["code"] for reason in response_body["reasons"]}
    assert "BATCH_SIZE_EXCEEDED" in codes


def test_check_denies_model_not_allowed(client: TestClient) -> None:
    body = _buildCheckBody()
    body["model"] = "claude-opus-4-7"
    resp = client.post("/v1/check", json=body)
    assert resp.status_code == 200
    response_body = resp.json()
    assert response_body["decision"] == "DENIED"
    assert any(
        r["code"] == "MODEL_NOT_ALLOWED"
        for r in response_body["reasons"]
    )


# ─── Governor: /v1/submit ────────────────────────────────────────────────────
def test_submit_requires_valid_approval(client: TestClient) -> None:
    resp = client.post("/v1/submit", json={"approval_id": "not-a-real-id"})
    assert resp.status_code == 410


def test_submit_happy_path_creates_batch(
    client: TestClient, app_state: AppState
) -> None:
    # First check → get approval id.
    check_resp = client.post(
        "/v1/check", json=_buildCheckBody(fingerprint="fp-submit-1")
    )
    approval_id = check_resp.json()["approval_id"]
    submit_resp = client.post(
        "/v1/submit", json={"approval_id": approval_id}
    )
    assert submit_resp.status_code == 200, submit_resp.text
    body = submit_resp.json()
    assert body["approval_id"] == approval_id
    assert body["batch_id"].startswith("batch_")
    # Anthropic received the create_batch call.
    assert len(app_state.anthropic.created) == 1  # type: ignore[attr-defined]
    # Batch id recorded in-flight.
    assert body["batch_id"] in app_state.upstash.sets.get(  # type: ignore[attr-defined]
        "gov:inflight", set()
    )


def test_submit_returns_503_on_anthropic_outage(
    client: TestClient, app_state: AppState
) -> None:
    app_state.anthropic.fail_create = True  # type: ignore[attr-defined]
    check_resp = client.post(
        "/v1/check", json=_buildCheckBody(fingerprint="fp-outage")
    )
    approval_id = check_resp.json()["approval_id"]
    submit_resp = client.post(
        "/v1/submit", json={"approval_id": approval_id}
    )
    assert submit_resp.status_code == 503


# ─── Governor: /v1/register ──────────────────────────────────────────────────
def test_register_records_batch_lifecycle(
    client: TestClient, app_state: AppState
) -> None:
    check_resp = client.post(
        "/v1/check", json=_buildCheckBody(fingerprint="fp-register")
    )
    approval_id = check_resp.json()["approval_id"]
    resp = client.post("/v1/register", json={
        "approval_id": approval_id,
        "batch_id": "batch_external_001",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert any(
        event.batch_id == "batch_external_001"
        for event in app_state.ledger.lifecycles  # type: ignore[attr-defined]
    )


# ─── Governor: /v1/status ────────────────────────────────────────────────────
def test_status_reports_apps_and_kill_switch(
    client: TestClient, app_state: AppState
) -> None:
    resp = client.get("/v1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kill_switch"] is False
    app_names = {entry["app"] for entry in body["daily"]}
    assert "default" in app_names
    assert "example-rank-batch" in app_names


# ─── Governor: /v1/kill ──────────────────────────────────────────────────────
def test_kill_cancels_batch_at_anthropic(
    client: TestClient, app_state: AppState
) -> None:
    resp = client.post("/v1/kill/batch_abc123")
    assert resp.status_code == 200
    assert resp.json()["canceled"] is True
    assert "batch_abc123" in app_state.anthropic.canceled  # type: ignore[attr-defined]
    assert app_state.metrics.killed_total == 1


# ─── Governor: /v1/kill-switch ───────────────────────────────────────────────
def test_kill_switch_on_and_off(
    client: TestClient, app_state: AppState
) -> None:
    on_resp = client.post(
        "/v1/kill-switch", json={"on": True, "reason": "investigation"}
    )
    assert on_resp.status_code == 200
    assert on_resp.json()["kill_switch"] is True
    assert "gov:kill-switch" in app_state.upstash.strings  # type: ignore[attr-defined]

    off_resp = client.post("/v1/kill-switch", json={"on": False})
    assert off_resp.status_code == 200
    assert off_resp.json()["kill_switch"] is False
    assert "gov:kill-switch" not in app_state.upstash.strings  # type: ignore[attr-defined]


def test_kill_switch_blocks_subsequent_checks(
    client: TestClient, app_state: AppState
) -> None:
    client.post("/v1/kill-switch", json={"on": True, "reason": "test"})
    resp = client.post("/v1/check", json=_buildCheckBody(fingerprint="fp-blocked"))
    body = resp.json()
    assert body["decision"] == "DENIED"
    assert body["reasons"][0]["code"] == "KILL_SWITCH_ACTIVE"


# ─── Governor: /v1/reconcile ─────────────────────────────────────────────────
def test_reconcile_returns_variance(client: TestClient) -> None:
    resp = client.post("/v1/reconcile/batch_xyz", json={
        "actual_input_tokens": 1000,
        "actual_output_tokens": 500,
        "actual_cost_usd": 0.25,
        "operation": "pt_rank",
    })
    assert resp.status_code == 200
    assert resp.json()["variance_pct"] == pytest.approx(12.5)


def test_reconcile_flags_projection_mismatch_beyond_tolerance(
    client: TestClient, app_state: AppState
) -> None:
    """FakeLedger reports 12.5% variance (> 10% tolerance) → a flag is emitted.

    This fires regardless of mode — reconciliation is observation, not gating.
    """
    resp = client.post("/v1/reconcile/batch_xyz", json={
        "actual_input_tokens": 1000,
        "actual_output_tokens": 500,
        "actual_cost_usd": 0.25,
    })
    assert resp.status_code == 200
    counts = app_state.flags.counts()  # type: ignore[attr-defined]
    assert counts["projection_mismatch"] == 1


def test_reconcile_within_tolerance_does_not_flag(
    client: TestClient, app_state: AppState
) -> None:
    # Pull variance back inside the 10% band; no flag should fire.
    app_state.ledger.variance_pct = 4.0  # type: ignore[attr-defined]
    resp = client.post("/v1/reconcile/batch_within", json={
        "actual_input_tokens": 1000,
        "actual_output_tokens": 500,
        "actual_cost_usd": 0.21,
    })
    assert resp.status_code == 200
    assert app_state.flags.counts()["projection_mismatch"] == 0  # type: ignore[attr-defined]


# ─── Permissive mode ─────────────────────────────────────────────────────────
@pytest.fixture
def permissive_state(app_state: AppState) -> AppState:
    """Flip the active state to permissive mode."""
    app_state.mode = GovernorMode.PERMISSIVE
    return app_state


def test_permissive_check_approves_and_flags_would_deny(
    client: TestClient, permissive_state: AppState
) -> None:
    # 500 requests exceeds max_batch_size=40 → enforcing would DENY.
    resp = client.post("/v1/check", json=_buildCheckBody(n_requests=500))
    assert resp.status_code == 200
    body = resp.json()
    # Auditor mode: APPROVED, not DENIED.
    assert body["decision"] == "APPROVED"
    assert "approval_id" in body
    # A would_deny flag was recorded.
    assert permissive_state.flags.counts()["would_deny"] == 1  # type: ignore[attr-defined]


def test_enforcing_check_still_denies_batch_size(client: TestClient) -> None:
    """Default (enforcing) fixture: the same request DENIES — unchanged behavior."""
    resp = client.post("/v1/check", json=_buildCheckBody(n_requests=500))
    body = resp.json()
    assert body["decision"] == "DENIED"


# ─── Status + health mode visibility ─────────────────────────────────────────
def test_status_reports_mode_and_flag_counts(
    client: TestClient, permissive_state: AppState
) -> None:
    # Trigger one would_deny flag, then read status.
    client.post("/v1/check", json=_buildCheckBody(n_requests=500))
    resp = client.get("/v1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "permissive"
    assert body["flags_since_start"]["would_deny"] == 1


def test_healthz_reports_mode(client: TestClient, permissive_state: AppState) -> None:
    body = client.get("/healthz").json()
    assert body["mode"] == "permissive"


def test_status_defaults_to_enforcing_mode(client: TestClient) -> None:
    body = client.get("/v1/status").json()
    assert body["mode"] == "enforcing"
    assert body["flags_since_start"] == {"would_deny": 0, "projection_mismatch": 0}
