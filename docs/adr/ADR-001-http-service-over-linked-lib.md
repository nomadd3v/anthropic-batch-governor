# ADR-001: HTTP service instead of per-language linked libraries

**Date:** 2026-04-17  **Status:** Accepted

## Context

Nine projects across the monorepo use the Anthropic SDK, split between TypeScript (Next.js, Vite, Edge APIs) and Python (Flask, agent scripts, CLIs). The governor needs to enforce rules consistently across all of them. On 2026-04-15 the example-app daemon burned $40–65 partly because each project maintained its own pricing table, its own cache math, and its own budget logic — duplication that allowed the 1h-cache-TTL miscalculation to go undetected.

## Decision

Ship **one FastAPI process on port 5185** that owns the gate, the calculator, the Upstash state, and the Anthropic batch API client. Apps call it over HTTP. Thin language-specific wrapper clients (`from governor import Anthropic`, `@batch-governor/governor`) hide the HTTP details.

## Consequences

**Positive:**
- One source of truth for pricing, cache math, caps, and calibration data. Updating a pricing constant or a denial rule is a one-line change in one process.
- Language-neutral by construction. New projects (future Rust service? Flask route?) can call the HTTP API without needing an SDK port.
- Centralised audit ledger. Every batch decision is written from one place.
- Easy to put behind a launchd job + monitor; easy to kill-switch globally.

**Negative:**
- Requires the service to be running. Mitigated by fail-open mode (ADR-005) — wrapper falls through to raw SDK with a warning + PushNotification if the service is unreachable.
- ~1–5ms added latency per gate check (HTTP round-trip on localhost). Trivial compared to Anthropic batch round-trip (minutes).
- Operators need to run `uvicorn` + a launchd plist. Documented in README quickstart.

**Mitigations:**
- Fail-open behavior ensures the service being down doesn't block legitimate work.
- Health check + heartbeat mean the monitor can flag "service degraded" quickly.
- The service itself is <500 LOC per-module; maintenance cost is low.

## Alternatives Considered

**Per-language linked library (pip package + NPM package).** Rejected because it's exactly what got us into the 2026-04-15 mess. Each project pulls in the library, pins its own version, and drifts. A shared Upstash state was still needed for cross-process rate limiting — so you'd need credentials in every app's env, and version skew on the pricing table would silently miscalculate cost.

**Network proxy (mitmproxy / /etc/hosts).** Rejected. See ADR-004.

**Hybrid — core logic as library, state server optional.** Considered but adds complexity: library version skew still a risk, and you'd need both moving parts anyway. A service-first approach with thin clients is simpler and sufficient.

## References

- 2026-04-15 incident: `an internal refactor plan`
- Port assignment: project docs
