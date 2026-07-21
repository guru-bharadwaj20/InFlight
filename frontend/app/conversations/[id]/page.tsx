"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, type ConversationDetail, type Message } from "@/lib/api";

function formatTime(value: string | null) {
  if (!value) return "—";
  return new Date(value).toISOString().replace("T", " ").slice(0, 23);
}

function MessageRow({ message }: { message: Message }) {
  return (
    <li className="px-4 py-3">
      <div className="flex items-baseline justify-between gap-4">
        <span className="text-xs uppercase tracking-wide text-zinc-500">
          {message.role}
        </span>
        <span className="font-mono text-[11px] text-zinc-600">
          {message.status}
        </span>
      </div>
      <p className="mt-1 whitespace-pre-wrap text-sm text-zinc-200">
        {message.content ?? <span className="text-zinc-600">(no content yet)</span>}
      </p>
      <dl className="mt-2 grid grid-cols-1 gap-x-6 font-mono text-[11px] text-zinc-500 sm:grid-cols-3">
        <div>submitted {formatTime(message.submitted_at)}</div>
        <div>completed {formatTime(message.completed_at)}</div>
        <div>cutoff {formatTime(message.context_cutoff)}</div>
      </dl>
    </li>
  );
}

export default function ConversationPage({
  params,
}: {
  params: { id: string };
}) {
  const [conversation, setConversation] = useState<ConversationDetail | null>(null);
  const [snapshot, setSnapshot] = useState<Message[] | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setConversation(await api.getConversation(params.id));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [params.id]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!draft.trim()) return;
    setBusy(true);
    try {
      await api.createMessage(params.id, { content: draft.trim() });
      setDraft("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleSnapshot() {
    try {
      setSnapshot(await api.getContextSnapshot(params.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <Link href="/" className="text-sm text-zinc-400 hover:text-zinc-200">
          ← All conversations
        </Link>
        <h2 className="mt-2 text-lg font-semibold">
          {conversation?.title ?? "Untitled conversation"}
        </h2>
        <p className="font-mono text-xs text-zinc-500">{params.id}</p>
      </div>

      {error && (
        <p className="rounded border border-failed/40 bg-failed/10 p-3 text-sm text-failed">
          {error}
        </p>
      )}

      <section>
        <h3 className="mb-3 text-sm font-medium uppercase tracking-wide text-zinc-400">
          Messages, in submitted order
        </h3>
        {conversation && conversation.messages.length > 0 ? (
          <ul className="divide-y divide-zinc-800 rounded-lg border border-zinc-800">
            {conversation.messages.map((message) => (
              <MessageRow key={message.id} message={message} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-zinc-500">No messages yet.</p>
        )}

        <form onSubmit={handleSubmit} className="mt-4 flex gap-2">
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Write a message row"
            className="flex-1 rounded border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm outline-none placeholder:text-zinc-600 focus:border-zinc-600"
          />
          <button
            type="submit"
            disabled={busy}
            className="rounded bg-zinc-100 px-4 py-2 text-sm font-medium text-zinc-900 disabled:opacity-50"
          >
            Insert
          </button>
        </form>
        <p className="mt-2 text-xs text-zinc-600">
          Stage 1 writes the row and stops — no model call yet. Generation
          arrives in Stage 2.
        </p>
      </section>

      <section>
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-medium uppercase tracking-wide text-zinc-400">
            Context snapshot as of now
          </h3>
          <button
            onClick={handleSnapshot}
            className="rounded border border-zinc-700 px-3 py-1 text-xs hover:bg-zinc-900"
          >
            Read snapshot
          </button>
        </div>
        <p className="mb-3 text-xs text-zinc-600">
          Only messages that <em>completed</em> before the cutoff are visible to
          a job stamped at that instant — which is why this list can be shorter
          than the one above.
        </p>
        {snapshot === null ? (
          <p className="text-sm text-zinc-500">Not read yet.</p>
        ) : snapshot.length === 0 ? (
          <p className="text-sm text-zinc-500">Snapshot is empty.</p>
        ) : (
          <ul className="divide-y divide-zinc-800 rounded-lg border border-zinc-800">
            {snapshot.map((message) => (
              <MessageRow key={message.id} message={message} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
