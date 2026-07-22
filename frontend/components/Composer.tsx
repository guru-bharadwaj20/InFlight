"use client";

import { useEffect, useRef, useState } from "react";

/**
 * The one place a prompt is written, on the hero and in a conversation alike.
 *
 * It is never disabled by work in progress. `disabled` covers only the moments
 * where there is genuinely nothing to type into yet — creating the conversation
 * itself — and never the case of an answer still streaming, which is the whole
 * point of the product.
 */
export function Composer({
  onSubmit,
  placeholder = "Reply, or ask something new",
  disabled = false,
  model,
  autoFocus = false,
  footer,
  banner,
}: {
  onSubmit: (prompt: string) => void | Promise<void>;
  placeholder?: string;
  disabled?: boolean;
  model?: string;
  autoFocus?: boolean;
  /** Status strip along the bottom edge — in-flight count, tokens, cost. */
  footer?: React.ReactNode;
  /** Sits above the field, e.g. the chained-reply notice. */
  banner?: React.ReactNode;
}) {
  const [draft, setDraft] = useState("");
  const field = useRef<HTMLTextAreaElement>(null);

  // Grow with the text, up to a point, then scroll — a long prompt should not
  // push the conversation off screen.
  useEffect(() => {
    const el = field.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [draft]);

  function submit() {
    const prompt = draft.trim();
    if (!prompt || disabled) return;
    setDraft("");
    void onSubmit(prompt);
  }

  return (
    <div className="rounded-2xl border border-edge bg-surface shadow-composer">
      {banner}

      <div className="flex items-end gap-2 px-3 pt-3">
        <button
          type="button"
          title="Attachments are not part of this project"
          className="mb-1.5 shrink-0 rounded-lg p-1.5 text-ink-faint transition-colors hover:bg-paper"
        >
          <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.7">
            <path d="M10 4v12M4 10h12" strokeLinecap="round" />
          </svg>
        </button>

        <textarea
          ref={field}
          value={draft}
          autoFocus={autoFocus}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
          rows={1}
          placeholder={placeholder}
          className="max-h-[200px] flex-1 resize-none bg-transparent py-2 text-[15px] leading-relaxed outline-none placeholder:text-ink-faint"
        />

        <div className="mb-1 flex shrink-0 items-center gap-1.5">
          {model && (
            <span className="hidden items-center gap-1.5 rounded-full border border-edge px-2.5 py-1 text-xs text-ink-soft sm:inline-flex">
              <span className="h-1.5 w-1.5 rounded-full bg-flight" />
              {model}
            </span>
          )}
          <button
            type="button"
            onClick={submit}
            disabled={!draft.trim() || disabled}
            aria-label="Send"
            className="rounded-full bg-ink p-2 text-paper transition-opacity disabled:opacity-25"
          >
            <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M10 16V4M5 9l5-5 5 5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </div>
      </div>

      {footer ? (
        <div className="border-t border-edge/70 px-4 py-2">{footer}</div>
      ) : (
        <div className="h-3" />
      )}
    </div>
  );
}
