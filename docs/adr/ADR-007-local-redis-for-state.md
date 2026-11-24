# ADR-007: Local Redis on :6379 for governor hot state

**Date:** 2026-04-17  **Status:** Proposed (supersedes [ADR-002](ADR-002-upstash-for-state.md))

## Context

ADR-002 assumed Upstash Redis was already wired for the project (via `example-extractor/.env.local`) and could be reused at zero infrastructure cost. That assumption was stale on the day it was written:

- The Upstash host in `.env.local` (`example-host.upstash.io`) resolves NXDOMAIN on both the local Tailscale resolver and Google 8.8.8.8 — the database had been deleted.
- example-extractor had already been migrated off Upstash onto Supabase Postgres. Its replacement schema `menu_import.usage_logs` carries the verbatim header `-- Usage logs (replaces Upstash Redis)`. The `KV_REST_API_*` and `REDIS_URL` env vars in its `.env.local` are residue; nothing still consumes them.
- There are no other live Upstash credentials in the workspace, keychain, or any Vercel project env. The "free" hot-state store the plan relied on does not exist.

During first smoke test we confirmed a **local Redis 8.6.2** is already running on `127.0.0.1:6379` under the service user account and responds to `PING` normally.

The governor's hot-state requirements (from ADR-002 §Context) are unchanged: low-latency atomic increments, sets, hashes, capped lists, and `SETNX` with TTL — every primitive Redis natively supports.

## Decision

Use the **already-running local Redis on `127.0.0.1:6379`** for the governor's hot state. Connect via the standard RESP protocol (async `redis-py`) instead of the Upstash REST shape. The governor process and monitor process both run on the same Mac as Redis, so "local" is the operative word — there is no network round-trip at all.

Credentials: none required (Redis is bound to loopback only; macOS firewall already blocks external traffic).

Configuration: single env var `REDIS_URL` (default `redis://127.0.0.1:6379/0`). Database 0 reserved for governor; we namespace every key with `gov:` / `calc:` prefixes as already designed. A non-loopback `REDIS_URL` works unchanged if the service ever moves.

The existing `UpstashClient` class is refactored to speak RESP in-process. It keeps its name for now — the public surface (`get`, `setex`, `incrbyfloat`, `sadd`, `lpush`, `ltrim`, `setnx_ex`, etc.) maps 1:1 onto native Redis commands. Renaming to `RedisClient` is a follow-up cleanup.

## Consequences

**Positive:**
- Zero new infrastructure, *really* this time — Redis is already installed and running.
- Loopback latency: sub-millisecond. Gate hot path p95 drops well below the 50ms target.
- Native RESP is more efficient than REST under any load; atomic multi-key transactions (`MULTI`/`EXEC`) become available if we ever need them.
- No external service bill, no cold-start, no rate limits.
- Redis persistence (RDB snapshots + AOF) is configurable locally and can be tuned to match the durability we need for quota counters. Governor re-reconciles against the Supabase ledger on boot anyway (audit is authoritative).

**Negative:**
- Coupled to this Mac. If the Mac sleeps, so does the governor — same as the launchd service itself, so no net loss.
- Redis running under the service user means the service must be up before governor starts. launchd dependencies + retry handle this (current plist already has `KeepAlive: true` + `ThrottleInterval: 30`).
- Anyone else running on this Mac (other Claude sessions, other dev work) can read the governor's keys. The `gov:` / `calc:` namespaces isolate by convention. If that becomes a problem we move governor to a dedicated Redis database (`redis://127.0.0.1:6379/1`) or a unix-socket Redis on a non-default port.
- No multi-host HA. Not a requirement — the governor is single-node by design (see ADR-001).

**Mitigations:**
- On Redis outage, the wrapper clients fail open (ADR-005) — exactly the same behavior ADR-002 specified. The only change is what process is down.
- Periodic snapshot check: monitor writes `gov:monitor:heartbeat` every tick; if the heartbeat key evaporates after a restart, the existing `/healthz` degraded-mode alert already fires.

## Alternatives Considered

**Provision a fresh Upstash DB via Vercel marketplace.** Rejected: adds an external dependency, a bill (even if free-tier today), and network latency (15ms on a good day) when we already have sub-millisecond Redis on the same box. The only reason ADR-002 picked Upstash was "zero net-new infra" — that premise evaporates the moment we'd have to provision a new DB.

**Rewrite governor's hot state onto Supabase Postgres (consolidate with example-extractor).** Rejected for the hot path: 80ms+ round-trip, no atomic counters without row-level locking, not a fit for sub-request state. Supabase stays the audit ledger (ADR-003, unchanged).

**`serverless-redis-http` REST shim in front of local Redis.** Considered: preserves the existing `UpstashClient` code literally. Rejected: adds a proxy process for no benefit now that we're committed to a same-box Redis. The in-process RESP rewrite is smaller than maintaining the shim.

**Dedicated Redis on non-default port under launchd.** Considered as a future hardening step if key-namespace isolation proves insufficient. Noted but deferred until the isolation issue actually surfaces.

## Rollout

1. Add `redis[hiredis]>=5` to `pyproject.toml` `dependencies`.
2. Refactor `server/upstash.py` → keep the class shape, swap transport to `redis.asyncio.Redis.from_url(REDIS_URL)`. Every method keeps its signature; tests only change in that the httpx mock becomes a `fakeredis` instance.
3. Update `server/upstash.py`'s module docstring to point at this ADR.
4. Delete the `UPSTASH_REDIS_REST_URL`/`UPSTASH_REDIS_REST_TOKEN` branches from `fromEnv()`; keep them as a fallback only if `REDIS_URL` is unset AND both REST vars are present (so Upstash can come back trivially if needed).
5. Update `launchd/dev.batchgovernor.service.plist` docstring comment to drop the `launchctl setenv UPSTASH_*` lines and add `REDIS_URL`.
6. Update `README.md` and state-store tables.
7. Re-run the full test suite (`pytest -q` + `mypy --strict`) — no behavior changes expected.

## References

- Smoke test on 2026-04-17: `lsof -nP -iTCP:6379 -sTCP:LISTEN` shows `redis-server` pid 2384 bound to `127.0.0.1:6379` and `[::1]:6379`; `PING` → `PONG`; `INFO server` → `redis_version:8.6.2`.
- `reference_adv_menu_import_kv_migration.md` (internal note) — provenance of the stale Upstash wiring.
- ADR-002 (superseded) retains the original requirements list.
- ADR-005 (fail-open with alert) applies unchanged.
