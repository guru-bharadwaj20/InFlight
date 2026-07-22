export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export const WS_BASE_URL =
  process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000/ws";

export const conversationSocketUrl = (conversationId: string) =>
  `${WS_BASE_URL}/conversations/${conversationId}`;

export type MessageStatus =
  | "pending"
  | "streaming"
  | "complete"
  | "error"
  | "cancelled";

export interface Message {
  id: string;
  conversation_id: string;
  role: "user" | "assistant";
  content: string | null;
  status: MessageStatus;
  /** When the user hit send — this is the order the list is rendered in. */
  submitted_at: string;
  /** When generation finished — this is the order rows become visible as context. */
  completed_at: string | null;
  /** The snapshot this job was stamped with at submit time. */
  context_cutoff: string;
  dependency_mode: "auto" | "chained" | "independent";
  parent_message_id: string | null;
  /** On an assistant row, the user message it is answering. */
  prompt_message_id: string | null;
  /** What dependency detection concluded, and how — not what the user asked for. */
  detected_dependency: "dependent" | "independent" | "unsure" | null;
  dependency_source: "heuristic" | "classifier" | "chained" | null;
  dependency_reason: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  model: string | null;
  error: string | null;
}

export interface Conversation {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
}

export interface Health {
  status: string;
  postgres: string;
  redis: string;
  generation_model: string;
  classifier_model: string;
  gemini_key_configured: boolean;
}

export interface ModelRate {
  /** USD per 1M tokens. */
  input: number;
  output: number;
}

export interface Pricing {
  updated: string;
  source: string;
  currency: string;
  unit: string;
  note: string;
  models: Record<string, ModelRate>;
}

/** Both rows a prompt creates. The answer itself arrives over the WebSocket. */
export interface PromptAccepted {
  user_message: Message;
  assistant_message: Message;
}

/**
 * Frames are addressed by `job_id`, never by arrival order — with several jobs
 * sharing one socket in Stage 3, order across jobs means nothing.
 */
export type Frame =
  | { job_id: string; type: "status"; status: MessageStatus }
  | { job_id: string; type: "chunk"; seq: number; text: string }
  /** Sent on (re)connect for a job already streaming: the text so far. */
  | { job_id: string; type: "resume"; status: MessageStatus; text: string; seq: number }
  /** The classifier settled an `unsure` verdict; the job may now be waiting. */
  | {
      job_id: string;
      type: "dependency";
      detected_dependency: "dependent" | "independent" | "unsure";
      dependency_source: "heuristic" | "classifier" | "chained";
      dependency_reason: string;
    }
  | {
      job_id: string;
      type: "done" | "error";
      status: MessageStatus;
      content: string | null;
      error: string | null;
      completed_at: string | null;
      prompt_tokens: number | null;
      completion_tokens: number | null;
      model: string | null;
    };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${body}`);
  }

  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/health"),

  /** Rates only — the client already holds the token counts to apply them to. */
  pricing: () => request<Pricing>("/pricing"),

  listConversations: () => request<Conversation[]>("/conversations"),

  createConversation: (title: string | null) =>
    request<Conversation>("/conversations", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),

  getConversation: (id: string) =>
    request<ConversationDetail>(`/conversations/${id}`),

  /** Returns as soon as the rows exist — it does not wait for the model. */
  sendPrompt: (conversationId: string, content: string) =>
    request<PromptAccepted>(`/conversations/${conversationId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),

  /** What a job stamped at `at` would be allowed to read as context. */
  getContextSnapshot: (conversationId: string, at?: string) =>
    request<Message[]>(
      `/conversations/${conversationId}/context${at ? `?at=${encodeURIComponent(at)}` : ""}`
    ),
};
