# Business Rules: Prompt-Cache Break-Even Math

The 2026-04-15 incident partly stemmed from a 1h prompt-cache TTL being used at small N where it **cost more** than no cache at all. This doc encodes the math so the mistake cannot recur silently.

## The core formula

When a batch of N requests shares a common system prompt of S tokens (the part with `cache_control`), and each request i has U_i unique input tokens:

```
C_in(i) = p_in × (m_w × S) / N          # amortized cache write cost per request
        + r_read × S × (N - 1) / N      # cache reads on all but the first
        + p_in × U_i                    # unique portion always pays full price
```

Where:

| Symbol | Meaning | Value |
|---|---|---|
| `p_in` | base input token price (per token) | Haiku: $0.80/M · Sonnet: $3/M |
| `p_out` | base output token price | Haiku: $4/M · Sonnet: $15/M |
| `m_w` | cache write multiplier | 5min: **1.25×** · 1h: **2.0×** |
| `r_read` | cache read discount (10% of input) | Haiku: $0.08/M · Sonnet: $0.30/M |
| `S` | shared system prompt tokens | — |
| `U_i` | unique per-request input tokens | — |
| `N` | number of requests in the cache window | — |

## The break-even point

Compare to no-cache cost (every request pays `p_in × (S + U_i)`). The cache wins when:

```
p_in × S  ≥  (p_in × m_w × S) / N  +  r_read × S × (N-1) / N
```

Solving for N (and simplifying using `r_read = 0.1 × p_in`):

```
N  ≥  (m_w - 0.1) / (1 - 0.1)
```

Which means:

| Cache mode | Break-even N |
|---|---|
| 5-minute (`m_w = 1.25`) | **N ≥ 1.28** → always wins if any repeat |
| 1-hour (`m_w = 2.0`)    | **N ≥ 2.11** → need ≥ 3 requests in the hour |

If N is below the break-even, the cache write cost exceeds what the reads save.

## The 2026-04-15 mistake

The deploy_agent.py code set `cache_mode="1h"` believing it would save money. But at the actual wave sizes (sometimes N=1, N=2, rarely above 5 per cache window), the 2.0× write multiplier lost money on every small wave. Cumulative drift over a rebuild contributed to the $40–65 overrun.

## What the governor does

At `POST /v1/check`, the gate computes the break-even N for the requested `cache_mode` and compares it to the actual batch N. If `cache_mode == "1h" and N < 3`, the gate returns a `CACHE_BREAK_EVEN_WARNING` in the reasons list. The batch is **not** denied — some operators genuinely want to pre-warm caches — but the warning is visible and logged.

## Worked examples

### Example 1: 200-row Sonnet 4.6 menu extraction with 1h cache

- `N = 200`, `S = 5000`, `U_i = 2000`, `O_i(P95) = 2000`
- `p_in = $3/M`, `p_out = $15/M`, `m_w = 2.0`, `r_read = $0.30/M`

Per request (P95):

| Term | Value |
|---|---|
| Amortized write: `3 × (2.0 × 5000) / 200` | $0.00015 |
| Reads on 199/200: `0.30 × 5000 × 199/200 / 1e6` | $0.001493 |
| Unique input: `3 × 2000 / 1e6` | $0.006 |
| Output: `15 × 2000 / 1e6` | $0.030 |
| **Per-request P95** | **~$0.0376** |

Batch P95 total: `200 × $0.0376` ≈ **$7.52**. Inside the $6–8 target for example-app.

No-cache baseline: `200 × (3 × 7000 + 15 × 2000) / 1e6` = **$10.20**. Cache saves ~26%.

### Example 2: 50-row Sonnet with 1h cache (the 2026-04-15 mistake)

Same inputs, `N = 50`:

| Term | Value |
|---|---|
| Amortized write: `3 × (2.0 × 5000) / 50` | $0.0006 |
| Reads on 49/50: `0.30 × 5000 × 49/50 / 1e6` | $0.00147 |
| Unique input | $0.006 |
| Output | $0.030 |
| Per-request P95 | ~$0.0381 |

Batch P95 total: `50 × $0.0381` ≈ **$1.905** vs no-cache `50 × $0.051` ≈ **$2.55**. The cache still wins because N=50 > break-even 2.11.

### Example 3: the actually-bad case — 1h cache at N=2

- `N = 2`, same S and U_i as above
- Amortized write: `3 × (2.0 × 5000) / 2` = $0.015
- Reads on 1/2: `0.30 × 5000 × 1/2 / 1e6` = $0.00075
- Unique input: $0.006
- Output: $0.030
- Per-request P95: ~$0.0518

Batch P95 total: `2 × $0.0518` ≈ **$0.1036**.

No-cache baseline: `2 × 0.051` = **$0.102**.

Cache **loses** (barely). At N=1 it loses by a larger margin. This is the case the governor warns on.

## When to override the warning

Valid reasons to suppress the warning with `suppress_cache_warning: true`:

- Pre-warming a cache for a known-upcoming larger batch within the hour.
- Deliberate testing of cache behavior.

The gate records the suppression in the audit ledger so it's visible in post-flight review.

## Updating this doc

If Anthropic changes cache multipliers or pricing, update `server/calculator/pricing.py` **and** the constants table at the top of this doc. The break-even table derives directly from those numbers; regenerate it.
