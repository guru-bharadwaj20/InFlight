"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  conversationSocketUrl,
  type Frame,
  type Message,
} from "./api";

/**
 * Owns the conversation's messages and the single socket feeding them.
 *
 * Every incoming frame is applied to one message, looked up by job id. Nothing
 * here tracks "the current response" — that concept stops existing in Stage 3,
 * and building it in now would only have to be torn out again.
 */
export function useConversation(conversationId: string) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [title, setTitle] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Highest chunk sequence applied per job. A reconnect replays the buffered
  // text wholesale, so any chunk frame at or below the replayed sequence is
  // text we already have.
  const lastSeq = useRef<Map<string, number>>(new Map());

  const patch = useCallback((id: string, changes: Partial<Message>) => {
    setMessages((current) =>
      current.map((message) =>
        message.id === id ? { ...message, ...changes } : message
      )
    );
  }, []);

  const applyFrame = useCallback(
    (frame: Frame) => {
      const seen = lastSeq.current.get(frame.job_id) ?? 0;

      switch (frame.type) {
        case "status":
          patch(frame.job_id, { status: frame.status });
          break;

        case "chunk":
          if (frame.seq <= seen) return;
          lastSeq.current.set(frame.job_id, frame.seq);
          setMessages((current) =>
            current.map((message) =>
              message.id === frame.job_id
                ? {
                    ...message,
                    content: (message.content ?? "") + frame.text,
                    status: "streaming",
                  }
                : message
            )
          );
          break;

        case "resume":
          lastSeq.current.set(frame.job_id, frame.seq);
          patch(frame.job_id, { content: frame.text, status: frame.status });
          break;

        case "done":
        case "error":
          patch(frame.job_id, {
            status: frame.status,
            content: frame.content,
            error: frame.error,
            completed_at: frame.completed_at,
            prompt_tokens: frame.prompt_tokens,
            completion_tokens: frame.completion_tokens,
            model: frame.model,
          });
          break;
      }
    },
    [patch]
  );

  const refresh = useCallback(async () => {
    try {
      const conversation = await api.getConversation(conversationId);
      setTitle(conversation.title);
      setMessages(conversation.messages);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [conversationId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let closed = false;

    const connect = () => {
      if (closed) return;
      socket = new WebSocket(conversationSocketUrl(conversationId));

      socket.onopen = () => {
        attempt = 0;
        setConnected(true);
      };

      socket.onmessage = (event) => {
        applyFrame(JSON.parse(event.data) as Frame);
      };

      socket.onclose = () => {
        setConnected(false);
        if (closed) return;
        // Back off, but stay responsive: a dropped socket mid-generation means
        // the user is watching a bubble that has stopped moving.
        const delay = Math.min(1000 * 2 ** attempt++, 10000);
        retry = setTimeout(connect, delay);
      };

      socket.onerror = () => socket?.close();
    };

    connect();

    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      socket?.close();
    };
  }, [conversationId, applyFrame]);

  const send = useCallback(
    async (content: string) => {
      try {
        const accepted = await api.sendPrompt(conversationId, content);
        setMessages((current) => [
          ...current,
          accepted.user_message,
          accepted.assistant_message,
        ]);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [conversationId]
  );

  // A count, not a boolean. Nothing about sending depends on it any more — it
  // is reported so the UI can say how many answers are in flight, not to gate
  // anything.
  const inFlight = messages.filter(
    (message) => message.status === "pending" || message.status === "streaming"
  ).length;

  return { messages, title, connected, loading, error, inFlight, send, refresh };
}
