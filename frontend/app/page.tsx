"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { Composer, type SubmitOptions } from "@/components/Composer";
import { Logo } from "@/components/Logo";

const SUGGESTIONS = [
  {
    label: "Ask two things at once",
    hint: "watch both answer in parallel",
    prompt: "Explain how TCP congestion control works, in detail.",
  },
  {
    label: "Ask a follow-up immediately",
    hint: "it waits for the answer it needs",
    prompt: "List three sorting algorithms with one line each.",
  },
  {
    label: "Just start a conversation",
    hint: "",
    prompt: "What can you help me with?",
  },
];

export default function HomePage() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  // `starting` state is what the UI reads to disable itself, but two clicks
  // dispatched before React commits that state update would both read the
  // stale `false` and pass the guard. The ref is set synchronously, so it
  // closes that window.
  const startingRef = useRef(false);

  /** Start a chat the way every other assistant does: type, and it exists. */
  async function start(prompt: string, options?: SubmitOptions) {
    if (startingRef.current) return;
    startingRef.current = true;
    setStarting(true);
    try {
      // No title: the server derives one from the first prompt now, so both
      // entry points get the same tidy label instead of this path alone getting
      // a raw mid-word slice that kept any newlines the prompt contained.
      const conversation = await api.createConversation(null);
      await api.sendPrompt(conversation.id, prompt, {
        model: options?.model,
        attachments: options?.attachments,
      });
      router.push(`/conversations/${conversation.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      startingRef.current = false;
      setStarting(false);
    }
  }

  return (
    <div className="relative h-full overflow-y-auto">
      <div className="hero-wash" />

      <div className="relative mx-auto flex min-h-full max-w-3xl flex-col justify-center px-6 py-16">
        <div className="flex flex-col items-center">
          <Logo className="h-12 w-12" id="hero" />
          <h1 className="mt-3 font-serif text-5xl tracking-tight sm:text-6xl">
            In<span className="text-flight">Flight</span>
          </h1>
        </div>
        <p className="mt-3 text-center text-[15px] text-ink-soft">
          Ask as many things as you like. You never have to wait for one answer
          before starting the next.
        </p>

        <div className="mt-8">
          <Composer
            onSubmit={start}
            disabled={starting}
            placeholder={starting ? "Starting…" : "Ask anything — or two things at once"}
            autoFocus
          />
        </div>

        {error && (
          <p className="mt-4 rounded-lg border border-failed/30 bg-failed/5 px-3 py-2 text-sm text-failed">
            {error}
          </p>
        )}

        <ul className="mx-auto mt-8 w-full max-w-xl space-y-1">
          {SUGGESTIONS.map((suggestion) => (
            <li key={suggestion.label}>
              <button
                onClick={() => start(suggestion.prompt)}
                disabled={starting}
                className="flex w-full items-baseline gap-2 rounded-xl px-3 py-2.5 text-left text-[15px] text-ink-soft transition-colors hover:bg-surface-2 disabled:opacity-50"
              >
                <span className="text-ink">{suggestion.label}</span>
                {suggestion.hint && (
                  <span className="text-xs text-ink-faint">— {suggestion.hint}</span>
                )}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
