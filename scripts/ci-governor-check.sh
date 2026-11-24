#!/usr/bin/env bash
#
# ci-governor-check.sh — CI grep wedge for the batch-governor project.
#
# Scans every ``.py``, ``.ts``, and ``.tsx`` file under ``SEARCH_ROOT`` (defaults
# to the current working directory) and fails with a non-zero exit when any
# file violates one of two rules:
#
#   1. Contains ``.batches.create(`` but does NOT import the governor wrapper.
#      (Raw SDK batch create without going through the governor.)
#
#   2. Imports the raw Anthropic SDK (``from anthropic import`` or
#      ``@anthropic-ai/sdk``) without also importing ``@batch-governor/governor`` /
#      ``from governor``. Single-shots are opt-in per ADR-006 but we want
#      visibility before anything new lands.
#
# Paths that legitimately import the raw SDK are allow-listed. See
# ``ALLOWLIST`` below. Skipped directories: ``node_modules``, ``.venv``,
# ``venv``, ``dist``, ``.next``, ``build``, ``target``, ``__pycache__``.
#
# Flags:
#   --warn-only   Print violations and exit 0 (for gradual rollout).
#   -h / --help   Print usage and exit 0.
#
# Exit codes:
#   0  no violations (or --warn-only)
#   1  one or more violations
#   2  bad invocation (unknown flag)
#
# Docs: batch-governor/README.md → "CI enforcement (grep check)".

set -euo pipefail

# ─── Flag parsing ───────────────────────────────────────────────────────────
WARN_ONLY=0
SEARCH_ROOT="${SEARCH_ROOT:-.}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --warn-only)
            WARN_ONLY=1
            shift
            ;;
        -h|--help)
            cat <<'EOF'
Usage: ci-governor-check.sh [--warn-only] [SEARCH_ROOT]

Scans .py/.ts/.tsx files for raw Anthropic SDK usage that skips the
batch-governor wrapper. Exits non-zero on violation (unless --warn-only).

Flags:
  --warn-only   Print violations and exit 0 (for gradual rollout).
  -h, --help    Show this help.

Environment:
  SEARCH_ROOT   Root to scan (default: current directory).
EOF
            exit 0
            ;;
        --)
            shift
            break
            ;;
        -*)
            printf 'ci-governor-check: unknown flag %q\n' "$1" >&2
            exit 2
            ;;
        *)
            SEARCH_ROOT="$1"
            shift
            ;;
    esac
done

if [[ ! -e "$SEARCH_ROOT" ]]; then
    printf 'ci-governor-check: search root %q does not exist\n' "$SEARCH_ROOT" >&2
    exit 2
fi

# ─── Allowlist ──────────────────────────────────────────────────────────────
# Files that legitimately import the raw SDK. Substring match on the full path
# (so ``clients/python/governor/`` also matches tests inside the wrapper dir).
ALLOWLIST=(
    "clients/python/governor/"
    "clients/ts/governor/"
    "server/anthropic_client.py"
    "tests/test_wrapper.py"
)

is_allowlisted() {
    local path="$1"
    local entry
    for entry in "${ALLOWLIST[@]}"; do
        if [[ "$path" == *"$entry"* ]]; then
            return 0
        fi
    done
    return 1
}

# ─── File discovery ─────────────────────────────────────────────────────────
# We deliberately shell out to ``find`` here (not Grep) because the script
# runs in CI environments that may lack ripgrep. Prune paths that never hold
# first-party source.
discover_source_files() {
    find "$SEARCH_ROOT" \
        \( \
            -path '*/node_modules' -o \
            -path '*/.venv' -o \
            -path '*/venv' -o \
            -path '*/dist' -o \
            -path '*/.next' -o \
            -path '*/build' -o \
            -path '*/target' -o \
            -path '*/__pycache__' -o \
            -path '*/.git' \
        \) -prune -o \
        \( -type f \( -name '*.py' -o -name '*.ts' -o -name '*.tsx' \) -print \)
}

# ─── Violation detection ────────────────────────────────────────────────────
# For each suspect file we scan for three features with line numbers and emit
# a violation when rule 1 or rule 2 trips. ``grep -n`` returns the line so we
# can report ``path:line:`` context. The governor import check is deliberately
# loose — any match on ``from governor import`` / ``@batch-governor/governor`` clears
# the file, which means an isolated commented-out import does too. Acceptable
# false-negative bias per ADR-004.
file_imports_raw_sdk() {
    local path="$1"
    if grep -En -- '^[[:space:]]*from[[:space:]]+anthropic[[:space:]]+import|^[[:space:]]*import[[:space:]]+anthropic([[:space:]]|$)' "$path" >/dev/null 2>&1; then
        return 0
    fi
    if grep -En -- "from[[:space:]]+[\"']@anthropic-ai/sdk[\"']|from[[:space:]]+@anthropic-ai/sdk|require\([[:space:]]*[\"']@anthropic-ai/sdk[\"']" "$path" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

file_imports_governor() {
    local path="$1"
    if grep -En -- '^[[:space:]]*from[[:space:]]+governor[[:space:]]+import|^[[:space:]]*import[[:space:]]+governor([[:space:]]|$)' "$path" >/dev/null 2>&1; then
        return 0
    fi
    if grep -En -- "from[[:space:]]+[\"']@batch-governor/governor[\"']|from[[:space:]]+@batch-governor/governor|require\([[:space:]]*[\"']@batch-governor/governor[\"']" "$path" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

report_violation() {
    local path="$1" line="$2" reason="$3"
    printf '%s:%s: %s\n' "$path" "$line" "$reason"
}

# ─── Main scan ──────────────────────────────────────────────────────────────
violations=0
while IFS= read -r path; do
    [[ -z "$path" ]] && continue
    if is_allowlisted "$path"; then
        continue
    fi

    has_governor=0
    if file_imports_governor "$path"; then
        has_governor=1
    fi

    # Rule 1: .batches.create( call without governor import.
    while IFS= read -r hit; do
        [[ -z "$hit" ]] && continue
        line_no="${hit%%:*}"
        if [[ "$has_governor" -eq 0 ]]; then
            report_violation "$path" "$line_no" \
                "raw .batches.create( without governor import — see batch-governor/README.md"
            violations=$((violations + 1))
        fi
    done < <(grep -En -- '\.batches\.create\(' "$path" 2>/dev/null || true)

    # Rule 2: raw SDK import without governor import.
    if file_imports_raw_sdk "$path" && [[ "$has_governor" -eq 0 ]]; then
        import_line="$(grep -En -- '^[[:space:]]*from[[:space:]]+anthropic[[:space:]]+import|^[[:space:]]*import[[:space:]]+anthropic([[:space:]]|$)|from[[:space:]]+[\"'\'']@anthropic-ai/sdk[\"'\'']|from[[:space:]]+@anthropic-ai/sdk|require\([[:space:]]*[\"'\'']@anthropic-ai/sdk[\"'\'']' "$path" 2>/dev/null | head -n1)"
        line_no="${import_line%%:*}"
        [[ -z "$line_no" ]] && line_no="1"
        report_violation "$path" "$line_no" \
            "raw Anthropic SDK import outside allowlist — see batch-governor/README.md"
        violations=$((violations + 1))
    fi
done < <(discover_source_files)

if [[ "$violations" -eq 0 ]]; then
    exit 0
fi

printf '\nci-governor-check: %d violation(s) found.\n' "$violations" >&2
printf 'Fix by importing the governor wrapper:\n' >&2
printf '  Python:     from governor import Anthropic, AsyncAnthropic\n' >&2
printf '  TypeScript: import { GovernorAnthropic } from "@batch-governor/governor"\n' >&2
printf 'See batch-governor/README.md for the full migration guide.\n' >&2

if [[ "$WARN_ONLY" -eq 1 ]]; then
    printf '(--warn-only set — exit 0)\n' >&2
    exit 0
fi
exit 1
