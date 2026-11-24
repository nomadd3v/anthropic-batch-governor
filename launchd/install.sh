#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# launchd/install.sh — bootstrap the batch-governor service + monitor.
#
# What this does:
#   1. Validates that required secrets exist in the current shell env.
#   2. Copies both plists to ~/Library/LaunchAgents.
#   3. Publishes ANTHROPIC_API_KEY + Redis vars to the user-agent launchd session
#      via `launchctl setenv`. SUPABASE_* are NOT published — run-service.sh reads
#      them from example-extractor/.env.local to avoid global launchd env pollution.
#   4. Bootstraps (loads) both jobs and kickstarts the first run.
#
# Requirements before running:
#   • Activate direnv at the batch-governor project root (`direnv allow`).
#     This puts UPSTASH_*, ANTHROPIC_API_KEY, SUPABASE_* on your env.
#   • Ensure `.venv` exists and `uvicorn` is installed (pip install -e '.[dev]').
#
# Re-running is safe: we attempt bootout before bootstrap so updated plists
# take effect cleanly.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
SERVICE_PLIST="dev.batchgovernor.service.plist"
MONITOR_PLIST="dev.batchgovernor.monitor.plist"

# Always-required secrets (hot-state store is validated separately below).
# BUSINESS RULE: SUPABASE_* are intentionally excluded from REQUIRED_VARS and
# from launchctl setenv. batch-governor/run-service.sh reads them directly from
# example-extractor/.env.local. Publishing them via
# launchctl setenv would contaminate the global launchd env and bleed credentials
# on daemon restart.
REQUIRED_VARS=(
    ANTHROPIC_API_KEY
)

# Hot state: prefer REDIS_URL (ADR-007, local Redis on :6379). Fall back to
# the legacy Upstash REST pair only if REDIS_URL is unset AND both REST vars
# are set (ADR-002, superseded).
REDIS_VARS_PRESENT=()
if [ -n "${REDIS_URL:-}" ]; then
    REDIS_VARS_PRESENT+=("REDIS_URL")
elif [ -n "${UPSTASH_REDIS_REST_URL:-}" ] && [ -n "${UPSTASH_REDIS_REST_TOKEN:-}" ]; then
    REDIS_VARS_PRESENT+=("UPSTASH_REDIS_REST_URL" "UPSTASH_REDIS_REST_TOKEN")
fi

echo "==> Checking required environment variables"
missing=()
for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var:-}" ]; then
        missing+=("$var")
    fi
done
if [ ${#REDIS_VARS_PRESENT[@]} -eq 0 ]; then
    missing+=("REDIS_URL (or UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN)")
fi
if [ ${#missing[@]} -gt 0 ]; then
    echo "ERROR: missing required env vars: ${missing[*]}" >&2
    echo "Run \`direnv allow\` at the project root first." >&2
    exit 1
fi

echo "==> Publishing secrets to launchd user-agent session"
for var in "${REQUIRED_VARS[@]}" "${REDIS_VARS_PRESENT[@]}"; do
    # ANTHROPIC_API_KEY is deliberately NOT published to the global launchd env.
    # run-service.sh reads it directly from the env file, so the governor never
    # needed it here; a global `launchctl setenv` bled it into every login/GUI
    # process — including every `claude -p` / Claude Code session, which then
    # billed metered API instead of the OAuth subscription. (2026-06-09)
    [ "$var" = "ANTHROPIC_API_KEY" ] && continue
    launchctl setenv "$var" "${!var}"
done

# Optional monitor knobs — only forward when explicitly set.
for optional_var in \
    MONITOR_POLL_INTERVAL_SECONDS \
    MONITOR_RUNAWAY_MULTIPLIER \
    MONITOR_ROGUE_GRACE_SECONDS \
    GOVERNOR_PORT \
    GOVERNOR_BASE_URL; do
    if [ -n "${!optional_var:-}" ]; then
        launchctl setenv "$optional_var" "${!optional_var}"
    fi
done

echo "==> Copying plists to $LAUNCH_AGENTS_DIR"
mkdir -p "$LAUNCH_AGENTS_DIR"
cp "$SCRIPT_DIR/$SERVICE_PLIST" "$LAUNCH_AGENTS_DIR/"
cp "$SCRIPT_DIR/$MONITOR_PLIST" "$LAUNCH_AGENTS_DIR/"

echo "==> Bootstrapping launchd jobs"
# Unload any previous copies first — ignore errors on fresh install.
launchctl bootout "gui/$UID" "$LAUNCH_AGENTS_DIR/$SERVICE_PLIST" 2>/dev/null || true
launchctl bootout "gui/$UID" "$LAUNCH_AGENTS_DIR/$MONITOR_PLIST" 2>/dev/null || true

launchctl bootstrap "gui/$UID" "$LAUNCH_AGENTS_DIR/$SERVICE_PLIST"
launchctl bootstrap "gui/$UID" "$LAUNCH_AGENTS_DIR/$MONITOR_PLIST"

echo "==> Kickstarting both jobs"
launchctl kickstart -k "gui/$UID/dev.batchgovernor.service"
launchctl kickstart -k "gui/$UID/dev.batchgovernor.monitor"

echo ""
echo "Installed. Tail logs with:"
echo "  tail -f /tmp/batch-governor.out.log /tmp/batch-governor.err.log"
echo "  tail -f /tmp/batch-governor-monitor.out.log /tmp/batch-governor-monitor.err.log"
