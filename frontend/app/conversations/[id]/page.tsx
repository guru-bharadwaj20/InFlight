"use client";

import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useRef, useState } from "react";
import { api, type Message, type MessageStatus, type Pricing } from "@/lib/api";
import { useConversation } from "@/lib/useConversation";
import { formatTokens, formatUsd, summarize } from "@/lib/usage";
import { Composer } from "@/components/Composer";

const STATUS_TEXT: Record<MessageStatus, string> = {
  pending: "text-pending",
  streaming: "text-streaming",
  complete: "text-complete",
  error: "text-failed",
  cancelled: "text-failed",
};

const STATUS_BG: Record<MessageStatus, string> = {
  pending: "bg-pending",
  streaming: "bg-streaming",
  complete: "bg-complete",
  error: "bg-failed",
  cancelled: "bg-failed",
};

/** Pulses only while unsettled, so "still working" reads without being read. */
function StatusChip({ message }: { message: Message }) {
  const active = message.status === "pending" || message.status === "streaming";
  const held = message.status === "pending" && message.detected_dependency === "dependent";

  return (
    <span className={`inline-flex items-center gap-1.5 text-[11px] ${STATUS_TEXT[message.status]}`}>
      <motion.span
        className={`h-1.5 w-1.5 rounded-full ${STATUS_BG[message.status]}`}
        animate={active ? { opacity: [1, 0.25, 1] } : { opacity: 1 }}
        transition={active ? { duration: 1.4, repeat: Infinity, ease: "easeInOut" } : undefined}
      />
      {held ? "waiting for context" : message.status}
    </span>
  );
}

function Bubble({
  message,
  onChain,
  onRegenerate,
  onCancel,
  chained,
}: {
  message: Message;
  onChain: (message: Message) => void;
  onRegenerate: (id: string) => void;
  onCancel: (id: string) => void;
  chained: boolean;
}) {
  const isUser = message.role === "user";
  const unsettled = message.status === "pending" || message.status === "streaming";
  const failed = message.status === "error";

  return (
    <motion.li
      // `layout` animates the height change as an answer fills in, so a long one
      // landing does not snap the rest of the thread down.
      layout
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ layout: { duration: 0.25, ease: "easeOut" }, duration: 0.2 }}
      className={`group flex ${isUser ? "justify-end" : "justify-start"}`}
    >
      <div className={`relative min-w-0 ${isUser ? "max-w-[75%]" : "max-w-[85%]"}`}>
        <div className="pointer-events-none absolute -top-3 right-0 z-10 flex gap-1 opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100">
          {!isUser && unsettled && (
            <button
              onClick={() => onCancel(message.id)}
              title="Stop this answer, keeping what it has produced"
              className="pointer-events-auto rounded-md border border-edge bg-surface px-1.5 py-0.5 text-[10px] text-ink-soft shadow-lift hover:text-failed"
            >
              stop
            </button>
          )}
          {/* Chaining an in-flight bubble is the point: it locks in the wait
              before the answer exists. */}
          <button
            onClick={() => onChain(message)}
            title="Chain a follow-up — always waits for this message"
            className="pointer-events-auto rounded-md border border-edge bg-surface px-1.5 py-0.5 text-[10px] text-ink-soft shadow-lift hover:text-flight"
          >
            ↩ chain
          </button>
        </div>

        <div
          className={
            isUser
              ? "rounded-2xl bg-flight-soft px-4 py-2.5 text-[15px] leading-relaxed"
              : `rounded-2xl border px-4 py-3 text-[15px] leading-relaxed ${
                  chained
                    ? "border-flight bg-surface"
                    : unsettled
                      ? "border-flight/30 bg-surface"
                      : "border-edge bg-surface"
                }`
          }
        >
          {failed ? (
            <p className="text-sm text-failed">{message.error ?? "generation failed"}</p>
          ) : message.status === "pending" ? (
            // Holds a line from the start, so the first token grows the bubble
            // instead of creating one. A held job says why it is held.
            <p className="text-sm italic text-ink-faint">
              {message.detected_dependency === "dependent"
                ? "waiting for the previous answer…"
                : "thinking…"}
            </p>
          ) : (
            <p className="whitespace-pre-wrap break-words">
              {message.content}
              {message.status === "streaming" && (
                <motion.span
                  className="ml-0.5 inline-block h-[1.05em] w-[3px] translate-y-[2px] rounded-sm bg-flight"
                  animate={{ opacity: [1, 0.15, 1] }}
                  transition={{ duration: 0.9, repeat: Infinity, ease: "easeInOut" }}
                />
              )}
            </p>
          )}
        </div>

        {!isUser && (
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 px-1 text-[11px] text-ink-faint">
            <StatusChip message={message} />
            {message.detected_dependency === "dependent" && message.status !== "pending" && (
              <span title={`${message.dependency_reason ?? ""} (${message.dependency_source ?? ""})`}>
                waited
              </span>
            )}
            {message.completion_tokens !== null && (
              <span>
                {message.prompt_tokens} in / {message.completion_tokens} out
              </span>
            )}
          </div>
        )}

        {/* Non-blocking by design: the answer stands, this only offers. */}
        {message.stale_context_reason && message.status === "complete" && (
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-xl border border-pending/40 bg-pending/5 px-3 py-2">
            <p className="min-w-0 flex-1 text-[12px] text-ink-soft">
              This answer may not have had your earlier prompt&apos;s context.
            </p>
            <button
              onClick={() => onRegenerate(message.id)}
              className="shrink-0 rounded-lg border border-pending/50 px-2 py-0.5 text-[11px] text-pending hover:bg-pending/10"
            >
              regenerate with it
            </button>
          </div>
        )}
      </div>
    </motion.li>
  );
}

/** Transparency, never a gate: this reports spend and does not limit it. */
function UsageStrip({ messages, inFlight }: { messages: Message[]; inFlight: number }) {
  const [pricing, setPricing] = useState<Pricing | null>(null);

  useEffect(() => {
    api.pricing().then(setPricing).catch(() => setPricing(null));
  }, []);

  const usage = summarize(messages, pricing);

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-ink-faint">
      <span className={inFlight > 0 ? "text-flight" : ""}>
        {inFlight > 0 ? `${inFlight} in flight` : "idle"}
      </span>
      <span>{usage.answers} answered</span>
      <span>{formatTokens(usage.totalTokens)} tokens</span>
      <span title={usage.partial ? "excludes models with no published rate" : undefined}>
        {formatUsd(usage.costUsd)}
        {usage.partial && "*"}
      </span>
      <span className="ml-auto hidden sm:inline">the input never locks</span>
    </div>
  );
}

export default function ConversationPage({ params }: { params: { id: string } }) {
  const { messages, title, connected, loading, error, inFlight, send, regenerate, cancel } =
    useConversation(params.id);
  const [replyTo, setReplyTo] = useState<Message | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const [stick, setStick] = useState(true);

  // Only follow the tail if already at it. With several answers growing at once,
  // yanking the viewport down on every chunk would make reading anything above
  // impossible.
  useEffect(() => {
    if (stick) bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, stick]);

  function onScroll() {
    const el = scroller.current;
    if (!el) return;
    setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
  }

  async function submit(prompt: string) {
    const parent = replyTo;
    setReplyTo(null);
    setStick(true);
    await send(prompt, parent?.id);
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between gap-4 border-b border-edge px-6 py-3">
        <h1 className="truncate font-serif text-[17px]">{title ?? "Untitled chat"}</h1>
        <span className={`shrink-0 text-[11px] ${connected ? "text-ink-faint" : "text-failed"}`}>
          {connected ? "connected" : "reconnecting…"}
        </span>
      </header>

      <div ref={scroller} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto max-w-3xl">
          {loading ? (
            <p className="text-sm text-ink-faint">Loading…</p>
          ) : (
            <ul className="space-y-5">
              <AnimatePresence initial={false}>
                {messages.map((message) => (
                  <Bubble
                    key={message.id}
                    message={message}
                    onChain={setReplyTo}
                    onRegenerate={regenerate}
                    onCancel={cancel}
                    chained={replyTo?.id === message.id}
                  />
                ))}
              </AnimatePresence>
            </ul>
          )}
          <div ref={bottom} className="h-2" />
        </div>
      </div>

      <div className="px-6 pb-5">
        <div className="mx-auto max-w-3xl">
          {error && (
            <p className="mb-2 rounded-lg border border-failed/30 bg-failed/5 px-3 py-2 text-sm text-failed">
              {error}
            </p>
          )}
          <Composer
            onSubmit={submit}
            footer={<UsageStrip messages={messages} inFlight={inFlight} />}
            banner={
              <AnimatePresence>
                {replyTo && (
                  <motion.div
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: "auto" }}
                    exit={{ opacity: 0, height: 0 }}
                    className="flex items-center justify-between gap-3 overflow-hidden border-b border-edge/70 px-4 py-2"
                  >
                    <p className="min-w-0 truncate text-xs text-ink-soft">
                      <span className="text-flight">chained</span> — waits for{" "}
                      {(replyTo.content ?? "this answer").slice(0, 60)}
                    </p>
                    <button
                      onClick={() => setReplyTo(null)}
                      className="shrink-0 text-[11px] text-ink-faint hover:text-ink"
                    >
                      cancel
                    </button>
                  </motion.div>
                )}
              </AnimatePresence>
            }
          />
        </div>
      </div>
    </div>
  );
}
