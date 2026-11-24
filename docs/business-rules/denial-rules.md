# Business Rules: Denial Codes

Plain-English statement of every rule the governor enforces at `POST /v1/check`. Each rule maps to a denial code returned to the calling app. Updating a rule requires updating this doc **and** the corresponding section in `server/gate.py`.

## BATCH_SIZE_EXCEEDED

**Rule:** An app may not submit a batch with more requests than its configured `max_batch_size` in `config/apps.yaml` (default **40**).

**Why:** The 2026-04-15 incident involved `WAVE_MAX_SIZE` being bumped from 40 → 200 without re-projecting cost. Capping batch size is the single most effective guard.

**How to comply:** Split the batch, or request an override by setting `allow_large_batch: true` for your app in `config/apps.yaml` (requires code review and an ADR entry).

**Denial response `fix` field example:**
> "batch size 200 exceeds app:example-app max 40; split into 5 batches of 40 or set allow_large_batch: true in config/apps.yaml"

---

## PER_SUBMIT_CAP

**Rule:** A single batch's projected P95 cost may not exceed the app's `per_submit_cap_usd` (default **$5.00**).

**Why:** Caps blast radius of any single submit. Even if daily budget has headroom, no one submit should be able to consume it.

**How to comply:** Reduce batch size, switch to a cheaper model (if allowed), or split the work.

**Denial response `fix` field example:**
> "projected P95 $47.20 exceeds per-submit cap $5.00; split into smaller batches or switch to haiku-4-5"

---

## DAILY_CEILING

**Rule:** An app's cumulative daily spend (projected + actual) may not exceed `daily_ceiling_usd` (default **$25.00**). Counter resets at midnight UTC.

**Why:** Stops runaway loops early. Even a daemon with a stuck submit loop hits this within minutes, not hours.

**How to comply:** Wait for reset, or raise the ceiling in `config/apps.yaml` (again, with review).

**Denial response `fix` field example:**
> "app:example-app has $12.30 remaining today; retry after midnight UTC or adjust daily_ceiling_usd"

---

## MONTHLY_CEILING

**Rule:** An app's cumulative monthly spend may not exceed `monthly_ceiling_usd` (default **$300.00**). Counter resets at the start of each calendar month.

**Why:** Longer-horizon backstop for months with many full rebuilds.

**How to comply:** Wait for reset, or raise the ceiling.

---

## MODEL_NOT_ALLOWED

**Rule:** An app may only submit batches using models in its `allowed_models` list. Default for most apps: `["claude-haiku-4-5-20251001"]`. Sonnet/Opus require explicit opt-in per app.

**Why:** Sonnet is ~4× input price, ~4× output price. An agent that picks Sonnet "just to be safe" on a Haiku-appropriate task quadruples cost invisibly. The default denies; opting in is deliberate.

**How to comply:** Use a cheaper allowed model, or add the model to your app's `allowed_models` in `config/apps.yaml`.

**Denial response `fix` field example:**
> "sonnet-4-6 not in app:example-app allowlist; use haiku-4-5 or add to allowed_models in config/apps.yaml"

---

## MISSING_CUSTOM_ID

**Rule:** Every request within a batch must have a unique `custom_id`. This is required by the reconciliation logic — without it, the governor cannot match Anthropic's response to the original request.

**Why:** Post-flight reconciliation and variance calculation depend on per-request identity.

**How to comply:** Add `custom_id` to every request in the batch. A UUID is fine.

**Denial response `fix` field example:**
> "request[17] missing custom_id (required for reconciliation); add a unique custom_id to each request"

---

## DUPLICATE_FINGERPRINT

**Rule:** If the identical set of requests (as hashed into a fingerprint) has been submitted as an Anthropic batch within the last 2 hours, the governor refuses the duplicate submit.

**Why:** Crash-recovery safety. A daemon that crashes mid-submit and restarts with the same queue would re-submit identical work, doubling cost. The fingerprint check prevents this.

**How to comply:** Check if your previous batch is still in-flight (via `GET /v1/status`) and drain it rather than re-submitting. If you genuinely need to re-run identical work (unusual), wait 2 hours or modify the request set.

**Denial response `fix` field example:**
> "identical request set submitted as batch_abc123 at 2026-04-17T10:22:17Z; drain via GET /v1/status or wait 2h"

---

## KILL_SWITCH_ACTIVE

**Rule:** When `POST /v1/kill-switch on` has been called, the gate denies all submits until `POST /v1/kill-switch off`.

**Why:** Emergency halt. An operator observing a runaway loop needs one command that freezes everything.

**How to comply:** Wait for the kill-switch to be cleared. The denial message includes the reason provided when the switch was set.

---

## CACHE_BREAK_EVEN_WARNING (warning, not denial)

**Rule:** When `cache_mode="1h"` is set and the batch size N < 3, the governor warns because the 1h cache write multiplier (2.0×) will cost more than no cache at that N. This is a **warning**, not a denial — some operators want to pre-warm caches for later batches.

**Why:** This is the exact cache miscalc that contributed to the 2026-04-15 incident. See `cache-break-even.md` for the math.

**How to comply:** Either increase N to ≥3, or switch `cache_mode="5min"` (break-even at N≥1.28), or acknowledge the warning by passing `suppress_cache_warning: true`.

---

## Adding a new denial code

To add a new rule:

1. Add a section to this file (plain English statement + why + how to comply + example).
2. Add a helper function in `server/gate.py` — one early-return per rule. Function name reads like a sentence: `deny_if_batch_size_exceeded()`.
3. Add a test to `tests/test_gate.py` covering both the allow and deny path.
4. Add the code to the `DenialCode` enum in `server/models.py`.
5. Update the wrapper library's docstring so the example response includes the new code.

This process is short on purpose — governance rules should be cheap to add but deliberate. Every new code is a constraint on callers.
