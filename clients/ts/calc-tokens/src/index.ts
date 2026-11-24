// @batch-governor/calc-tokens — TypeScript client for the batch-governor token calculator.
// Mirrors clients/python/calc_tokens/client.py. Every method maps 1:1 to an
// endpoint on the FastAPI service.

import type {
  BatchCostProjection,
  BatchRequestSpec,
  CacheMode,
  CalibrationRecord,
  CostProjection,
  InputEstimate,
  ModelPricing,
  OutputProjection,
  PromptMessage,
  PromptSystem,
} from "./types.js";

export * from "./types.js";

// ─── Constants ──────────────────────────────────────────────────────────────
export const DEFAULT_GOVERNOR_BASE_URL: string = "http://127.0.0.1:5185";
export const DEFAULT_TIMEOUT_MS: number = 10_000;

// ─── Options ────────────────────────────────────────────────────────────────
export interface CalcTokensClientOptions {
  readonly baseUrl?: string;
  readonly timeoutMs?: number;
  readonly fetchImpl?: typeof fetch;
}

export interface EstimateInputArgs {
  readonly model: string;
  readonly system: PromptSystem;
  readonly messages: readonly PromptMessage[];
  readonly tools?: readonly Record<string, unknown>[];
  readonly useCountTokens?: boolean;
}

export interface ProjectOutputArgs {
  readonly operation: string;
  readonly model: string;
  readonly fallbackP50: number;
  readonly fallbackP95: number;
  readonly fallbackP99?: number;
}

export interface ProjectCostArgs {
  readonly model: string;
  readonly inputTokens: number;
  readonly output: OutputProjection;
  readonly cacheMode?: CacheMode;
  readonly cachedInputTokens?: number;
}

export interface ProjectBatchArgs {
  readonly requests: readonly BatchRequestSpec[];
  readonly sharedSystemTokens: number;
  readonly cacheMode: CacheMode;
  readonly model: string;
  readonly output: OutputProjection;
}

// ─── Env lookup (safe with or without @types/node) ──────────────────────────
// Reads ``GOVERNOR_BASE_URL`` without assuming a Node global. ``globalThis``
// cast-and-narrow keeps the code usable in browser + edge environments.
function readEnvBaseUrl(): string | undefined {
  const globalScope: { process?: { env?: Record<string, string | undefined> } } =
    globalThis as unknown as {
      process?: { env?: Record<string, string | undefined> };
    };
  const value = globalScope.process?.env?.["GOVERNOR_BASE_URL"];
  return typeof value === "string" ? value : undefined;
}

// ─── HTTP Error ─────────────────────────────────────────────────────────────
export class CalcTokensHttpError extends Error {
  public readonly status: number;
  public readonly body: string;

  public constructor(status: number, body: string, url: string) {
    super(`CalcTokens HTTP ${status} for ${url}: ${body.slice(0, 200)}`);
    this.name = "CalcTokensHttpError";
    this.status = status;
    this.body = body;
  }
}

// ─── Client ─────────────────────────────────────────────────────────────────
export class CalcTokensClient {
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  public constructor(options: CalcTokensClientOptions = {}) {
    const resolved: string =
      options.baseUrl ?? readEnvBaseUrl() ?? DEFAULT_GOVERNOR_BASE_URL;
    this.baseUrl = resolved.replace(/\/+$/, "");
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  // ─── Endpoint: /calc/estimate-input ───────────────────────────────────
  public async estimateInputTokens(
    args: EstimateInputArgs,
  ): Promise<InputEstimate> {
    const body: Record<string, unknown> = {
      model: args.model,
      system: args.system ?? null,
      messages: args.messages,
      tools: args.tools ?? null,
      use_count_tokens: args.useCountTokens ?? true,
    };
    return this.postJson<InputEstimate>("/calc/estimate-input", body);
  }

  // ─── Endpoint: /calc/project-output ───────────────────────────────────
  public async projectOutputDistribution(
    args: ProjectOutputArgs,
  ): Promise<OutputProjection> {
    const body: Record<string, unknown> = {
      operation: args.operation,
      model: args.model,
      fallback_p50: args.fallbackP50,
      fallback_p95: args.fallbackP95,
      fallback_p99: args.fallbackP99 ?? null,
    };
    return this.postJson<OutputProjection>("/calc/project-output", body);
  }

  // ─── Endpoint: /calc/project-cost ─────────────────────────────────────
  public async projectSingleRequestCost(
    args: ProjectCostArgs,
  ): Promise<CostProjection> {
    const body: Record<string, unknown> = {
      model: args.model,
      input_tokens: args.inputTokens,
      output: args.output,
      cache_mode: args.cacheMode ?? "none",
      cached_input_tokens: args.cachedInputTokens ?? 0,
    };
    return this.postJson<CostProjection>("/calc/project-cost", body);
  }

  // ─── Endpoint: /calc/project-batch ────────────────────────────────────
  public async projectBatchCostWithCacheAmortization(
    args: ProjectBatchArgs,
  ): Promise<BatchCostProjection> {
    const body: Record<string, unknown> = {
      requests: args.requests,
      shared_system_tokens: args.sharedSystemTokens,
      cache_mode: args.cacheMode,
      model: args.model,
      output: args.output,
    };
    return this.postJson<BatchCostProjection>("/calc/project-batch", body);
  }

  // ─── Endpoint: /calc/calibrate ────────────────────────────────────────
  public async calibrateWithActualUsage(
    record: CalibrationRecord,
  ): Promise<void> {
    const body: Record<string, unknown> = {
      operation: record.operation,
      model: record.model,
      actual_input: record.actual_input,
      actual_output: record.actual_output,
      actual_cost_usd: record.actual_cost_usd,
    };
    await this.postJson<Record<string, never>>("/calc/calibrate", body);
  }

  // ─── Endpoint: /calc/pricing ──────────────────────────────────────────
  public async getPricingTable(): Promise<Readonly<Record<string, ModelPricing>>> {
    return this.getJson<Readonly<Record<string, ModelPricing>>>("/calc/pricing");
  }

  // ─── HTTP Helpers ─────────────────────────────────────────────────────
  private async postJson<TResult>(
    path: string,
    payload: Readonly<Record<string, unknown>>,
  ): Promise<TResult> {
    const url = `${this.baseUrl}${path}`;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await this.fetchImpl(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      if (!response.ok) {
        const text = await response.text();
        throw new CalcTokensHttpError(response.status, text, url);
      }
      return (await response.json()) as TResult;
    } finally {
      clearTimeout(timeout);
    }
  }

  private async getJson<TResult>(path: string): Promise<TResult> {
    const url = `${this.baseUrl}${path}`;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await this.fetchImpl(url, {
        method: "GET",
        signal: controller.signal,
      });
      if (!response.ok) {
        const text = await response.text();
        throw new CalcTokensHttpError(response.status, text, url);
      }
      return (await response.json()) as TResult;
    } finally {
      clearTimeout(timeout);
    }
  }
}
