# batch-governor

Anthropic batch governance for apps that call the Anthropic Batch API. Prevents rogue agents from burning the API budget — and gives every project a standalone, accurate token/cost calculator so projections replace assumptions.

Born from the **2026-04-15 incident**: `deploy_agent.py` burned $40–65 on a rebuild that should have cost $6–8. Root causes were uncontrolled batch sizes, miscalculated cache amortization (1h TTL at N=50 costs more than no cache), orphan re-submissions, and no actual-vs-projected reconciliation. This service makes that failure mode unrepeatable.

## What lives here

Two peer modules hosted by one FastAPI service on **port 5185**:

1. **Token Calculator** (`/calc/*`) — standalone cost projector. Callable from any TypeScript or Python project. Uses `count_tokens` as source of truth, reservoir-based P50/P95/P99 output projection, cache-amortization math with break-even warnings.
2. **Batch Governor** (`/v1/*`) — pre-submit gate + active watchdog + audit ledger. Every `messages.batches.create` routes through this.

## Architecture at a glance

```
┌──────────────────────────────────────────────────────────┐
│  App (Python, TS, Next.js, Flask — doesn't matter)        │
│    from governor import Anthropic   # drop-in subclass    │
└───────────────┬──────────────────────────────────────────┘
                │  POST /v1/check   → APPROVED or DENIED(codes)
                │  POST /v1/submit  → Anthropic batches.create
                ▼
┌──────────────────────────────────────────────────────────┐
│  batch-governor service   :5185                          │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐          │
│  │ /calc/*    │  │  /v1/*     │  │  monitor   │          │
│  │ calculator │  │  gate      │  │  watchdog  │          │
│  └────────────┘  └─────┬──────┘  └─────┬──────┘          │
└──────────────────────┼───────────────┼───────────────────┘
                       ▼               ▼
              ┌─────────────┐  ┌───────────────────┐
              │  Local      │  │  Supabase         │
              │  Redis      │  │  governor schema  │
              │  :6379      │  │  (audit ledger)   │
              │  (state)    │  │                   │
              └─────────────┘  └───────────────────┘
```

## Quickstart (New Developer Test: 30 minutes)

```bash
# 1. Install
cd "$HOME/src/batch-governor"
python3 -m venv .venv && source .venv/bin/activate
# (or: uv venv .venv && source .venv/bin/activate)
pip install -e .

# 2. Env — hot state defaults to the local Redis on :6379 (see ADR-007)
export REDIS_URL=redis://127.0.0.1:6379/0
export ANTHROPIC_API_KEY=...
export SUPABASE_URL=https://<project-ref>.supabase.co
export SUPABASE_SERVICE_ROLE_KEY=...
# Optional fallback — only used if REDIS_URL is unset AND both are present:
# export UPSTASH_REDIS_REST_URL=...
# export UPSTASH_REDIS_REST_TOKEN=...

# 3. Apply ledger schema (DEV only — PROD needs explicit approval)
psql "$SUPABASE_DB_URL" -f sql/001_ledger.sql

# 4. Start the service
uvicorn server.app:app --host 127.0.0.1 --port 5185

# 5. In another shell, verify
curl http://127.0.0.1:5185/healthz
./batch-governor status
```

## Tests

```bash
pip install -e '.[dev]'   # pytest, fakeredis etc.
pytest -q                 # hermetic: fake Redis + fake ledger, no network, no API calls
```

## Run as a service (launchd, macOS)

The plists and `launchd/run-*.sh` contain `/path/to/batch-governor` placeholders: replace them with your checkout path (and point `ENV_FILE` at your own env file) before running `launchd/install.sh`.

For persistent operation on macOS, load the service + monitor under `launchd`. This
section is macOS-only; other nodes have no `launchd` and must supervise it another way
(e.g. a cron `@reboot` + minute tick, as rezd does).

```bash
# 1. Activate env at the project root (brings REDIS_URL, ANTHROPIC_API_KEY,
#    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY onto your shell via direnv)
cd "$HOME/src/batch-governor"
direnv allow .

# 2. Install + bootstrap both launchd jobs (safe to re-run to pick up plist edits)
bash launchd/install.sh

# 3. Verify both are running and healthz is green
launchctl list | grep batch-governor
curl -s http://127.0.0.1:5185/healthz
# → {"status":"ok", "monitor_heartbeat_age_s": <60}

# Logs
tail -f /tmp/batch-governor.out.log /tmp/batch-governor.err.log
tail -f /tmp/batch-governor-monitor.out.log /tmp/batch-governor-monitor.err.log

# Uninstall
bash launchd/uninstall.sh
```

The plists call `launchd/run-service.sh` / `launchd/run-monitor.sh` wrappers so credentials are sourced from the canonical `.env.local` at spawn time rather than being embedded in `~/Library/LaunchAgents/*.plist`.

## Using from an app

**Python:**
```python
from governor import Anthropic  # subclass of anthropic.Anthropic

client = Anthropic(app="example-rank-batch")
# Same API as the real SDK — but batches.create routes through the governor.
batch = client.messages.batches.create(requests=[...])
```

**TypeScript:**
```ts
import { Anthropic } from "@batch-governor/governor";
const client = new Anthropic({ app: "example-rank-batch" });
const batch = await client.messages.batches.create({ requests: [...] });
```

When a batch would violate a rule the governor denies it with structured reasons. Each denial includes a `fix` field — agents should read it and adjust. Example:

```json
{
  "decision": "DENIED",
  "reasons": [
    {
      "code": "BATCH_SIZE_EXCEEDED",
      "detail": "batch size 200 exceeds app:example-app max 40",
      "fix": "split into 5 batches of 40 or set allow_large_batch: true in config/apps.yaml"
    }
  ]
}
```

## CLI

```
batch-governor check <app> <requests.jsonl>
batch-governor submit <app> <requests.jsonl>
batch-governor status [app]
batch-governor kill <batch_id>
batch-governor kill-switch on "reason" | off
batch-governor ledger tail [n]
batch-governor budget <app>
```

Mirrors a lock CLI's style — non-zero exit on DENIED so shell callers can `||` correctly.

## Documentation

- [`TOOLS.md`](./TOOLS.md) — tool entry (what, why, iteration history)
- [`docs/adr/`](./docs/adr/) — architectural decision records
  - [ADR-001 — HTTP service over linked lib](./docs/adr/ADR-001-http-service-over-linked-lib.md)
  - [ADR-002 — Upstash for state](./docs/adr/ADR-002-upstash-for-state.md) *(superseded by ADR-007)*
  - [ADR-003 — Supabase for ledger](./docs/adr/ADR-003-supabase-for-ledger.md)
  - [ADR-004 — Wrapper + CI grep wedge](./docs/adr/ADR-004-wrapper-plus-ci-grep-wedge.md)
  - [ADR-005 — Fail-open with alert](./docs/adr/ADR-005-fail-open-with-alert.md)
  - [ADR-006 — Batches-only scope](./docs/adr/ADR-006-batches-only-scope.md)
  - [ADR-007 — Local Redis for state](./docs/adr/ADR-007-local-redis-for-state.md)
- [`docs/business-rules/`](./docs/business-rules/) — plain-English governance rules
  - [denial-rules.md](./docs/business-rules/denial-rules.md) — every denial code explained
  - [cache-break-even.md](./docs/business-rules/cache-break-even.md) — the 2026-04-15 lesson in math form

## Failure modes (summary)

| Failure | Behavior |
|---|---|
| Governor service down | Wrapper library logs warning + fires PushNotification; passes through to raw SDK (**fail-open**, per ADR-005) |
| Redis down | Same as above — gate returns `STATE_UNAVAILABLE` and the wrapper falls through |
| Supabase down | Ledger writes queue to `/tmp/governor-ledger-queue/`; gate does not block |
| Monitor down | Heartbeat expires in 180s; gate continues approving; Anthropic workspace cap is backstop |
| `count_tokens` fails | Calculator falls back to char heuristic, marks `source="heuristic"` |
| No calibration data | Returns caller fallbacks (500/1500/1800), `n_observations: 0` flag |

## Service ports

Port **5185** (override with `GOVERNOR_PORT`).

## Related

- [`docs/adr/`](./docs/adr) — design decisions
- [`docs/business-rules/`](./docs/business-rules) — denial rules and cache break-even math
