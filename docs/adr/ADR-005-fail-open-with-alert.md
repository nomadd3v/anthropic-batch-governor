# ADR-005: Fail-open with alert when governor or Upstash is unreachable

**Date:** 2026-04-17  **Status:** Accepted

## Context

The wrapper library depends on the governor service (and the service depends on Upstash). When the governor is unreachable, apps attempting to call `messages.batches.create` must either:

- **Fail-closed:** block the batch with an error until the governor is back up.
- **Fail-open:** pass through to the raw SDK with a logged warning and alert.

The incident that motivated this whole project (2026-04-15) was an over-spend event, not a missed-batch event. Blocking legitimate work during a governor outage is expensive; failing a critical customer batch because a Redis TTL hiccupped is unacceptable.

## Decision

**Fail-open with alert.** If the wrapper cannot reach the governor (timeout, connection refused, 5xx), it:

1. Logs a warning with the request fingerprint, app name, and reason.
2. Fires a PushNotification on the `batch-governor-unreachable` topic.
3. Falls through to the raw `anthropic.Anthropic` SDK.

The resulting batch is not gated, but it is visible — operators can see in real time that a fallback happened and investigate.

The monitor's rogue-batch detection (see ADR-004) still applies: if the monitor is alive but the gate was bypassed, it will flag and cancel. If the monitor is also down, the Anthropic workspace spend cap is the final backstop (not set yet per user).

## Consequences

**Positive:**
- No single point of failure for legitimate batch work. A governor outage degrades to "no enforcement this window" rather than "everything stops".
- Alerts surface the failure. Operators see fallbacks and can act.
- Monitor still catches rogue batches in the common case where only the governor API is down but Upstash and Anthropic are up.

**Negative:**
- During an outage, an adversarial or buggy agent could exceed budget caps. Partially mitigated by the monitor; fully mitigated only by workspace spend cap (future work).
- "Fail-open with alert" is a middle-ground that requires operators to respond to alerts. If alerts are ignored, enforcement is toothless during outages.

**Mitigations:**
- PushNotification is the same channel used for other critical alerts, so it lands in the existing operational stream.
- Warnings include enough detail (app, fingerprint, time) to audit after the fact.
- Eventually set the Anthropic workspace spend cap as the backstop.

## Alternatives Considered

**Fail-closed.** Rejected (user decision). Rationale: single point of failure risk outweighs the enforcement benefit, given this is our first governance layer. Revisit if future incidents suggest otherwise.

**Fail-closed for daemons, open for humans.** Considered — daemons are the rogue-risk surface, humans are typically catching mistakes interactively. Rejected for v0.1 because the user chose simple fail-open; can revisit in v0.2 once the shape of real failures is clearer.

## User decision

Documented here because this ADR encodes a direct user choice (2026-04-17 plan approval) that trades enforcement strictness for availability. Any future shift to fail-closed requires updating this ADR and the wrapper library both.

## References

- Wrapper behavior: `clients/python/governor/__init__.py`, `clients/ts/governor/src/index.ts`
- Alert channel: PushNotification with topic `batch-governor-unreachable`
