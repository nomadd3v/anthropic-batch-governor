"""Tests for :mod:`server.flags` — the permissive-mode flag stream.

Covers the two flag kinds' JSONL serialization, the append-only sink, the
per-kind counters that back ``/v1/status``, and the resilience guarantee (a
broken sink path never raises).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.flags import (  # noqa: E402
    DEFAULT_FLAGS_PATH,
    PROJECTION_MISMATCH_TOLERANCE_RATIO,
    FlagEvent,
    FlagSink,
)


# ─── FlagEvent serialization ─────────────────────────────────────────────────
def test_would_deny_event_serializes_only_relevant_fields() -> None:
    event = FlagEvent(
        kind="would_deny",
        app="example-rank-batch",
        projected_p95_usd=47.20,
        overage_usd=42.20,
        budget_state={"daily": {"remaining_usd": 5.0}},
        would_deny_reasons=[{"code": "PER_SUBMIT_CAP", "detail": "..."}],
    )
    line = event.to_jsonl_line()
    parsed = json.loads(line)
    assert parsed["kind"] == "would_deny"
    assert parsed["overage_usd"] == 42.20
    assert parsed["would_deny_reasons"][0]["code"] == "PER_SUBMIT_CAP"
    # projection_mismatch-only fields are omitted (exclude_none).
    assert "ratio" not in parsed
    assert "actual_usd" not in parsed
    # ts is always present and ISO-formatted.
    assert "ts" in parsed


def test_projection_mismatch_event_serializes_only_relevant_fields() -> None:
    event = FlagEvent(
        kind="projection_mismatch",
        app="example-deploy-agent",
        batch_id="batch_abc",
        projected_usd=0.20,
        actual_usd=0.40,
        delta_usd=0.20,
        ratio=1.0,
        tolerance=PROJECTION_MISMATCH_TOLERANCE_RATIO,
    )
    parsed = json.loads(event.to_jsonl_line())
    assert parsed["kind"] == "projection_mismatch"
    assert parsed["ratio"] == 1.0
    assert parsed["batch_id"] == "batch_abc"
    # would_deny-only fields are omitted.
    assert "budget_state" not in parsed
    assert "overage_usd" not in parsed


# ─── FlagSink append + counters ──────────────────────────────────────────────
def test_sink_appends_one_line_per_flag_and_counts(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "flags.jsonl"
    sink = FlagSink(path=path)
    # Parent dir is created eagerly.
    assert path.parent.exists()

    sink.emit(FlagEvent(kind="would_deny", app="a"))
    sink.emit(FlagEvent(kind="would_deny", app="b"))
    sink.emit(FlagEvent(kind="projection_mismatch", app="a", batch_id="x"))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    # Every line is valid standalone JSON (append-only JSONL).
    kinds = [json.loads(line)["kind"] for line in lines]
    assert kinds == ["would_deny", "would_deny", "projection_mismatch"]

    assert sink.counts() == {"would_deny": 2, "projection_mismatch": 1}
    assert sink.total == 3


def test_sink_counts_is_a_copy(tmp_path: Path) -> None:
    sink = FlagSink(path=tmp_path / "flags.jsonl")
    sink.emit(FlagEvent(kind="would_deny", app="a"))
    snapshot = sink.counts()
    snapshot["would_deny"] = 999
    # Mutating the returned dict must not corrupt the sink's counters.
    assert sink.counts()["would_deny"] == 1


def test_sink_never_raises_on_unwritable_path(tmp_path: Path) -> None:
    # Point the sink at a path whose parent is a file, not a directory: opening
    # for append raises OSError, which emit must swallow (counter still moves).
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    sink = FlagSink(path=blocker / "flags.jsonl")
    sink.emit(FlagEvent(kind="would_deny", app="a"))  # must not raise
    assert sink.counts()["would_deny"] == 1


def test_from_env_uses_default_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Redirect HOME so the default ``~/.governor`` resolves under tmp_path and
    # the test never touches the real home directory.
    monkeypatch.delenv("GOVERNOR_FLAGS_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    sink = FlagSink.fromEnv()
    assert sink.path == Path(DEFAULT_FLAGS_PATH).expanduser()
    assert str(sink.path).startswith(str(tmp_path))


def test_from_env_respects_override(tmp_path: Path) -> None:
    import os

    override = tmp_path / "custom" / "flags.jsonl"
    os.environ["GOVERNOR_FLAGS_PATH"] = str(override)
    try:
        sink = FlagSink.fromEnv()
        assert sink.path == override
    finally:
        os.environ.pop("GOVERNOR_FLAGS_PATH", None)
