// Shared types for @batch-governor/calc-tokens.
// Mirror the pydantic models in server/calculator/*.py. Drift here is a silent
// bug — keep these in lockstep with the server.

export type CacheMode = "none" | "5min" | "1h";

export type EstimationSource = "api" | "heuristic";

export type CalibrationSource = "reservoir" | "fallback";

export interface InputEstimate {
  readonly tokens: number;
  readonly source: EstimationSource;
  readonly error: string | null;
}

export interface OutputProjection {
  readonly p50: number;
  readonly p95: number;
  readonly p99: number;
  readonly n_observations: number;
  readonly source: CalibrationSource;
}

export interface CostBreakdown {
  readonly input_cost_usd: number;
  readonly cache_read_cost_usd: number;
  readonly cache_write_cost_usd: number;
  readonly output_cost_usd: number;
  readonly total_usd: number;
}

export interface CostProjection {
  readonly p50_usd: number;
  readonly p95_usd: number;
  readonly p99_usd: number;
  readonly breakdown_p95: CostBreakdown;
  readonly model: string;
  readonly cache_mode: CacheMode;
}

export interface BatchRequestSpec {
  readonly unique_input_tokens: number;
}

export interface BatchCostProjection {
  readonly n_requests: number;
  readonly shared_system_tokens: number;
  readonly cache_mode: CacheMode;
  readonly model: string;
  readonly total_p50_usd: number;
  readonly total_p95_usd: number;
  readonly total_p99_usd: number;
  readonly per_request_p95_usd: number;
  readonly cache_break_even_warning: boolean;
  readonly cache_break_even_min_n: number;
  readonly break_even_detail: string;
}

export interface ModelPricing {
  readonly model_id: string;
  readonly input_per_token: number;
  readonly output_per_token: number;
  readonly cache_read_per_token: number;
  readonly cache_write_5min_multiplier: number;
  readonly cache_write_1h_multiplier: number;
}

export interface CalibrationRecord {
  readonly operation: string;
  readonly model: string;
  readonly actual_input: number;
  readonly actual_output: number;
  readonly actual_cost_usd: number;
}

// ─── Prompt shapes accepted by /calc/estimate-input ──────────────────────
// Mirror the Anthropic SDK content-block shapes the estimator understands.

export interface TextBlock {
  readonly type: "text";
  readonly text: string;
}

export interface ImageBlock {
  readonly type: "image";
  readonly tile_count?: number;
  readonly source?: unknown;
}

export type ContentBlock = TextBlock | ImageBlock | Record<string, unknown>;

export interface PromptMessage {
  readonly role: "user" | "assistant";
  readonly content: string | readonly ContentBlock[];
}

export type PromptSystem =
  | string
  | readonly ContentBlock[]
  | null
  | undefined;

// ─── Discriminated union for the /calc/* call results ────────────────────
// Every CalcTokensClient method returns one of these. Callers can narrow on
// ``kind`` in a single switch.

export type CalcResult =
  | { readonly kind: "input_estimate"; readonly value: InputEstimate }
  | { readonly kind: "output_projection"; readonly value: OutputProjection }
  | { readonly kind: "cost"; readonly value: CostProjection }
  | { readonly kind: "batch_cost"; readonly value: BatchCostProjection }
  | { readonly kind: "pricing"; readonly value: Readonly<Record<string, ModelPricing>> };
