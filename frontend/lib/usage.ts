import type { Message, Pricing } from "./api";

const TOKENS_PER_UNIT = 1_000_000;

export interface UsageSummary {
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  answers: number;
  /** Null if nothing priceable has completed yet. */
  costUsd: number | null;
  /** True when at least one completed answer used a model with no known rate. */
  partial: boolean;
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
  let promptTokens = 0;
  let completionTokens = 0;
  let answers = 0;
  let cost: number | null = null;
  let partial = false;

  for (const message of messages) {
    if (message.status === "pending" || message.status === "streaming") continue;
    // Usage is only meaningful once a job has reported it.
    if (message.prompt_tokens === null && message.completion_tokens === null) continue;

    const p = message.prompt_tokens ?? 0;
    const c = message.completion_tokens ?? 0;
    promptTokens += p;
    completionTokens += c;
    answers++;

    const rate = pricing?.models[message.model ?? "unknown"] ?? null;
    if (rate) {
      cost = (cost ?? 0) + (p * rate.input + c * rate.output) / TOKENS_PER_UNIT;
    } else {
      partial = true;
    }
  }

  return {
    promptTokens,
    completionTokens,
    totalTokens: promptTokens + completionTokens,
    answers,
    costUsd: cost,
    partial,
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
