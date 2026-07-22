"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api, type Message } from "@/lib/api";
import { useConversation } from "@/lib/useConversation";

function formatTime(value: string | null) {
  if (!value) return "—";
  return new Date(value).toISOString().replace("T", " ").slice(11, 23);
}

const STATUS_COLOR: Record<Message["status"], string> = {
  pending: "text-pending",
  streaming: "text-streaming",
  complete: "text-complete",
  error: "text-failed",
  cancelled: "text-failed",
};

function Bubble({ message }: { message: Message }) {
  const isUser = message.role === "user";
  const streaming = message.status === "streaming" || message.status === "pending";

  return (
    <li className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[80ch] rounded-2xl px-4 py-3 ${
          isUser
            ? "bg-zinc-100 text-zinc-900"
            : "border border-zinc-800 bg-zinc-900/60 text-zinc-100"
        }`}
      >
        {message.status === "error" ? (
          <p className="text-sm text-failed">{message.error ?? "generation failed"}</p>
        ) : (
          <p className="whitespace-pre-wrap text-sm leading-relaxed">
            {message.content}
            {streaming && (
              <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-streaming align-text-bottom" />
            )}
          </p>
        )}

        {!isUser && message.status !== "streaming" && (
          <div
            className={`mt-2 flex flex-wrap gap-x-3 font-mono text-[10px] ${
              STATUS_COLOR[message.status]
            }`}
          >
            <span>{message.status}</span>
            {message.completion_tokens !== null && (
              <span className="text-zinc-600">
                {message.prompt_tokens} in / {message.completion_tokens} out
              </span>
            )}
            {message.model && <span className="text-zinc-600">{message.model}</span>}
            <span className="text-zinc-600">done {formatTime(message.completed_at)}</span>
          </div>
        )}
      </div>
    </li>
  );
}

function SnapshotInspector({ conversationId }: { conversationId: string }) {
  const [snapshot, setSnapshot] = useState<Message[] | null>(null);
  const [open, setOpen] = useState(false);

  async function read() {
    setSnapshot(await api.getContextSnapshot(conversationId));
    setOpen(true);
  }

  return (
    <section className="rounded-lg border border-zinc-800 p-4">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-400">
            Context snapshot
          </h3>
          <p className="mt-1 text-xs text-zinc-600">
            What a job stamped <em>now</em> would be allowed to read. Only rows
            that <em>completed</em> before the cutoff are in it, so anything
            still streaming is absent.
          </p>
        </div>
        <button
          onClick={read}
          className="shrink-0 rounded border border-zinc-700 px-3 py-1 text-xs hover:bg-zinc-900"
        >
          Read
        </button>
      </div>

      {open && snapshot && (
        <ul className="mt-3 space-y-1 font-mono text-[11px] text-zinc-500">
          {snapshot.length === 0 && <li>snapshot is empty</li>}
          {snapshot.map((message) => (
            <li key={message.id}>
              {formatTime(message.completed_at)} · {message.role} ·{" "}
              {(message.content ?? "").slice(0, 60)}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function ConversationPage({ params }: { params: { id: string } }) {
  const { messages, title, connected, loading, error, inFlight, send } =
    useConversation(params.id);
  const [draft, setDraft] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const content = draft.trim();
    if (!content) return;
    // Cleared before the request resolves, so the box is ready for the next
    // prompt immediately rather than after a round trip.
    setDraft("");
    await send(content);
  }

  return (
    <div className="flex h-[calc(100vh-8rem)] flex-col gap-4">
      <header className="flex items-baseline justify-between gap-4">
        <div>
          <Link href="/" className="text-sm text-zinc-400 hover:text-zinc-200">
            ← All conversations
          </Link>
          <h2 className="mt-1 text-lg font-semibold">
            {title ?? "Untitled conversation"}
          </h2>
        </div>
        <span
          className={`font-mono text-[11px] ${
            connected ? "text-complete" : "text-failed"
          }`}
        >
          {connected ? "socket connected" : "socket offline"}
        </span>
      </header>

      {error && (
        <p className="rounded border border-failed/40 bg-failed/10 p-3 text-sm text-failed">
          {error}
        </p>
      )}

      <div className="flex-1 overflow-y-auto rounded-lg border border-zinc-800 p-4">
        {loading ? (
          <p className="text-sm text-zinc-500">Loading…</p>
        ) : messages.length === 0 ? (
          <p className="text-sm text-zinc-500">No messages yet. Say something.</p>
        ) : (
          <ul className="space-y-3">
            {messages.map((message) => (
              <Bubble key={message.id} message={message} />
            ))}
          </ul>
        )}
        <div ref={bottom} />
      </div>

      <form onSubmit={submit} className="flex gap-2">
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) void submit(event);
          }}
          rows={1}
          placeholder="Send a message — you don't have to wait"
          className="flex-1 resize-none rounded border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm outline-none placeholder:text-zinc-600 focus:border-zinc-600"
        />
        <button
          type="submit"
          disabled={!draft.trim()}
          className="rounded bg-zinc-100 px-4 py-2 text-sm font-medium text-zinc-900 disabled:opacity-40"
        >
          Send
        </button>
      </form>

      <p className="text-xs text-zinc-600">
        {inFlight > 0
          ? `${inFlight} ${inFlight === 1 ? "answer is" : "answers are"} generating. Send another — the input never locks.`
          : "The input never locks. Send a follow-up while an answer is still generating and both run at once."}
      </p>

      <SnapshotInspector conversationId={params.id} />
    </div>
  );
}
