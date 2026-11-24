#!/usr/bin/env bash
#
# test_ci_check.sh — smoke test for ``scripts/ci-governor-check.sh``.
#
# Seeds a tmp fixture directory with three shapes:
#   (a) compliant.py          — imports ``from governor`` + calls batches.create
#   (b) compliant.ts          — imports ``@batch-governor/governor`` + calls batches.create
#   (c) raw_batch.py          — imports ``from anthropic`` + calls batches.create
#   (d) raw_batch.ts          — imports ``@anthropic-ai/sdk`` + calls batches.create
#   (e) raw_import_only.py    — imports ``from anthropic`` (no batch call)
#   (f) node_modules/*        — fake vendored code with raw imports (must be pruned)
#
# Runs the checker in each configured mode and asserts the expected exit
# codes + output contents. This script does NOT require the governor service
# running.
#
# Run manually:
#   ./tests/test_ci_check.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHECKER="${REPO_ROOT}/scripts/ci-governor-check.sh"

if [[ ! -x "$CHECKER" ]]; then
    echo "FAIL — $CHECKER missing or not executable" >&2
    exit 1
fi

TMPDIR="$(mktemp -d -t ci-governor-check.XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

_pass() { echo "  ok   — $1"; }
_fail() { echo "  FAIL — $1"; exit 1; }

echo "ci-governor-check.sh smoke tests"
echo "--------------------------------"

# ─── Fixture: compliant Python file ────────────────────────────────────────
mkdir -p "$TMPDIR/app/python"
cat >"$TMPDIR/app/python/compliant.py" <<'PY'
"""Uses the governor wrapper — should NOT be reported."""
from governor import Anthropic

def run() -> None:
    client = Anthropic(app="example-app")
    client.messages.batches.create(requests=[])
PY

# ─── Fixture: compliant TypeScript file ────────────────────────────────────
mkdir -p "$TMPDIR/app/ts"
cat >"$TMPDIR/app/ts/compliant.ts" <<'TS'
// Uses the governor wrapper — should NOT be reported.
import { GovernorAnthropic } from "@batch-governor/governor";

const client = new GovernorAnthropic({ app: "example-app" });
await client.messages.batches.create({ requests: [] });
TS

# ─── Fixture: raw Python batch create ──────────────────────────────────────
cat >"$TMPDIR/app/python/raw_batch.py" <<'PY'
"""Imports raw SDK + calls batches.create — MUST be reported."""
from anthropic import Anthropic

def run() -> None:
    client = Anthropic()
    client.messages.batches.create(requests=[])
PY

# ─── Fixture: raw TypeScript batch create ──────────────────────────────────
cat >"$TMPDIR/app/ts/raw_batch.ts" <<'TS'
// Imports raw SDK + calls batches.create — MUST be reported.
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic();
await client.messages.batches.create({ requests: [] });
TS

# ─── Fixture: raw import only, no batches call (rule 2 only) ───────────────
cat >"$TMPDIR/app/python/raw_import_only.py" <<'PY'
"""Imports raw SDK for a single-shot — flagged by rule 2."""
from anthropic import Anthropic

def run() -> None:
    client = Anthropic()
    client.messages.create(model="claude-haiku-4-5-20251001", messages=[])
PY

# ─── Fixture: node_modules (must be pruned) ────────────────────────────────
mkdir -p "$TMPDIR/app/node_modules/some-lib"
cat >"$TMPDIR/app/node_modules/some-lib/index.ts" <<'TS'
// Vendored code — must be ignored by the checker.
import Anthropic from "@anthropic-ai/sdk";
await new Anthropic().messages.batches.create({ requests: [] });
TS

# ─── Run: expect non-zero exit and two violations reported ─────────────────
set +e
output="$("$CHECKER" "$TMPDIR/app" 2>&1)"
status=$?
set -e

if [[ "$status" -eq 0 ]]; then
    _fail "checker exited 0 on fixtures with violations"
else
    _pass "checker exited non-zero on violations (status=$status)"
fi

# raw_batch.py must be reported (both rule 1 AND rule 2 trip — we check for presence).
if printf '%s' "$output" | grep -q "app/python/raw_batch.py"; then
    _pass "reports raw_batch.py"
else
    _fail "did not report raw_batch.py"
fi

# raw_batch.ts must be reported.
if printf '%s' "$output" | grep -q "app/ts/raw_batch.ts"; then
    _pass "reports raw_batch.ts"
else
    _fail "did not report raw_batch.ts"
fi

# raw_import_only.py must be reported (rule 2 only).
if printf '%s' "$output" | grep -q "app/python/raw_import_only.py"; then
    _pass "reports raw_import_only.py (rule 2)"
else
    _fail "did not report raw_import_only.py"
fi

# compliant.py must NOT be reported.
if printf '%s' "$output" | grep -q "app/python/compliant.py"; then
    _fail "false positive: compliant.py reported"
else
    _pass "compliant.py not reported"
fi

# compliant.ts must NOT be reported.
if printf '%s' "$output" | grep -q "app/ts/compliant.ts"; then
    _fail "false positive: compliant.ts reported"
else
    _pass "compliant.ts not reported"
fi

# node_modules must NOT be scanned.
if printf '%s' "$output" | grep -q "node_modules/some-lib"; then
    _fail "node_modules was not pruned"
else
    _pass "node_modules pruned"
fi

# Output format: path:line: ... (line number present on at least one row).
if printf '%s' "$output" | grep -Eq "app/python/raw_batch\.py:[0-9]+: "; then
    _pass "output has path:line:reason shape"
else
    _fail "output missing line numbers"
fi

# ─── Run: --warn-only must exit 0 but still report ─────────────────────────
set +e
warn_output="$("$CHECKER" --warn-only "$TMPDIR/app" 2>&1)"
warn_status=$?
set -e

if [[ "$warn_status" -eq 0 ]]; then
    _pass "--warn-only exits 0 even with violations"
else
    _fail "--warn-only exited non-zero (status=$warn_status)"
fi

if printf '%s' "$warn_output" | grep -q "app/python/raw_batch.py"; then
    _pass "--warn-only still reports violations"
else
    _fail "--warn-only suppressed output"
fi

# ─── Run: clean tree should exit 0 ──────────────────────────────────────────
CLEAN_DIR="$(mktemp -d -t ci-governor-clean.XXXXXX)"
trap 'rm -rf "$TMPDIR" "$CLEAN_DIR"' EXIT
mkdir -p "$CLEAN_DIR/app"
cat >"$CLEAN_DIR/app/only_compliant.py" <<'PY'
"""Clean file — no raw SDK, no batch call."""
from governor import Anthropic

def run() -> None:
    Anthropic(app="example-app").messages.batches.create(requests=[])
PY

set +e
"$CHECKER" "$CLEAN_DIR/app" >/dev/null 2>&1
clean_status=$?
set -e
if [[ "$clean_status" -eq 0 ]]; then
    _pass "clean tree exits 0"
else
    _fail "clean tree exited non-zero (status=$clean_status)"
fi

# ─── Run: --help works ──────────────────────────────────────────────────────
set +e
help_output="$("$CHECKER" --help 2>&1)"
help_status=$?
set -e
if [[ "$help_status" -eq 0 ]] && printf '%s' "$help_output" | grep -q "Usage:"; then
    _pass "--help shows usage and exits 0"
else
    _fail "--help broken (status=$help_status)"
fi

# ─── Run: unknown flag exits 2 ──────────────────────────────────────────────
set +e
"$CHECKER" --definitely-not-a-flag "$TMPDIR/app" >/dev/null 2>&1
unknown_status=$?
set -e
if [[ "$unknown_status" -eq 2 ]]; then
    _pass "unknown flag exits 2"
else
    _fail "unknown flag exit code: $unknown_status (expected 2)"
fi

echo
echo "ci-governor-check.sh: all assertions passed"
