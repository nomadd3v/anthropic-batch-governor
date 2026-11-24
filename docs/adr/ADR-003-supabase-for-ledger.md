# ADR-003: Supabase Postgres for the audit ledger

**Date:** 2026-04-17  **Status:** Accepted

## Context

Every gate decision (approve/deny with reasons) and every batch lifecycle event (submitted → ended/killed, projected vs actual, variance) needs to be durably logged. The ledger drives:

- Post-incident diagnosis — the 2026-04-15 diagnosis took days because spend data was scattered across agent logs, Anthropic dashboard exports, and Supabase row timestamps. A proper ledger would have made the drift obvious on day one.
- Calibration improvement — comparing projected vs actual over time.
- Compliance / accounting — who spent what, when, why.

## Decision

Use **Supabase Postgres** with a dedicated schema `governor` in the DEV project `<project-ref>`. Two tables:

- `governor.decisions` — every `/v1/check` result.
- `governor.batches` — lifecycle and reconciliation.

RLS is strict: service-role only. No anon reads. Future dashboard access comes through signed JWTs with a read-only claim.

## Consequences

**Positive:**
- Durable. Unlike Upstash TTLs, Postgres rows persist until explicitly archived.
- SQL for variance queries: `select app, avg(variance_pct), percentile_cont(0.95) within group (order by variance_pct) from governor.batches where submitted_at > now() - '7 days'::interval group by app`. Trivial in SQL, painful in Redis.
- Existing RLS patterns — Supabase is already the primary store. Operators know how to query it, dashboards know how to read it.
- Schema separation keeps governance data out of app namespaces.

**Negative:**
- Writing to Postgres on the gate's critical path would add latency. Mitigation: ledger writes are fire-and-forget with a retry queue at `/tmp/governor-ledger-queue/`; the gate's decision latency is dominated by Upstash (ADR-002), not Supabase.
- DEV project is not PROD. For this service specifically, DEV is fine — the audit data is operational, not customer-facing. If this scales, migrate to PROD with a proper retention policy.

**Mitigations:**
- `server/supabase_ledger.py` uses a retry queue so Supabase downtime does not block the gate.
- Ledger writes are async; gate responds before the write completes.

## Alternatives Considered

**Write ledger rows to Upstash lists.** Rejected: no SQL, TTLs mean history evaporates, no useful aggregation.

**Append-only JSONL file on disk.** Rejected: doesn't survive service migration, hard to query, no RLS.

**Dedicated SQLite file.** Rejected: same portability problem as JSONL, plus you still want SQL queries from remote operators.

## Schema

See `sql/001_ledger.sql`. Decisions table holds every approve/deny (jsonb reasons, fingerprint, projected costs, actor). Batches table holds lifecycle + variance.

## References

- 2026-04-15 diagnosis difficulty: `example-app/agent/REFACTOR_PLAN.md`
- Supabase DEV project: `<project-ref>` 
