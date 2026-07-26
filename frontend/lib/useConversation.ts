"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  conversationSocketUrl,
  type Frame,
  type Message,
} from "./api";
import { orderMessages } from "./ordering";

/**
 * Owns the conversation's messages and the single socket feeding them.
 *
 * Every incoming frame is applied to one message, looked up by job id. Nothing
 * here tracks "the current response" — that concept stops existing in Stage 3,
 * and building it in now would only have to be torn out again.
 */
export function useConversation(conversationId: string) {
  const [raw, setRaw] = useState<Message[]>([]);
  const [title, setTitle] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Highest chunk sequence applied per job. A reconnect replays the buffered
  // text wholesale, so any chunk frame at or below the replayed sequence is
  // text we already have.
  const lastSeq = useRef<Map<string, number>>(new Map());
  // Jobs with a resync in flight, so a burst of gapped frames triggers one fetch
  // rather than a storm of them.
  const resyncing = useRef<Set<string>>(new Set());

  // The app-router reuses this component instance across `/conversations/[id]`
  // navigations, so nothing else clears state on a conversation switch. Without
  // this, the previous conversation's messages stay on screen (and its job ids
  // stay in the replay-cursor maps) until the new `refresh()` resolves, and a
  // frame for the new conversation that lands first can be dropped as
  // unmatched. Reset synchronously, before the refresh/socket effects run.
  useEffect(() => {
    setRaw([]);
    setTitle(null);
    setLoading(true);
    setError(null);
    lastSeq.current.clear();
    resyncing.current.clear();
  }, [conversationId]);

  const patch = useCallback((id: string, changes: Partial<Message>) => {
    setRaw((current) =>
      current.map((message) =>
        message.id === id ? { ...message, ...changes } : message
      )
    );
  }, []);

  // Heal a detected gap: fetch the authoritative text-so-far and reset to it,
  // rather than appending past a hole. This is what makes the at-least-once
  // fan-out deliver exactly-once *text* — a missed chunk is recovered, never
  // silently skipped.
  const resync = useCallback(
    async (jobId: string) => {
      if (resyncing.current.has(jobId)) return;
      resyncing.current.add(jobId);
      try {
        const state = await api.streamState(conversationId, jobId);
        // A committed job's text is final: park the cursor high so any late
        // duplicate chunk is ignored.
        lastSeq.current.set(jobId, state.final ? Number.MAX_SAFE_INTEGER : state.seq);
        patch(jobId, { content: state.text, status: state.status });
      } catch {
        /* a reconnect and its resume frame will heal it if this failed */
      } finally {
        resyncing.current.delete(jobId);
      }
    },
    [conversationId, patch]
  );

  const applyFrame = useCallback(
    (frame: Frame) => {
      const seen = lastSeq.current.get(frame.job_id) ?? 0;

      switch (frame.type) {
        case "status":
          patch(frame.job_id, { status: frame.status });
          break;

        case "chunk":
          if (frame.seq <= seen) return; // duplicate: already have this text
          if (frame.seq > seen + 1) {
            // A forward gap — at least one chunk never arrived. Appending now
            // would splice text across the hole, so recover the buffer instead.
            void resync(frame.job_id);
            return;
          }
          lastSeq.current.set(frame.job_id, frame.seq);
          setRaw((current) =>
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

        case "stale_context":
          patch(frame.job_id, {
            stale_context_reason: frame.stale_context_reason,
            stale_context_source_id: frame.stale_context_source_id,
          });
          break;

        case "dependency":
          patch(frame.job_id, {
            detected_dependency: frame.detected_dependency,
            dependency_source: frame.dependency_source,
            dependency_reason: frame.dependency_reason,
          });
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
    [patch, resync]
  );

  const refresh = useCallback(async () => {
    try {
      const conversation = await api.getConversation(conversationId);
      setTitle(conversation.title);
      setRaw(conversation.messages);
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
    async (
      content: string,
      options?: {
        parentMessageId?: string | null;
        model?: string | null;
        attachments?: { mime_type: string; data: string }[];
      }
    ) => {
      try {
        const accepted = await api.sendPrompt(conversationId, content, options);
        setRaw((current) => [
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

  const regenerate = useCallback(
    async (messageId: string) => {
      try {
        // The row is reset server-side; clear the local replay cursor too, or
        // the first chunks of the new answer would be dropped as already-seen.
        lastSeq.current.delete(messageId);
        const fresh = await api.regenerate(conversationId, messageId);
        patch(messageId, fresh);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [conversationId, patch]
  );

  const cancel = useCallback(
    async (messageId: string) => {
      try {
        await api.cancel(conversationId, messageId);
        // The request succeeded server-side, but confirmation is a socket
        // frame — which may not arrive for a while if the socket happens to
        // be mid-reconnect backoff right now. Reflect the cancellation
        // immediately instead of leaving the "stop" click looking like it did
        // nothing; the eventual done/error frame still lands and reconciles.
        patch(messageId, { status: "cancelled" });
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [conversationId, patch]
  );

  // Derived, not maintained in state: rows arrive from three places (initial
  // fetch, POST response, frames) and concurrent sends resolve in whatever order
  // the network returns them, so ordering at the edge is the only place it
  // cannot be got wrong.
  const messages = useMemo(() => orderMessages(raw), [raw]);

  // A count, not a boolean. Nothing about sending depends on it any more — it
  // is reported so the UI can say how many answers are in flight, not to gate
  // anything.
  const inFlight = messages.filter(
    (message) => message.status === "pending" || message.status === "streaming"
  ).length;

  return {
    messages,
    title,
    connected,
    loading,
    error,
    inFlight,
    send,
    regenerate,
    cancel,
    refresh,
  };
}
