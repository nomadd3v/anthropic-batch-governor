// Error classes + shared payload types for the @batch-governor/governor drop-in SDK wrapper.
//
// Two user-facing shapes:
// * GovernorDeniedError — pre-submit gate refused the batch. Caller inspects
//   ``reasons`` for every rule that failed. Blocking: batch was NOT submitted.
// * GovernorUnreachableError — governor service itself is down. The wrapper
//   catches this internally and falls through to the raw SDK per ADR-005
//   (fail-open). Exported so tests + custom callbacks can introspect.
//
// Mirrors clients/python/governor/exceptions.py — keep the shapes in sync.

/**
 * Wire-shape mirror of ``server.gate.DenialReason``. Matches the JSON returned
 * by ``POST /v1/check`` when the decision is ``DENIED``. See ``server/gate.py``
 * for the source of truth.
 */
export interface DenialReason {
  readonly code: string;
  readonly detail: string;
  readonly fix: string;
}

/**
 * Payload handed to ``onFallback`` when the wrapper fails open.
 *
 * Carries just enough info for a PushNotification payload — which is what
 * Stream F will wire this to. ``topic`` stays on the fixed
 * ``batch-governor-unreachable`` channel so operators can filter for it.
 */
export interface FallbackInfo {
  readonly app: string;
  readonly fingerprint: string;
  readonly reason: string;
  readonly topic: string;
  readonly extra: Readonly<Record<string, unknown>>;
}

/**
 * Raised when the governor's pre-submit gate denies a batch.
 *
 * Inspect ``reasons`` to see every rule that failed; each reason carries
 * ``code``, ``detail``, and a ``fix`` hint written for a human reader. The
 * batch was **not** submitted to Anthropic.
 */
export class GovernorDeniedError extends Error {
  public readonly reasons: readonly DenialReason[];
  public readonly app: string | undefined;
  public readonly fingerprint: string | undefined;

  public constructor(
    reasons: readonly DenialReason[],
    context: { readonly app?: string; readonly fingerprint?: string } = {},
  ) {
    const detail = reasons
      .map((reason) => `${reason.code}: ${reason.detail}`)
      .join("; ");
    super(`batch-governor denied submit (app=${context.app ?? "unknown"}): ${detail}`);
    this.name = "GovernorDeniedError";
    this.reasons = reasons;
    this.app = context.app;
    this.fingerprint = context.fingerprint;
  }
}

/**
 * Raised when the governor's HTTP service is unreachable.
 *
 * The wrapper catches this internally per ADR-005 and falls through to the
 * raw Anthropic SDK. Exposed publicly so tests + the optional ``onFallback``
 * callback can pattern-match on the cause.
 */
export class GovernorUnreachableError extends Error {
  public readonly reason: string;
  public override readonly cause: unknown;

  public constructor(reason: string, cause?: unknown) {
    super(`batch-governor unreachable: ${reason}`);
    this.name = "GovernorUnreachableError";
    this.reason = reason;
    this.cause = cause;
  }
}
