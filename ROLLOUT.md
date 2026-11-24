# Rollout — migrating your app onto the governor

Single-page checklist for any app that calls Anthropic
batches or single-shot. After the 2026-04-15 incident and the 2026-04-17
governance restructure, **all Anthropic calls route through
the governor wrapper, and every batch requires the operator's tap-approval
before it submits.**

Service is live on the Mac at `http://127.0.0.1:5185` under launchd
(`dev.batchgovernor.service` + `-monitor`, `KeepAlive=true`).

---

## What changed (2026-04-17)

The governor is now the single source of truth for projection and the
sole approver of submission. Callers stopped carrying pricing constants,
cache-amortization math, per-app caps, velocity limits, and spend
tracking. Those all live in the governor, backed by its reservoir +
Supabase ledger history.

**Caller responsibilities collapsed to four things.** If your app did
more than this before, delete the rest.

---

## TL;DR — the 4-step contract

1. Swap import to `from governor import Anthropic`.
2. Use `calc.project(...)` for every cost projection you show the operator. Never hardcode pricing, never count tokens yourself, never compute cache amortization. If your repo has any of that code, delete it.
3. Submit via the wrapper. The call blocks until the operator taps approve on a PushNotification. Plan for ≤30-min wait, or an `APPROVAL_TIMEOUT` denial.
4. Call `calc.calibrate(...)` after every run so the reservoir improves.

The CI grep enforces the no-local-math rule — it now blocks hardcoded
`$/Mtok` constants and manual `count_tokens` usage in caller repos in
addition to raw SDK imports.

---

## 1. Swap the import

### Python

```diff
- from anthropic import Anthropic
+ from governor import Anthropic

- client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
+ client = Anthropic(app="example-rank-batch")  # key still picked up from env
```

`governor.Anthropic` is a drop-in subclass of `anthropic.Anthropic`. It
overrides `messages.batches.create` and `messages.create` to route
through `/v1/submit`. Streaming and other SDK calls pass through.

Install (in your app's venv):

```bash
pip install -e /path/to/batch-governor/clients/python/governor
```

### TypeScript

```diff
- import Anthropic from "@anthropic-ai/sdk";
+ import { Anthropic } from "@batch-governor/governor";

- const client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
+ const client = new Anthropic({ app: "example-extractor-extract" });
```

Package at `clients/ts/governor` — consume via workspace path or `npm link`.

---

## 2. Projection — governor is the only source of truth

**You do not maintain pricing constants, token counting, or cache math.**
Delete anything like `PRICING_PER_MTOK_HAIKU = 0.80` or
`estimated_tokens = len(prompt) // 4` from your codebase. The calculator
client is a thin wrapper that always hits the governor.

### Python

```python
from calc_tokens import Calculator
calc = Calculator()

projection = calc.project(
    app="example-rebuild-batch",
    operation="pt-rank",                      # reservoir key
    model="claude-haiku-4-5-20251001",
    system_prompt=system,
    messages=messages,
    tools=tools,                              # optional
    cache_mode="5min",                        # "none" | "5min" | "1h"
    n_requests=len(requests),                 # for batch amortization
)

# projection.p95_usd — this is the number you show the operator when asking
# him to approve the run.
print(f"Rebuild projected at ${projection.p95_usd:.2f} "
      f"(reservoir n={projection.n_observations}).")
```

### TypeScript

```ts
import { Calculator } from "@batch-governor/calc-tokens";
const calc = new Calculator();
const projection = await calc.project({
  app: "example-pipeline",
  operation: "tier2-score",
  model: "claude-sonnet-4-6",
  systemPrompt, messages, cacheMode: "none", nRequests: 1,
});
// projection.p95Usd
```

### What `calc.project` returns

```ts
{
  p50_usd: 3.12,
  p95_usd: 8.40,
  p99_usd: 11.20,
  input_tokens: 42180,
  output_p50: 510,
  output_p95: 1480,
  n_observations: 47,              // 0 if this (operation, model) is new
  source: "reservoir" | "ledger-backfill" | "heuristic",
  flags: string[],                 // e.g. ["thin_reservoir", "new_combo"]
}
```

If `n_observations == 0` or `flags` is non-empty, the governor will
anomaly-flag this projection to the operator on submission. The projection
number is still usable — it just means reservoir confidence is low.

### Fail-closed

If the governor is unreachable, `calc.project()` **raises an exception**.
There is no fallback math, because the whole point is that the operator sees a
real number before approving. If projection is broken, don't submit.

(This is the opposite of the submit path, which fails open — see §6.)

---

## 3. Submit — the gate asks the operator

`client.messages.batches.create(...)` goes through `/v1/submit`. The
gate runs hard checks (size, model, fingerprint, sanity ceilings), then
fires a PushNotification to the operator with the governor's projection. Your
call blocks for up to 30 minutes waiting for his tap.

```python
batch = client.messages.batches.create(
    requests=[...],                           # each with stable custom_id
)
# This call blocks seconds-to-minutes. Handle denials below.
```

### What the operator sees on their phone

```
example-rebuild-batch
27 requests, Sonnet 4.6, 5min cache
Governor P95: $8.40 (reservoir n=47, confidence: ok)
Today: $0.00 · Month: $12.40
[ Approve ]   [ Deny ]
```

Tap approve → your call returns with an approved batch. Tap deny or
30-min timeout → your call raises an exception carrying a denial code.

### Denials you must handle

| Code | Meaning | What caller should do |
|---|---|---|
| `HUMAN_DENIED` | the operator tapped deny | Abort; do not retry without operator guidance |
| `APPROVAL_TIMEOUT` | 30 min no tap | Abort; surface to operator |
| `BATCH_SIZE_EXCEEDED` | batch bigger than your app's `max_batch_size` | Split or stop |
| `MODEL_NOT_ALLOWED` | model not in allowlist for your app | Halt; request allowlist update via ADR |
| `MISSING_CUSTOM_ID` | a request has no stable `custom_id` | Assign stable ids — reconciliation requires them |
| `DUPLICATE_FINGERPRINT` | identical request set already submitted | Do NOT resubmit (crash-recovery protection); adopt existing run |
| `SANITY_CEILING_EXCEEDED` | projected cost exceeds the governor's sanity ceiling | Your projection is probably broken — investigate before re-asking |
| `KILL_SWITCH_ACTIVE` | operator hit the red button | Wait; do not retry |
| `PROJECTION_DIVERGENCE` | check-time projection drifted >10% from what the operator pre-approved | Re-request approval with the new number |

Every denial includes a `fix` field with a human-readable hint.

---

## 4. Calibrate after every run

When Anthropic returns usage numbers, report them back so the reservoir
tracks reality. Without this the projection degrades over time.

```python
calc.calibrate(
    app="example-rebuild-batch",
    operation="pt-rank",
    model="claude-haiku-4-5-20251001",
    actual_input_tokens=usage.input_tokens,
    actual_output_tokens=usage.output_tokens,
    actual_cost_usd=reported_cost,
)
```

TypeScript:
```ts
await calc.calibrate({ app, operation, model, actualInputTokens,
                       actualOutputTokens, actualCostUsd });
```

This is how `calc.project` gets smarter. Skip it and the reservoir
stagnates — your next projection for the operator will be based on stale data.

---

## 5. CI grep before pushing

```bash
bash /path/to/batch-governor/scripts/ci-governor-check.sh <path-to-your-repo>
```

Fails the commit if any file in the caller repo:

- imports raw `anthropic` / `@anthropic-ai/sdk` without a sibling `from governor` / `@batch-governor/governor` import
- contains hardcoded pricing constants (`$/Mtok`, `PRICING_*`, per-million rates)
- calls `client.messages.count_tokens(...)` directly (should go through `calc.project` which counts tokens governor-side)
- computes cache amortization (`* 1.25`, `* 2.0`, break-even constants)

Wire into pre-commit and GitHub Actions.

---

## 6. Fail-open on submit, fail-closed on project

Two different failure modes on purpose:

- **`calc.project()` fails closed** — if the governor is down, the call raises. No fallback math. The operator must see the real number before approving, so a broken governor blocks the approval path entirely.
- **`messages.batches.create()` fails open** — if the governor is down mid-submit, the wrapper logs `[BATCH-GOVERNOR-FALLBACK]`, fires a PushNotification with the fingerprint, and passes through to the raw SDK. The batch still runs. Rationale: if the operator already approved the work conceptually, a governor outage shouldn't block legitimate work from completing.

The inconsistency is deliberate. Projection is a planning activity with
the operator in the loop; submit is an already-blessed action.

---

## 7. What is NOT your concern (governor-side policy)

You don't configure, read, or react to any of this — the governor owns it:

- Daily / monthly ceilings
- Velocity limits
- `allow_daemon` — categorically off. No app re-enables batch-submitting daemons; the operator initiates every batch.
- Runaway cancellation (>1.5× P95)
- Rogue-batch detection + cancel
- Reconciliation, variance tracking, anomaly routing
- Org-wide spend budgets
- `apps.yaml` contents

If you think you need one of these, you probably don't — ask the operator.

---

## 8. Migration checklist

For each caller repo:

- [ ] Swap the import to `from governor import Anthropic` / `@batch-governor/governor`
- [ ] Pass `app="<slug>"` to the constructor
- [ ] Delete all pricing constants (`$/Mtok`, `PRICING_*`, `COST_PER_MTOK_*`)
- [ ] Delete all manual token counting (`count_tokens`, `len(prompt) // 4` heuristics)
- [ ] Delete all cache-amortization math (`* 1.25`, `* 2.0`, break-even logic)
- [ ] Replace local projection with `calc.project(...)` — this is the number you show the operator
- [ ] Add `calc.calibrate(...)` after every Anthropic response
- [ ] Add CI grep to pre-commit and GHA
- [ ] Ensure every request has a stable `custom_id`
- [ ] Handle the 9 denial codes (table in §3) — abort on most, never auto-retry on `HUMAN_DENIED` / `DUPLICATE_FINGERPRINT`

---

## Scope — what the governor does NOT catch

The governor is a guardrail for good-faith agents plus a human-approval
control, not adversarial defense:

- Raw `import anthropic` in a local one-off script that never hits CI.
- Direct `httpx` / `curl` to `api.anthropic.com`.
- A different Anthropic key/workspace — invisible to the monitor.
- Rogue agents that decide not to route through the wrapper.

Hard catch-all would be an Anthropic workspace-level spend cap (deferred —
all apps share one key, so a workspace cap can't discriminate per-app).
Rogue-batch detection in the monitor is the backstop within our
workspace: unregistered batches are flagged and cancelled within ~90s.

---

## Questions / gotchas

- **Service URL override:** `GOVERNOR_BASE_URL` (default `http://127.0.0.1:5185`).
- **Keys:** always in env (`ANTHROPIC_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`). Never in `apps.yaml`.
- **Service + monitor autostart** on login via launchd. Manual restart:
  `launchctl kickstart -k gui/$UID/dev.batchgovernor.service{,-monitor}`.
- **Approval UX** — if you're not getting PushNotifications, check `batch-governor status`; the approval queue shows pending items and you can `batch-governor approve <approval_id>` from the CLI as a fallback.
- **Operation labels matter** — the reservoir is keyed on `(operation, model)`. Use stable, descriptive labels (`pt-rank`, `tier2-score`, `menu-extract-pdf`). Changing the label orphans the reservoir.
- See `README.md` §Run as a service, §CLI, §Failure modes for operator workflows.
