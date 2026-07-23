export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export const WS_BASE_URL =
  process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000/ws";

const TOKEN_KEY = "inflight-token";

export const auth = {
  get token(): string | null {
    if (typeof window === "undefined") return null;
    return window.localStorage.getItem(TOKEN_KEY);
  },
  set(token: string) {
    window.localStorage.setItem(TOKEN_KEY, token);
  },
  clear() {
    window.localStorage.removeItem(TOKEN_KEY);
  },
};

// A browser cannot attach an Authorization header to a WebSocket, so the token
// rides along as a query parameter; the server validates it before accepting.
export const conversationSocketUrl = (conversationId: string) => {
  const token = auth.token ?? "";
  return `${WS_BASE_URL}/conversations/${conversationId}?token=${encodeURIComponent(token)}`;
};

export interface User {
  id: string;
  name: string;
  email: string;
}

export interface AuthResult {
  token: string;
  user: User;
}

/** Thrown on a 401 so callers (and the shell) can react to a dead session. */
export class Unauthorized extends Error {
  constructor() {
    super("unauthorized");
  }
}

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
  /** Set when this answer is suspected of having missed context it needed. */
  stale_context_reason: string | null;
  stale_context_source_id: string | null;
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
  starred: boolean;
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

export interface ModelInfo {
  id: string;
  label: string;
}

export interface Models {
  default: string;
  models: ModelInfo[];
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
  /** The retrospective check suspects this answer missed context it needed. */
  | {
      job_id: string;
      type: "stale_context";
      stale_context_reason: string;
      stale_context_source_id: string;
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
  const token = auth.token;
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  });

  if (response.status === 401) {
    // The token is gone or expired. Drop it so the shell routes to /login
    // rather than looping on failed authed calls.
    auth.clear();
    throw new Unauthorized();
  }

  if (!response.ok) {
    const body = await response.text();
    throw new Error(await friendlyError(response.status, body));
  }

  // 204 No Content (delete) has no body to parse.
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

/** Pull FastAPI's `detail` out of the error envelope; fall back to the raw body. */
async function friendlyError(statusCode: number, body: string): Promise<string> {
  try {
    const parsed = JSON.parse(body);
    if (typeof parsed.detail === "string") return parsed.detail;
    if (Array.isArray(parsed.detail) && parsed.detail[0]?.msg)
      return parsed.detail[0].msg;
  } catch {
    /* not JSON */
  }
  return body || `request failed (${statusCode})`;
}

export const api = {
  health: () => request<Health>("/health"),

  signup: (name: string, email: string, password: string) =>
    request<AuthResult>("/auth/signup", {
      method: "POST",
      body: JSON.stringify({ name, email, password }),
    }),

  login: (email: string, password: string) =>
    request<AuthResult>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  me: () => request<User>("/auth/me"),

  /** Rates only — the client already holds the token counts to apply them to. */
  pricing: () => request<Pricing>("/pricing"),

  /** Free Gemini chat models this key can use, for the composer's picker. */
  models: () => request<Models>("/models"),

  listConversations: () => request<Conversation[]>("/conversations"),

  createConversation: (title: string | null) =>
    request<Conversation>("/conversations", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),

  getConversation: (id: string) =>
    request<ConversationDetail>(`/conversations/${id}`),

  /** Rename or (un)star a chat. */
  updateConversation: (
    id: string,
    changes: { title?: string; starred?: boolean }
  ) =>
    request<Conversation>(`/conversations/${id}`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    }),

  deleteConversation: (id: string) =>
    request<void>(`/conversations/${id}`, { method: "DELETE" }),

  /**
   * Returns as soon as the rows exist — it does not wait for the model.
   * `parentMessageId` chains the prompt: a deterministic override that makes
   * the job wait, whatever dependency detection would have concluded.
   */
  sendPrompt: (
    conversationId: string,
    content: string,
    options?: {
      parentMessageId?: string | null;
      model?: string | null;
      attachments?: { mime_type: string; data: string }[];
    }
  ) =>
    request<PromptAccepted>(`/conversations/${conversationId}/messages`, {
      method: "POST",
      body: JSON.stringify({
        content,
        parent_message_id: options?.parentMessageId ?? null,
        model: options?.model ?? null,
        attachments: (options?.attachments ?? []).map((a) => ({
          mime_type: a.mime_type,
          data: a.data,
        })),
      }),
    }),

  /** Stop one in-flight answer, keeping whatever it already produced. */
  cancel: (conversationId: string, messageId: string) =>
    request<Message>(
      `/conversations/${conversationId}/messages/${messageId}/cancel`,
      { method: "POST" }
    ),

  /** Re-run one answer against a fresh snapshot. User-initiated only. */
  regenerate: (conversationId: string, messageId: string) =>
    request<Message>(
      `/conversations/${conversationId}/messages/${messageId}/regenerate`,
      { method: "POST" }
    ),

  /** What a job stamped at `at` would be allowed to read as context. */
  getContextSnapshot: (conversationId: string, at?: string) =>
    request<Message[]>(
      `/conversations/${conversationId}/context${at ? `?at=${encodeURIComponent(at)}` : ""}`
    ),
};
