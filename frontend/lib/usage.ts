import type { Message, Pricing } from "./api";

const TOKENS_PER_UNIT = 1_000_000;

export interface ModelUsage {
  model: string;
  promptTokens: number;
  completionTokens: number;
  answers: number;
  /** Null when the model has no published rate — absent, not zero. */
  costUsd: number | null;
}

export interface UsageSummary {
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  answers: number;
  inFlight: number;
  /** Null if nothing priceable has completed yet. */
  costUsd: number | null;
  /** True when at least one completed answer used a model with no known rate. */
  partial: boolean;
  byModel: ModelUsage[];
}

/**
 * Total a conversation's usage from rows the client already holds.
 *
 * Deliberately derived rather than fetched: the same frames that fill the
 * bubbles carry the token counts, so the dashboard stays live without polling
 * and can never disagree with what is on screen.
 *
 * This is transparency, not enforcement — nothing here gates a request. An
 * unpriced model contributes tokens but no cost, and flags the total as partial
 * so an underestimate is never presented as exact.
 */
export function summarize(messages: Message[], pricing: Pricing | null): UsageSummary {
  const byModel = new Map<string, ModelUsage>();
  let promptTokens = 0;
  let completionTokens = 0;
  let answers = 0;
  let inFlight = 0;
  let cost: number | null = null;
  let partial = false;

  for (const message of messages) {
    if (message.status === "pending" || message.status === "streaming") {
      inFlight++;
      continue;
    }
    // Usage is only meaningful once a job has reported it.
    if (message.prompt_tokens === null && message.completion_tokens === null) continue;

    const p = message.prompt_tokens ?? 0;
    const c = message.completion_tokens ?? 0;
    promptTokens += p;
    completionTokens += c;
    answers++;

    const model = message.model ?? "unknown";
    const rate = pricing?.models[model] ?? null;
    const entry = byModel.get(model) ?? {
      model,
      promptTokens: 0,
      completionTokens: 0,
      answers: 0,
      costUsd: rate ? 0 : null,
    };
    entry.promptTokens += p;
    entry.completionTokens += c;
    entry.answers++;

    if (rate) {
      const spend = (p * rate.input + c * rate.output) / TOKENS_PER_UNIT;
      entry.costUsd = (entry.costUsd ?? 0) + spend;
      cost = (cost ?? 0) + spend;
    } else {
      partial = true;
    }
    byModel.set(model, entry);
  }

  return {
    promptTokens,
    completionTokens,
    totalTokens: promptTokens + completionTokens,
    answers,
    inFlight,
    costUsd: cost,
    partial,
    byModel: [...byModel.values()].sort((a, b) => b.completionTokens - a.completionTokens),
  };
}

/** Sub-cent spend is normal here, so a fixed 2dp would read as a flat $0.00. */
export function formatUsd(value: number | null): string {
  if (value === null) return "—";
  if (value === 0) return "$0.00";
  if (value < 0.01) return `$${value.toFixed(5)}`;
  return `$${value.toFixed(4)}`;
}

export function formatTokens(value: number): string {
  return value.toLocaleString("en-US");
}
