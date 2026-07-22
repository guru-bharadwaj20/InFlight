"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type Health } from "@/lib/api";
import { Composer } from "@/components/Composer";

function greeting() {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

/**
 * The greeting can only be decided in the browser.
 *
 * Reading the clock during render makes the server and the client disagree —
 * the container runs UTC while the visitor is in their own zone, so React
 * hydrates "Good afternoon" over "Good evening" and throws. Resolving it after
 * mount is the fix; the placeholder exists purely to reserve the line so the
 * hero does not jump, and is invisible until the real value arrives.
 */
function useGreeting() {
  const [value, setValue] = useState<string | null>(null);
  useEffect(() => setValue(greeting()), []);
  return value;
}

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
  const greet = useGreeting();
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  /** Start a chat the way every other assistant does: type, and it exists. */
  async function start(prompt: string) {
    if (starting) return;
    setStarting(true);
    try {
      const conversation = await api.createConversation(prompt.slice(0, 48));
      await api.sendPrompt(conversation.id, prompt);
      router.push(`/conversations/${conversation.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setStarting(false);
    }
  }

  return (
    <div className="relative h-full overflow-y-auto">
      <div className="hero-wash" />

      <div className="relative mx-auto flex min-h-full max-w-3xl flex-col justify-center px-6 py-16">
        <h1 className="text-center font-serif text-4xl tracking-tight sm:text-5xl">
          <span className="text-flight">✳</span>{" "}
          <span
            className={`transition-opacity duration-300 ${
              greet ? "opacity-100" : "opacity-0"
            }`}
          >
            {greet ?? "Good evening"}
          </span>
        </h1>
        <p className="mt-3 text-center text-[15px] text-ink-soft">
          Ask as many things as you like. You never have to wait for one answer
          before starting the next.
        </p>

        <div className="mt-8">
          <Composer
            onSubmit={start}
            disabled={starting}
            placeholder={starting ? "Starting…" : "Ask anything — or two things at once"}
            model={health?.generation_model}
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
                className="flex w-full items-baseline gap-2 rounded-xl px-3 py-2.5 text-left text-[15px] text-ink-soft transition-colors hover:bg-white/70 disabled:opacity-50"
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
