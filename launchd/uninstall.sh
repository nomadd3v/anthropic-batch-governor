#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# launchd/uninstall.sh — stop and remove the batch-governor service + monitor.
#
# Reverse of install.sh. Safe to run multiple times — every step tolerates a
# missing job or file.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
SERVICE_PLIST="dev.batchgovernor.service.plist"
MONITOR_PLIST="dev.batchgovernor.monitor.plist"

echo "==> Booting out launchd jobs"
launchctl bootout "gui/$UID" "$LAUNCH_AGENTS_DIR/$SERVICE_PLIST" 2>/dev/null || true
launchctl bootout "gui/$UID" "$LAUNCH_AGENTS_DIR/$MONITOR_PLIST" 2>/dev/null || true

echo "==> Removing plist files"
rm -f "$LAUNCH_AGENTS_DIR/$SERVICE_PLIST"
rm -f "$LAUNCH_AGENTS_DIR/$MONITOR_PLIST"

echo "Uninstalled. Log files at /tmp/batch-governor*.log were left in place."
