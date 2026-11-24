# ADR-006: Gate only `messages.batches.create`, not `messages.create`

**Date:** 2026-04-17  **Status:** Accepted

## Context

The Anthropic SDK has two main entry points for cost-bearing API calls:

- `messages.create(...)` — single-shot. Returns immediately.
- `messages.batches.create(...)` — batch. Submits N requests at ~50% cost, runs over hours, results pulled back.

The 2026-04-15 incident involved a batch daemon. The broader inventory (Explore pass) found:

- **2** batch call sites (both in example-app)
- **35+** single-shot call sites across 9 projects

Should the governor gate both, or just batches?

## Decision

**Gate `messages.batches.create` only.** Single-shot `messages.create` is out of scope for v0.1.

## Consequences

**Positive:**
- Migration is minimal: 2 call sites to wrap, not 35+.
- Friction stays where the blast radius is. A single-shot Haiku call costs fractions of a cent; gating it adds DX pain for no meaningful safety.
- Governor code stays simple. Batching has natural gate points (pre-submit); single-shots would require instrumentation per route.

**Negative:**
- A hypothetical future failure mode involving single-shots would not be caught by the governor. If such an incident happens, revisit this ADR.
- The calculator module IS used for single-shot projections (via explicit `calc.projectCost()` calls), so cost visibility exists even without gating. Just no enforcement.

**Mitigations:**
- Calibration feedback from single-shot call sites is captured (`calc.calibrate()`) to improve projections — even without gating, we learn from actuals.
- Apps can opt in to gating individual routes later by importing the wrapper, without any governor code change.

## Alternatives Considered

**Gate everything.** Rejected (user decision). The DX cost across 35+ routes outweighs the risk reduction given that the incident history is 100% batch-driven.

**Gate batches + expensive single-shots (Sonnet vision, image gen).** Considered. Rejected for v0.1 in favor of simplicity. If future incidents come from these routes, revisit — the wrapper library already supports single-shot gating via a flag; only the rollout would need to change.

## User decision

Documented because this is a scope decision made by the user on 2026-04-17. Future expansion requires explicit user sign-off.

## Revisit criteria

Reopen this ADR if any of the following occur:

- A single-shot call site causes a >$10 unexpected spend.
- Cumulative single-shot spend exceeds 10% of total Anthropic spend (measurable via ledger).
- A new project with expensive single-shots comes online (e.g., long-context Opus calls).

## References

- Exploration inventory: 35+ call sites across 9 projects (see /path/to/batch-governor/README.md for list).
- Wrapper library already supports single-shot gating via flag — no code change needed to expand scope later.
