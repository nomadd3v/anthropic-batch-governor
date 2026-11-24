// @batch-governor/governor — drop-in Anthropic SDK subclass that routes batches through
// the batch-governor HTTP service.
//
// Usage:
//
//     import { GovernorAnthropic } from "@batch-governor/governor";
//
//     const client = new GovernorAnthropic({ app: "example-app" });
//     const batch = await client.beta.messages.batches.create({ requests: [...] });
//
// Every non-batch call (``messages.create``, ``messages.countTokens``, etc.)
// passes straight through to the real SDK. Only ``beta.messages.batches.create``
// is overridden — which is where the Anthropic TypeScript SDK exposes batches
// today. The override runs three HTTP hops against the governor:
//
// 1. ``POST /calc/project-batch`` — ask the governor to price this batch.
// 2. ``POST /v1/check``           — run the pre-submit rule engine + reserve
//    budget. Returns an ``approval_id`` or a list of ``DENIED`` reasons.
// 3. ``POST /v1/submit``          — hand the approval back so the governor
//    submits to Anthropic itself.
//
// Fail-open (ADR-005): when the governor is unreachable — timeout, connection
// refused, or 5xx — the wrapper logs a ``[BATCH-GOVERNOR-FALLBACK]`` warning,
// fires the optional ``onFallback`` callback, and falls through to the raw
// SDK so legitimate customer work does not block on a governor outage.
//
// Mirrors clients/python/governor/client.py — keep the two in sync.

import Anthropic, { type ClientOptions } from "@anthropic-ai/sdk";

import type { DenialReason, FallbackInfo } from "./exceptions.js";
import {
  GovernorDeniedError,
  GovernorUnreachableError,
} from "./exceptions.js";

export { GovernorDeniedError, GovernorUnreachableError };
export type { DenialReason, FallbackInfo };

// ─── Constants ──────────────────────────────────────────────────────────────
export const DEFAULT_GOVERNOR_BASE_URL: string = "http://127.0.0.1:5185";
export const DEFAULT_GOVERNOR_TIMEOUT_MS: number = 8_000;
export const GOVERNOR_BASE_URL_ENV: string = "GOVERNOR_BASE_URL";
export const GOVERNOR_ACTOR_ENV: string = "GOVERNOR_ACTOR";
export const FALLBACK_LOG_PREFIX: string = "[BATCH-GOVERNOR-FALLBACK]";
export const PUSH_NOTIFICATION_TOPIC: string = "batch-governor-unreachable";

// ─── Public types ───────────────────────────────────────────────────────────
export type FallbackCallback = (info: FallbackInfo) => void;

export interface GovernorClientOptions {
  readonly app: string;
  readonly governorBaseUrl?: string;
  readonly governorTimeoutMs?: number;
  readonly onFallback?: FallbackCallback;
}

export type GovernorAnthropicOptions = GovernorClientOptions & ClientOptions;

/** Shape returned from ``POST /v1/submit`` when the gate approves. */
interface SubmitResponse {
  readonly batch_id: string;
  readonly approval_id: string;
}

/** Shape returned from ``POST /v1/check`` on approval. */
interface CheckApprovedResponse {
  readonly decision: "APPROVED";
  readonly approval_id: string;
  readonly p95_usd: number;
  readonly expires_at: string;
}

/** Shape returned from ``POST /v1/check`` on denial. */
interface CheckDeniedResponse {
  readonly decision: "DENIED";
  readonly reasons: readonly DenialReason[];
}

type CheckResponse = CheckApprovedResponse | CheckDeniedResponse;

/** Minimal ``MessageBatch`` shape — a loose mirror of the SDK type. */
export interface GovernorMessageBatch {
  readonly id: string;
  readonly type: "message_batch";
  readonly processing_status: "in_progress";
  readonly created_at: string;
  readonly expires_at: string;
  readonly request_counts: {
    readonly processing: number;
    readonly succeeded: number;
    readonly errored: number;
    readonly canceled: number;
    readonly expired: number;
  };
}

// ─── Environment helpers ────────────────────────────────────────────────────
// Read env vars without assuming a Node global. ``globalThis`` cast-and-narrow
// keeps the wrapper usable in edge runtimes that expose a shimmed ``process``.
function readEnvVar(key: string): string | undefined {
  const scope: { process?: { env?: Record<string, string | undefined> } } =
    globalThis as unknown as {
      process?: { env?: Record<string, string | undefined> };
    };
  const value = scope.process?.env?.[key];
  return typeof value === "string" ? value : undefined;
}

// BUSINESS RULE: ``actor`` attribution on every governed submit — audit rows
// in the ledger match humans to actions. Mirrors ``resolveActor`` in the
// Python sibling so audit rows look identical regardless of call site.
function resolveActor(): string {
  const explicit = readEnvVar(GOVERNOR_ACTOR_ENV);
  if (explicit !== undefined && explicit !== "") {
    return explicit;
  }
  const user = readEnvVar("USER") ?? readEnvVar("USERNAME") ?? "unknown";
  const host = readEnvVar("HOSTNAME") ?? "unknown-host";
  return `${user}@${host}`;
}

// ─── Fingerprint ────────────────────────────────────────────────────────────
// Runs inside any JS runtime that provides WebCrypto (Node 20+, Deno, Bun,
// modern browsers). The Python sibling uses hashlib.sha256 — same algorithm.
async function sha256Hex(input: string): Promise<string> {
  const cryptoHandle = (globalThis as { crypto?: Crypto }).crypto;
  if (cryptoHandle === undefined || cryptoHandle.subtle === undefined) {
    throw new Error(
      "WebCrypto not available; @batch-governor/governor requires Node 20+ or an equivalent runtime",
    );
  }
  const encoded = new TextEncoder().encode(input);
  const digest = await cryptoHandle.subtle.digest("SHA-256", encoded);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

// Stable JSON — mirrors Python's ``json.dumps(..., sort_keys=True)``.
function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(stableStringify).join(",")}]`;
  }
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record).sort();
  const parts = keys.map(
    (key) => `${JSON.stringify(key)}:${stableStringify(record[key])}`,
  );
  return `{${parts.join(",")}}`;
}

export async function buildBatchFingerprint(args: {
  readonly requests: readonly Record<string, unknown>[];
  readonly model: string;
}): Promise<string> {
  const entries: Array<readonly [string, string]> = [];
  for (const request of args.requests) {
    const customIdRaw = request["custom_id"];
    const customId = typeof customIdRaw === "string" ? customIdRaw : "";
    const paramsRaw = request["params"];
    const params =
      typeof paramsRaw === "object" && paramsRaw !== null
        ? (paramsRaw as Record<string, unknown>)
        : {};
    const paramsHash = await sha256Hex(stableStringify(params));
    entries.push([customId, paramsHash]);
  }
  entries.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  const payload = stableStringify({ model: args.model, entries });
  return sha256Hex(payload);
}

// ─── Projection payload ─────────────────────────────────────────────────────
// Approximate per-request input tokens with a 4-char-per-token heuristic —
// the governor's own ``/calc/project-batch`` path does real estimation. The
// wrapper only pre-counts roughly so callers with huge payloads do not drag
// the governor's estimator into the hot path for every submit.
function buildProjectionPayload(args: {
  readonly requests: readonly Record<string, unknown>[];
  readonly model: string;
}): Record<string, unknown> {
  const items = args.requests.map((request) => {
    const paramsRaw = request["params"];
    const params =
      typeof paramsRaw === "object" && paramsRaw !== null
        ? (paramsRaw as Record<string, unknown>)
        : {};
    const messagesRaw = params["messages"];
    const messages = Array.isArray(messagesRaw) ? messagesRaw : [];
    const approximateChars = messages.reduce<number>((accumulator, message) => {
      return accumulator + JSON.stringify(message ?? "").length;
    }, 0);
    const approximateTokens = Math.max(1, Math.floor(approximateChars / 4));
    return { unique_input_tokens: approximateTokens };
  });
  return {
    requests: items,
    shared_system_tokens: 0,
    cache_mode: "none",
    model: args.model,
    operation: "wrapper.batch.create",
  };
}

function inferBatchModel(
  requests: readonly Record<string, unknown>[],
): string {
  for (const entry of requests) {
    const params = entry["params"];
    if (typeof params === "object" && params !== null) {
      const model = (params as Record<string, unknown>)["model"];
      if (typeof model === "string" && model.length > 0) {
        return model;
      }
    }
  }
  return "";
}

// ─── Fallback alerting ──────────────────────────────────────────────────────
export function fireFallbackAlert(args: {
  readonly app: string;
  readonly fingerprint: string;
  readonly reason: string;
  readonly callback: FallbackCallback | undefined;
  readonly extra?: Readonly<Record<string, unknown>>;
}): void {
  const extra: Record<string, unknown> = { ...(args.extra ?? {}) };
  if (extra["timestamp"] === undefined) {
    extra["timestamp"] = new Date().toISOString();
  }
  const message =
    `${FALLBACK_LOG_PREFIX} app=${args.app} ` +
    `fingerprint=${args.fingerprint.slice(0, 12)} reason=${args.reason}`;

  // BUSINESS RULE: every fallback leaves a stderr trace even without a
  // callback, so CI + local runs carry evidence until Stream F wires
  // PushNotification (ADR-005).
  // eslint-disable-next-line no-console
  console.warn(message);
  if (args.callback === undefined) {
    return;
  }
  const info: FallbackInfo = {
    app: args.app,
    fingerprint: args.fingerprint,
    reason: args.reason,
    topic: PUSH_NOTIFICATION_TOPIC,
    extra,
  };
  try {
    args.callback(info);
  } catch (error) {
    // eslint-disable-next-line no-console
    console.warn(`${FALLBACK_LOG_PREFIX} onFallback callback raised: ${String(error)}`);
  }
}

// ─── Governor HTTP transport ────────────────────────────────────────────────
interface GovernorHttpConfig {
  readonly baseUrl: string;
  readonly timeoutMs: number;
  readonly fetchImpl: typeof fetch;
}

async function postJson<TResult>(
  config: GovernorHttpConfig,
  path: string,
  payload: Readonly<Record<string, unknown>>,
  step: string,
): Promise<TResult> {
  const url = `${config.baseUrl.replace(/\/+$/, "")}${path}`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), config.timeoutMs);
  let response: Response;
  try {
    response = await config.fetchImpl(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
  } catch (error) {
    throw new GovernorUnreachableError(`${step} failed: ${String(error)}`, error);
  } finally {
    clearTimeout(timeout);
  }
  if (response.status >= 500) {
    throw new GovernorUnreachableError(
      `${step} returned ${response.status}`,
    );
  }
  if (!response.ok) {
    const body = await response.text();
    throw new GovernorUnreachableError(
      `${step} rejected: ${response.status} ${body.slice(0, 200)}`,
    );
  }
  return (await response.json()) as TResult;
}

// ─── Governed flow: project → check → submit ───────────────────────────────
async function projectAndSubmit(args: {
  readonly config: GovernorHttpConfig;
  readonly app: string;
  readonly requests: readonly Record<string, unknown>[];
  readonly model: string;
  readonly actor: string;
}): Promise<{
  readonly batchId: string;
  readonly approvalId: string;
  readonly fingerprint: string;
}> {
  const fingerprint = await buildBatchFingerprint({
    requests: args.requests,
    model: args.model,
  });
  const projection = await postJson<Record<string, unknown>>(
    args.config,
    "/calc/project-batch",
    buildProjectionPayload({ requests: args.requests, model: args.model }),
    "calc/project-batch",
  );
  const checkResult = await postJson<CheckResponse>(
    args.config,
    "/v1/check",
    {
      app: args.app,
      requests: args.requests,
      model: args.model,
      projection,
      fingerprint,
      actor: args.actor,
    },
    "v1/check",
  );
  if (checkResult.decision === "DENIED") {
    throw new GovernorDeniedError(checkResult.reasons, {
      app: args.app,
      fingerprint,
    });
  }
  const submitResult = await postJson<SubmitResponse>(
    args.config,
    "/v1/submit",
    { approval_id: checkResult.approval_id },
    "v1/submit",
  );
  return {
    batchId: submitResult.batch_id,
    approvalId: checkResult.approval_id,
    fingerprint,
  };
}

function buildGovernorBatchShape(batchId: string): GovernorMessageBatch {
  const stamp = new Date().toISOString();
  return {
    id: batchId,
    type: "message_batch",
    processing_status: "in_progress",
    created_at: stamp,
    expires_at: stamp,
    request_counts: {
      processing: 0,
      succeeded: 0,
      errored: 0,
      canceled: 0,
      expired: 0,
    },
  };
}

// ─── Batches proxy ──────────────────────────────────────────────────────────
// The SDK's ``beta.messages.batches`` is a class instance with multiple
// methods. We cannot subclass it reliably across SDK minor versions, so
// instead we wrap it in a Proxy that forwards unknown property access and
// replaces ``create`` with our governed implementation.
//
// ``unknown`` is used for the batches type so we can proxy across SDK
// minor versions without tying ourselves to internal ``Batches`` class
// shapes — we only promise the ``create(body)`` entry point.
type OpaqueBatches = object & { create: (...args: readonly unknown[]) => unknown };

function buildGovernedBatches(args: {
  readonly inner: OpaqueBatches;
  readonly config: GovernorHttpConfig;
  readonly app: string;
  readonly onFallback: FallbackCallback | undefined;
}): OpaqueBatches {
  const governedCreate = async (
    body: { readonly requests: readonly Record<string, unknown>[] } & Record<string, unknown>,
    ...extraArgs: readonly unknown[]
  ): Promise<GovernorMessageBatch | unknown> => {
    const requests = body.requests;
    const model = inferBatchModel(requests);
    const actor = resolveActor();
    try {
      const outcome = await projectAndSubmit({
        config: args.config,
        app: args.app,
        requests,
        model,
        actor,
      });
      return buildGovernorBatchShape(outcome.batchId);
    } catch (error) {
      if (error instanceof GovernorUnreachableError) {
        const fingerprint = await buildBatchFingerprint({ requests, model });
        fireFallbackAlert({
          app: args.app,
          fingerprint,
          reason: error.reason,
          callback: args.onFallback,
        });
        const fallbackCreate = args.inner.create.bind(args.inner) as (
          ...callArgs: readonly unknown[]
        ) => unknown;
        return fallbackCreate(body, ...extraArgs);
      }
      throw error;
    }
  };

  // Proxy: forward every property access to the real batches object; only
  // ``create`` is replaced. We use ``Reflect.get`` on the underlying instance
  // so SDK-bound method semantics (``this``) stay intact.
  const handler: ProxyHandler<OpaqueBatches> = {
    get(target, property, receiver) {
      if (property === "create") {
        return governedCreate;
      }
      const value = Reflect.get(target, property, receiver) as unknown;
      return typeof value === "function" ? value.bind(target) : value;
    },
  };
  return new Proxy(args.inner, handler);
}

// ─── Public subclass ────────────────────────────────────────────────────────
// We subclass ``Anthropic`` and override the ``beta`` getter so
// ``beta.messages.batches.create`` routes through our proxy. All other
// accessors (``messages``, ``completions``, ``beta.*`` siblings) pass through
// unchanged.
//
// The SDK's 0.30.x line exposes batches only at ``beta.messages.batches``.
// If the user's code still types ``messages.batches.create`` it will not
// compile against that SDK version — in which case, they were already
// calling the raw SDK improperly. We do not add a shim there.
export class GovernorAnthropic extends Anthropic {
  private readonly _governorApp: string;
  private readonly _governorConfig: GovernorHttpConfig;
  private readonly _governorOnFallback: FallbackCallback | undefined;
  private _governedBatches: OpaqueBatches | undefined;
  private _governedBeta: unknown | undefined;

  public constructor(options: GovernorAnthropicOptions) {
    const {
      app,
      governorBaseUrl,
      governorTimeoutMs,
      onFallback,
      ...sdkOptions
    } = options;
    super(sdkOptions);
    this._governorApp = app;
    this._governorConfig = {
      baseUrl:
        governorBaseUrl ??
        readEnvVar(GOVERNOR_BASE_URL_ENV) ??
        DEFAULT_GOVERNOR_BASE_URL,
      timeoutMs: governorTimeoutMs ?? DEFAULT_GOVERNOR_TIMEOUT_MS,
      fetchImpl:
        ((globalThis as { fetch?: typeof fetch }).fetch ??
          undefined) as typeof fetch,
    };
    this._governorOnFallback = onFallback;
  }

  // ``beta`` is a property on the real SDK, not an accessor; we override it
  // as a getter on the subclass. The getter builds a proxy that exposes our
  // governed batches object under ``messages.batches`` and delegates every
  // other access to the underlying ``beta`` instance.
  public get governedBeta(): unknown {
    if (this._governedBeta === undefined) {
      const rawBeta = (this as unknown as { beta: unknown }).beta;
      this._governedBeta = this._buildGovernedBeta(rawBeta);
    }
    return this._governedBeta;
  }

  private _buildGovernedBeta(rawBeta: unknown): unknown {
    if (typeof rawBeta !== "object" || rawBeta === null) {
      return rawBeta;
    }
    const rawMessages = (rawBeta as { messages?: unknown }).messages;
    if (typeof rawMessages !== "object" || rawMessages === null) {
      return rawBeta;
    }
    const rawBatches = (rawMessages as { batches?: unknown }).batches;
    if (typeof rawBatches !== "object" || rawBatches === null) {
      return rawBeta;
    }
    const governedBatches = this._ensureGovernedBatches(
      rawBatches as OpaqueBatches,
    );
    const messagesProxy = new Proxy(rawMessages as object, {
      get: (target, property, receiver) => {
        if (property === "batches") {
          return governedBatches;
        }
        const value = Reflect.get(target, property, receiver) as unknown;
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    return new Proxy(rawBeta as object, {
      get: (target, property, receiver) => {
        if (property === "messages") {
          return messagesProxy;
        }
        const value = Reflect.get(target, property, receiver) as unknown;
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
  }

  private _ensureGovernedBatches(inner: OpaqueBatches): OpaqueBatches {
    if (this._governedBatches === undefined) {
      this._governedBatches = buildGovernedBatches({
        inner,
        config: this._governorConfig,
        app: this._governorApp,
        onFallback: this._governorOnFallback,
      });
    }
    return this._governedBatches;
  }
}

export default GovernorAnthropic;
