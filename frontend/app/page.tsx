"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, type Conversation, type Health } from "@/lib/api";

function StatusDot({ value }: { value: string }) {
  const ok = value === "ok";
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full ${ok ? "bg-complete" : "bg-failed"}`}
      aria-hidden
    />
  );
}

export default function HomePage() {
  const [health, setHealth] = useState<Health | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [nextHealth, nextConversations] = await Promise.all([
        api.health(),
        api.listConversations(),
      ]);
      setHealth(nextHealth);
      setConversations(nextConversations);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function handleCreate(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await api.createConversation(title.trim() || null);
      setTitle("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-10">
      <section>
        <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-zinc-400">
          Services
        </h2>
        {health ? (
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 text-sm sm:grid-cols-3">
            <div className="flex items-center gap-2">
              <StatusDot value={health.postgres} />
              <dt className="text-zinc-400">Postgres</dt>
              <dd className="text-zinc-200">{health.postgres}</dd>
            </div>
            <div className="flex items-center gap-2">
              <StatusDot value={health.redis} />
              <dt className="text-zinc-400">Redis</dt>
              <dd className="text-zinc-200">{health.redis}</dd>
            </div>
            <div className="flex items-center gap-2">
              <StatusDot value={health.anthropic_key_configured ? "ok" : "unset"} />
              <dt className="text-zinc-400">API key</dt>
              <dd className="text-zinc-200">
                {health.anthropic_key_configured ? "configured" : "not set"}
              </dd>
            </div>
            <div className="col-span-2 text-zinc-400 sm:col-span-3">
              Generation:{" "}
              <span className="text-zinc-200">{health.generation_model}</span>
              {"  ·  "}
              Classifier:{" "}
              <span className="text-zinc-200">{health.classifier_model}</span>
            </div>
          </dl>
        ) : (
          <p className="text-sm text-zinc-500">Contacting backend…</p>
        )}
        {error && (
          <p className="mt-3 rounded border border-failed/40 bg-failed/10 p-3 text-sm text-failed">
            {error}
          </p>
        )}
      </section>

      <section>
        <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-zinc-400">
          Conversations
        </h2>

        <form onSubmit={handleCreate} className="mb-4 flex gap-2">
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="New conversation title (optional)"
            className="flex-1 rounded border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm outline-none placeholder:text-zinc-600 focus:border-zinc-600"
          />
          <button
            type="submit"
            disabled={busy}
            className="rounded bg-zinc-100 px-4 py-2 text-sm font-medium text-zinc-900 disabled:opacity-50"
          >
            Create
          </button>
        </form>

        {conversations.length === 0 ? (
          <p className="text-sm text-zinc-500">
            No conversations yet. Create one to write and read message rows.
          </p>
        ) : (
          <ul className="divide-y divide-zinc-800 rounded-lg border border-zinc-800">
            {conversations.map((conversation) => (
              <li key={conversation.id}>
                <Link
                  href={`/conversations/${conversation.id}`}
                  className="flex items-baseline justify-between px-4 py-3 hover:bg-zinc-900"
                >
                  <span className="text-sm">
                    {conversation.title ?? "Untitled conversation"}
                  </span>
                  <span className="font-mono text-xs text-zinc-500">
                    {conversation.id}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
