# Changelog

All notable changes to batch-governor.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Project adheres to semantic versioning.

## [Unreleased]

### Added

- **Permissive mode — governor as auditor instead of gatekeeper** (`GOVERNOR_MODE=permissive|enforcing`, env or `governor_mode:` in `config/apps.yaml`; env wins; default `enforcing` = unchanged behavior). In permissive mode a request that enforcing would DENY is instead APPROVED and **flagged for review** (`would_deny`) rather than blocked. Budget is still reserved and the would-be denial reasons are recorded on the APPROVED audit row's `reasons` column. The emergency kill switch is never auto-approved in either mode.
- **Flag stream (`server/flags.py`)** — two flag kinds share one shape: `would_deny` (permissive-mode approvals that enforcing would deny — records projection, budget/quota state, overage, denial reasons) and `projection_mismatch` (reconciliation actual-vs-projection divergence beyond the 10% variance tolerance — records projection, actual, delta, ratio). `projection_mismatch` fires in BOTH modes since reconciliation is observation, not gating. Two sinks: an append-only JSONL at `GOVERNOR_FLAGS_PATH` (default `~/.governor/flags.jsonl`, rsynced to the mining box) and the existing Supabase `governor` ledger (via the established fire-and-forget insert pattern — off the gate hot path). No Supabase schema change: `would_deny` reuses `governor.decisions.reasons` (jsonb), `projection_mismatch` reuses `governor.batches.variance_pct`.
- **Mode + flag visibility** — every permissive startup logs one loud warning naming the flag stream. `GET /healthz` and `GET /v1/status` now report the active `mode`; `/v1/status` also reports `flags_since_start` (per-kind counters since process start).

### Changed

- **Hot-state store switched from Upstash REST to local Redis on `127.0.0.1:6379`** (ADR-007, supersedes ADR-002). The Upstash instance the original plan assumed (reused from example-extractor) was already dead — example-extractor had migrated to Supabase Postgres before this project started. Local Redis 8.6.2 was already running on the Mac, so hot state now lives on loopback (sub-millisecond) instead of a REST round-trip.
- Default env var is now `REDIS_URL` (default `redis://127.0.0.1:6379/0`). `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN` remain as a fallback if `REDIS_URL` is unset, so Upstash can be re-introduced trivially if the governor ever leaves the Mac.
- Docs updated: README architecture diagram, quickstart env block, ADR index, TOOLS.md constraints section.

### Deprecated

- The `UpstashClient` class name in `server/upstash.py` will be renamed `RedisClient` in a follow-up once the RESP refactor lands. The REST transport path is retained behind the `UPSTASH_REDIS_REST_*` fallback only.

### Fixed

- `sql/001_ledger.sql` now includes `grant usage`/`grant all on tables`/`grant all on sequences` for `service_role`, the default-privileges hook, and a guarded `alter role authenticator set pgrst.db_schemas` block that appends `governor` to the PostgREST-exposed schema list. Discovered while applying the migration to DEV on 2026-04-17: the original file only created the schema + tables + RLS policies, leaving supabase-py writes blocked with `APIError PGRST106: Invalid schema: governor` followed by `42501: permission denied for schema governor`. The revised migration makes a fresh apply work end-to-end without manual dashboard fiddling.

## [0.1.0] — 2026-04-17

### Added

- Initial release following the 2026-04-15 example-app incident ($40–65 rebuild vs $6–8 target).
- FastAPI service on port 5185 hosting two peer modules:
  - **Token Calculator** (`/calc/*`) — standalone cost projector with Anthropic `count_tokens` as source of truth, per-operation reservoir-based P50/P95/P99 output projection, cache-amortization math with break-even warnings.
  - **Batch Governor** (`/v1/*`) — pre-submit gate with 9 denial codes, one-shot submit, kill-switch, post-flight reconciliation.
- Active monitor (watchdog) — polls Anthropic batch list every 60s; cancels batches exceeding 1.5× P95 projection; flags rogue batches not registered with the governor.
- Bash CLI `batch-governor` in lock-CLI style.
- Drop-in `Anthropic` subclass for Python and TypeScript — wraps `messages.batches.create` to route through the gate.
- `scripts/ci-governor-check.sh` — grep-based CI wedge that blocks raw SDK imports without the wrapper.
- Upstash Redis as state store (reuses `UPSTASH_REDIS_REST_URL` from example-extractor).
- Supabase `governor` schema (decisions, batches) in DEV project `<project-ref>`.
- 6 ADRs covering HTTP-over-linked-lib, Upstash, Supabase, wrapper+grep wedge, fail-open mode, batches-only scope.
- 2 business-rule docs covering denial codes and cache break-even math.

### Migrated

- `example-app/agent/pt_rank_batch.py` — routes through governor with `app="example-rank-batch"`.
- `example-app/agent/deploy_agent.py` — routes through governor; startup health check + `allow_daemon` gate in config/apps.yaml.
- `example-app/app/api/extract/route.ts` — pricing constants replaced with calculator client; calibration feedback loop installed.
- `example-extractor/app/api/extract-ai/route.ts` — calibration only (single-shot, no gating).

### Configuration

- Default caps per app: `max_batch_size: 40`, `per_submit_cap_usd: 5.0`, `daily_ceiling_usd: 25.0`, `monthly_ceiling_usd: 300.0`.
- Model allowlist enforced — Sonnet requires explicit opt-in per app.
- `allow_daemon: false` default — long-running daemons must explicitly opt in.

### Known limitations

- Anthropic workspace spend cap not configured (user deferred).
- Single-shot `messages.create` calls are not gated (scope-limited per ADR-006).
- Fail-open mode means an outage of governor or Upstash does not block batches; it logs a warning and notifies.
