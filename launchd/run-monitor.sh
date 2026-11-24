#!/usr/bin/env bash
# Wrapper launched by dev.batchgovernor.monitor.plist.
# Sources credentials from the canonical .env.local, then execs the monitor.
set -euo pipefail

ROOT="/path/to/batch-governor"
ENV_FILE="/path/to/batch-governor/.env.local"

export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"

if [ -f "$ENV_FILE" ]; then
    export ANTHROPIC_API_KEY="$(grep -E '^ANTHROPIC_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
    export SUPABASE_URL="$(grep -E '^NEXT_PUBLIC_SUPABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
    export SUPABASE_SERVICE_ROLE_KEY="$(grep -E '^SUPABASE_SERVICE_ROLE_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
fi

cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m server.monitor
