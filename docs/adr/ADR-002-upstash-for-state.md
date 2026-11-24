# ADR-002: Upstash Redis for governor state

**Date:** 2026-04-17  **Status:** Superseded by [ADR-007](ADR-007-local-redis-for-state.md) (same day, pre-launch)

> **2026-04-17 update:** The premise below — "Upstash already wired for
> example-extractor, reuse it" — was stale when written. During first smoke
> test the Upstash host `example-host.upstash.io` resolved
> NXDOMAIN on both Tailscale and Google DNS: the database had been
> deleted. example-extractor was already migrated to Supabase Postgres
> (`menu_import.usage_logs` table, header `-- Usage logs (replaces
> Upstash Redis)`). The `KV_*` env vars are residue. See ADR-007 for
> the replacement decision (local Redis on `127.0.0.1:6379`).
> The rest of this ADR is preserved for historical context — the
> requirements list in §Context still applies.

## Context

The governor needs a shared state store for quota counters, inflight batch sets, approval TTLs, fingerprint dedup, and the calculator's per-operation calibration reservoirs. The store must:

1. Be low-latency on the gate hot path (gate check target: <50ms p95).
2. Support atomic increments (`INCRBYFLOAT`) for daily/monthly spend counters.
3. Support sets (for inflight batch IDs), hashes (for approvals), capped lists (for reservoirs), and `SETNX` with TTL (for fingerprint dedup).
4. Be available across processes — gate, monitor, wrapper clients all read/write.
5. Not require new infrastructure.

## Decision

Use **Upstash Redis** via its REST API. `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN` are already in `example-extractor`'s `.env.local`; reuse the same credentials (noted in suite root CLAUDE.md shared infrastructure).

## Consequences

**Positive:**
- Zero new infrastructure. Credentials already exist.
- REST API means no TCP pool management; trivial from FastAPI, TypeScript, and bash.
- Redis primitives (hashes, sets, lists, TTLs, atomic increments) match every governor state requirement exactly.
- Upstash REST latency from Mac on Wi-Fi: ~15ms p95. Well under the 50ms target.
- Free tier + pay-per-request pricing — negligible at governor volume (hundreds of ops per day).

**Negative:**
- External dependency — if Upstash is down, gate degrades (fail-open, ADR-005). Acceptable because the wedge is honor-system anyway.
- REST API is not as efficient as native Redis wire protocol for high QPS. Governor volume is nowhere near that limit.
- No native transactions across keys; we use `SETNX` + atomic increments carefully.

## Alternatives Considered

**Supabase Postgres.** Rejected for hot-path state: round-trip latency through pooler is ~80ms and spikes worse under load. Fine for the audit ledger (ADR-003) but wrong for per-request quota checks.

**Local SQLite / filesystem.** Rejected: governor state must be visible across monitor process + service process + (potentially) multiple service replicas. File locking is the wrong primitive.

**In-memory only.** Rejected: monitor restarts would lose inflight set and quota counters. Fail-closed during restart windows is worse UX than the failure modes Upstash introduces.

## References

- Upstash credentials: `example-extractor/.env.local`
- project docs
