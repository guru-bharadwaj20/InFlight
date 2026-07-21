export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

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
  anthropic_key_configured: boolean;
}

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

  listConversations: () => request<Conversation[]>("/conversations"),

  createConversation: (title: string | null) =>
    request<Conversation>("/conversations", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),

  getConversation: (id: string) =>
    request<ConversationDetail>(`/conversations/${id}`),

  createMessage: (
    conversationId: string,
    body: { content: string; role?: "user" | "assistant" }
  ) =>
    request<Message>(`/conversations/${conversationId}/messages`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** What a job stamped at `at` would be allowed to read as context. */
  getContextSnapshot: (conversationId: string, at?: string) =>
    request<Message[]>(
      `/conversations/${conversationId}/context${at ? `?at=${encodeURIComponent(at)}` : ""}`
    ),
};
