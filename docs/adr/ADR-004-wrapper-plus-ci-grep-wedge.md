# ADR-004: Wrapper library + CI grep as the enforcement wedge

**Date:** 2026-04-17  **Status:** Accepted

## Context

A governor that can be bypassed by `pip install anthropic; client.messages.batches.create(...)` provides zero value. The question: how do we make routing through the governor the path of least resistance, and the path that survives CI?

Three options were considered:

1. **Wrapper library** (`from governor import Anthropic`) — drop-in subclass that overrides `batches.create`. Honor system.
2. **CI grep** — static check that fails any PR importing raw SDK without the wrapper.
3. **Network proxy** — intercept `api.anthropic.com` via `/etc/hosts` or mitmproxy.

## Decision

Use **(1) + (2)** together. The wrapper library is the ergonomic path for developers; the CI grep is the hard backstop for PRs.

## Consequences

**Positive:**
- Wrapper library is a one-import change (`from governor import Anthropic` instead of `from anthropic import Anthropic`). Zero friction for compliant code.
- CI grep catches new code that imports raw SDK. A CI-enforced convention — proven enforcement shape.
- Fail-open behavior (ADR-005) means the wrapper is never a blocker when the governor is down.
- Grep is fast, dependency-free, works in any repo's pre-commit + GitHub Actions.

**Negative:**
- Honor system at runtime. A determined agent can still bypass by constructing HTTP requests directly or importing `anthropic.Anthropic` in a file the grep missed.
- Grep has false positives on ambiguous cases. Acceptable: PR comment cost is 10 seconds; $40 cost is 40 dollars. False-positive bias.

**Mitigations:**
- The monitor (`server/monitor.py`) detects any in-flight batch **not** registered with the governor and cancels it as `rogue`. This catches runtime bypass.
- Anthropic workspace spend cap (not yet set, per user) is the ultimate backstop.

## Alternatives Considered

**Network proxy interception.** Rejected:
- Fragile in Vercel — production Next.js apps can't redirect `api.anthropic.com` at the network layer.
- Breaks CI when `api.anthropic.com` is not reachable.
- Keys in env allow `curl https://api.anthropic.com/...` to bypass the proxy entirely.
- mitmproxy-style requires cert trust stores; not a broad win.

**AST-based check instead of grep.** Rejected: adds per-language tooling (Python AST, TypeScript compiler API), slows CI, and provides no real advantage — the grep check is already strict enough to catch drift.

**Anthropic API key rotation + governor-only key.** Considered — could issue a governor-only API key and rotate the shared one. Deferred because rotation logistics across 9 projects is its own project; wrapper + grep is sufficient for now.

## Implementation

- Wrapper: `clients/python/governor/__init__.py`, `clients/ts/governor/src/index.ts`. Both are subclasses of the real SDK; override `messages.batches.create` to call `POST /v1/submit`; preserve all other methods.
- Grep: `scripts/ci-governor-check.sh`. Runs in each repo's pre-commit hook and GitHub Actions workflow. Exits non-zero with a clear message pointing to README migration section.
- Runtime backstop: `server/monitor.py` flags rogue batches with a 30s grace period.

## References

- CI-enforced preflight checks as enforcement precedent
- Monitor rogue-batch logic: `server/monitor.py`
