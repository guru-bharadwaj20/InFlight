"use client";

import Link from "next/link";
import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useRef, useState } from "react";
import { api, type Message, type MessageStatus } from "@/lib/api";
import { useConversation } from "@/lib/useConversation";

function formatTime(value: string | null) {
  if (!value) return "—";
  return new Date(value).toISOString().replace("T", " ").slice(11, 23);
}

const STATUS_COLOR: Record<MessageStatus, string> = {
  pending: "text-pending",
  streaming: "text-streaming",
  complete: "text-complete",
  error: "text-failed",
  cancelled: "text-failed",
};

const STATUS_DOT: Record<MessageStatus, string> = {
  pending: "bg-pending",
  streaming: "bg-streaming",
  complete: "bg-complete",
  error: "bg-failed",
  cancelled: "bg-failed",
};

/** Pulses only while the job is unsettled, so "still working" reads at a glance. */
function StatusChip({ status }: { status: MessageStatus }) {
  const active = status === "pending" || status === "streaming";

  return (
    <span
      className={`inline-flex items-center gap-1.5 font-mono text-[10px] ${STATUS_COLOR[status]}`}
    >
      <motion.span
        className={`h-1.5 w-1.5 rounded-full ${STATUS_DOT[status]}`}
        animate={active ? { opacity: [1, 0.25, 1] } : { opacity: 1 }}
        transition={
          active ? { duration: 1.4, repeat: Infinity, ease: "easeInOut" } : undefined
        }
      />
      {status}
    </span>
  );
}

function Bubble({ message }: { message: Message }) {
  const isUser = message.role === "user";
  const unsettled = message.status === "pending" || message.status === "streaming";
  const failed = message.status === "error" || message.status === "cancelled";

  return (
    <motion.li
      // `layout` animates the height change when a bubble fills in, so a long
      // answer landing does not snap the rest of the list down by 200px.
      layout
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ layout: { duration: 0.25, ease: "easeOut" }, duration: 0.2 }}
      className={`flex ${isUser ? "justify-end" : "justify-start"}`}
    >
      <div
        className={`min-w-0 max-w-[80ch] rounded-2xl px-4 py-3 ${
          isUser
            ? "bg-zinc-100 text-zinc-900"
            : `border bg-zinc-900/60 text-zinc-100 ${
                unsettled ? "border-streaming/40" : "border-zinc-800"
              }`
        }`}
      >
        {failed ? (
          <p className="text-sm text-failed">
            {message.error ?? "generation failed"}
          </p>
        ) : message.status === "pending" ? (
          // Occupies a line from the start, so first token arriving grows the
          // bubble instead of creating one.
          <p className="text-sm italic text-zinc-500">waiting to start…</p>
        ) : (
          <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">
            {message.content}
            {message.status === "streaming" && (
              <motion.span
                className="ml-0.5 inline-block h-4 w-1.5 bg-streaming align-text-bottom"
                animate={{ opacity: [1, 0.15, 1] }}
                transition={{ duration: 0.9, repeat: Infinity, ease: "easeInOut" }}
              />
            )}
          </p>
        )}

        {!isUser && (
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
            <StatusChip status={message.status} />
            {message.completion_tokens !== null && (
              <span className="font-mono text-[10px] text-zinc-600">
                {message.prompt_tokens} in / {message.completion_tokens} out
              </span>
            )}
            {message.completed_at && (
              <span className="font-mono text-[10px] text-zinc-600">
                done {formatTime(message.completed_at)}
              </span>
            )}
          </div>
        )}
      </div>
    </motion.li>
  );
}

function SnapshotInspector({ conversationId }: { conversationId: string }) {
  const [snapshot, setSnapshot] = useState<Message[] | null>(null);

  async function read() {
    setSnapshot(await api.getContextSnapshot(conversationId));
  }

  return (
    <section className="rounded-lg border border-zinc-800 p-3">
      <div className="flex items-center justify-between gap-4">
        <p className="text-xs text-zinc-600">
          <span className="font-medium uppercase tracking-wide text-zinc-400">
            Context snapshot
          </span>{" "}
          — what a job stamped <em>now</em> could read. Anything still streaming
          is absent.
        </p>
        <button
          onClick={read}
          className="shrink-0 rounded border border-zinc-700 px-3 py-1 text-xs hover:bg-zinc-900"
        >
          Read
        </button>
      </div>

      {snapshot && (
        <ul className="mt-3 space-y-1 font-mono text-[11px] text-zinc-500">
          {snapshot.length === 0 && <li>snapshot is empty</li>}
          {snapshot.map((message) => (
            <li key={message.id} className="truncate">
              {formatTime(message.completed_at)} · {message.role} ·{" "}
              {(message.content ?? "").slice(0, 70)}
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
  const scroller = useRef<HTMLDivElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const [stick, setStick] = useState(true);

  // Only follow the tail if the user is already at it. With several answers
  // growing at once, yanking the viewport down on every chunk would make
  // reading anything above impossible.
  useEffect(() => {
    if (stick) bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, stick]);

  function onScroll() {
    const el = scroller.current;
    if (!el) return;
    setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const content = draft.trim();
    if (!content) return;
    // Cleared before the request resolves, so the box is ready for the next
    // prompt immediately rather than after a round trip.
    setDraft("");
    setStick(true);
    await send(content);
  }

  return (
    <div className="flex h-[calc(100vh-8rem)] flex-col gap-3">
      <header className="flex items-baseline justify-between gap-4">
        <div>
          <Link href="/" className="text-sm text-zinc-400 hover:text-zinc-200">
            ← All conversations
          </Link>
          <h2 className="mt-1 text-lg font-semibold">
            {title ?? "Untitled conversation"}
          </h2>
        </div>
        <div className="flex items-center gap-3 font-mono text-[11px]">
          <AnimatePresence>
            {inFlight > 0 && (
              <motion.span
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="text-streaming"
              >
                {inFlight} in flight
              </motion.span>
            )}
          </AnimatePresence>
          <span className={connected ? "text-complete" : "text-failed"}>
            {connected ? "socket connected" : "socket offline"}
          </span>
        </div>
      </header>

      {error && (
        <p className="rounded border border-failed/40 bg-failed/10 p-3 text-sm text-failed">
          {error}
        </p>
      )}

      <div
        ref={scroller}
        onScroll={onScroll}
        className="flex-1 overflow-y-auto rounded-lg border border-zinc-800 p-4"
      >
        {loading ? (
          <p className="text-sm text-zinc-500">Loading…</p>
        ) : messages.length === 0 ? (
          <p className="text-sm text-zinc-500">No messages yet. Say something.</p>
        ) : (
          <ul className="space-y-3">
            <AnimatePresence initial={false}>
              {messages.map((message) => (
                <Bubble key={message.id} message={message} />
              ))}
            </AnimatePresence>
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
        Bubbles sit in the order you asked, but each resolves on its own — a
        later question can finish first without moving anything.
      </p>

      <SnapshotInspector conversationId={params.id} />
    </div>
  );
}
