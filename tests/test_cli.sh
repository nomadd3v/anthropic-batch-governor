#!/usr/bin/env bash
#
# test_cli.sh — smoke test for the batch-governor bash CLI.
#
# Covers arg parsing and help output only; full end-to-end flow is
# exercised by the Python test suite hitting the HTTP service directly.
# This script does NOT require the service to be running.
#
# Run manually:
#   ./tests/test_cli.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLI="${REPO_ROOT}/batch-governor"

_pass() { echo "  ok   — $1"; }
_fail() { echo "  FAIL — $1"; exit 1; }

echo "batch-governor CLI smoke tests"
echo "------------------------------"

# 1. Help runs and mentions every subcommand.
help_output="$("$CLI" help)"
for sub in check submit status kill kill-switch ledger budget; do
    if printf '%s' "$help_output" | grep -q "batch-governor $sub"; then
        _pass "help mentions '$sub'"
    else
        _fail "help missing '$sub'"
    fi
done

# 2. Unknown subcommand exits non-zero.
if "$CLI" nosuchcommand >/dev/null 2>&1; then
    _fail "unknown subcommand should exit non-zero"
else
    _pass "unknown subcommand → non-zero exit"
fi

# 3. Missing args on 'check' exit non-zero.
if "$CLI" check >/dev/null 2>&1; then
    _fail "'check' with no args should exit non-zero"
else
    _pass "'check' with no args → non-zero exit"
fi

# 4. Missing file on 'check' exits non-zero with clear message.
if "$CLI" check example-rank-batch /tmp/definitely-not-a-real-file.jsonl \
    >/dev/null 2>&1; then
    _fail "'check' with missing file should fail"
else
    _pass "'check' with missing file → non-zero exit"
fi

# 5. Connection error when service unreachable gives the standard hint.
if output=$(GOVERNOR_BASE_URL="http://127.0.0.1:1" \
    "$CLI" status 2>&1); then
    _fail "'status' should fail when service unreachable"
else
    if printf '%s' "$output" | grep -q ":5185" || \
       printf '%s' "$output" | grep -qi "running"; then
        _pass "'status' unreachable → hint surfaced"
    else
        _fail "'status' unreachable did not print hint: $output"
    fi
fi

echo ""
echo "all CLI smoke tests passed."
