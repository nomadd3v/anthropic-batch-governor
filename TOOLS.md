# TOOLS.md — batch-governor

Describes what the governor does, how it's invoked, environment it needs, constraints discovered during development, and known issues.

## Entries

### batch-governor (FastAPI service)

- **Name / path:** `/path/to/batch-governor/server/app.py`
- **Purpose:** Pre-submit gate + watchdog + audit ledger for Anthropic batches; peer token/cost calculator exposed for all apps.
- **What it achieves:** Makes the 2026-04-15 class of failure (runaway batch, cache miscalc, orphan re-submission, no reconciliation) unrepeatable. Prevents a rogue agent from spending past configured caps. Returns actionable error messages so agents can self-correct.
- **Inputs / env vars:**
  - `REDIS_URL` — state store (default `redis://127.0.0.1:6379/0`, see ADR-007)
  - `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` — optional REST fallback if `REDIS_URL` is unset (originally ADR-002, superseded)
  - `ANTHROPIC_API_KEY` — for batch list/retrieve/cancel
  - `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` — audit ledger
  - `GOVERNOR_CONFIG_PATH` — override for `config/apps.yaml` (default uses repo path)
  - `GOVERNOR_BIND_HOST`, `GOVERNOR_BIND_PORT` — default `127.0.0.1:5185`
- **Constraints & iteration history:**
  - **Why HTTP service not linked libraries:** every app maintaining its own pricing table + Redis client was the exact duplication that enabled the 2026-04-15 incident. One FastAPI process is the consolidation point. See ADR-001.
  - **Why local Redis not Postgres for hot-path state:** round-trip latency on gate checks must be <50ms. Loopback Redis is sub-millisecond; Supabase Postgres via pooler is ~80ms. See ADR-007 (supersedes ADR-002).
  - **Why not Upstash (the ADR-002 plan):** the Upstash instance the original plan assumed (reused from example-extractor) was already dead — the DB had been deleted and example-extractor migrated off Upstash onto Supabase Postgres before this project started. See ADR-007 for the replacement reasoning.
  - **Why Supabase for ledger:** durability + SQL variance queries. Upstash TTLs are too short. See ADR-003.
  - **Why wrapper + CI grep instead of network proxy:** network-layer interception is fragile in Vercel and breaks CI; keys in env still allow `curl` bypass. Honor-system wrapper + CI grep follows the same shape as `whoami-check`. See ADR-004.
  - **Why fail-open not fail-closed:** user decision — outage of governor/Upstash should not block legitimate work. The Anthropic workspace spend cap is the ultimate backstop (though not set yet per user). See ADR-005.
  - **Why batches-only scope:** 35+ single-shot call sites, mostly cheap Haiku, would add friction without proportionate risk reduction. Batches are where incidents happen. See ADR-006.
  - **Port 5185:** default port for the service.
- **Known issues:**
  - Single-shot `messages.create` is not gated (by design, per ADR-006). If a future incident originates from single-shots, revisit.
  - Anthropic workspace spend limit not configured yet (user deferred). Until set, total-bypass is bounded only by the governor itself.
  - `count_tokens` API sometimes rate-limits; calculator falls back to char heuristic and marks source. Watch for elevated `calc:errors:count_tokens` counter.

### batch-governor (bash CLI)

- **Name / path:** `/path/to/batch-governor/batch-governor`
- **Purpose:** Shell-friendly interface to the service for scripts, tmux operators, and quick checks.
- **What it achieves:** One-command check/submit/status/kill/kill-switch. Non-zero exit on DENIED so shell `||` works.
- **Inputs / env vars:**
  - `GOVERNOR_BASE_URL` (default `http://127.0.0.1:5185`)
  - Subcommand args per command (`check <app> <requests.jsonl>`, etc.)
- **Constraints & iteration history:**
  - Style mirrors `a sibling lock CLI` — same shape lets operators move between tools without relearning.
  - Dependencies: `curl` + `jq` only. No Python runtime required for the CLI path.
- **Known issues:**
  - `ledger tail` reads from Supabase via the service; if the service is down, the CLI falls back to a helpful error rather than polling Supabase directly (avoids credential sprawl in the shell path).

### scripts/ci-governor-check.sh

- **Name / path:** `/path/to/batch-governor/scripts/ci-governor-check.sh`
- **Purpose:** Block PRs that import the raw Anthropic SDK for batch calls without the governor wrapper.
- **What it achieves:** Honor-system enforcement backed by lint. Any file with `.batches.create(` must also import `from governor` / `@batch-governor/governor`.
- **Inputs / env vars:** None. Operates on the working tree.
- **Constraints & iteration history:**
  - **Why grep not AST:** fast, dependency-free, works in any repo's pre-commit + GitHub Actions. AST parsing would need separate tooling for Python and TypeScript.
  - **False positives acceptable:** the check intentionally over-fires on ambiguous cases — the cost of a false positive is a 10-second PR comment, the cost of a false negative is $40.
- **Known issues:**
  - Doesn't catch dynamic imports or HTTP-direct calls to `api.anthropic.com`. Those are tracked by the monitor's "rogue batch" detection instead.

### server/monitor.py

- **Name / path:** `/path/to/batch-governor/server/monitor.py`
- **Purpose:** Active watchdog. Polls Anthropic's batch list every 60s; cancels runaway batches; flags rogue batches not registered with the governor.
- **What it achieves:** Real-time protection against "approved a small batch but it ran away" and "someone bypassed the gate".
- **Inputs / env vars:** Same as the service; reads `gov:*` keys from Redis (local `REDIS_URL` by default, see ADR-007).
- **Constraints & iteration history:**
  - **Why separate launchd job:** if the monitor dies, the service continues approving (fail-open); if the service dies, monitor can still cancel in-flight batches.
  - **Why 60s cadence:** balances responsiveness (kill runaway within ~90s) against Anthropic API rate limits on `batches.list`.
  - **30s grace period for rogue detection:** race between `batches.create` returning and the app calling `/v1/register`. Without the grace window, every legitimately-submitted batch would look rogue for the first few seconds.
- **Known issues:**
  - If Anthropic API is slow/unavailable, monitor falls behind; batches could overrun their 1.5× P95 cap before detection. Mitigation: tight per_submit_cap_usd per app so worst-case is bounded.
