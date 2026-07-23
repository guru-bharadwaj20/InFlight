"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, type Conversation, type Health } from "@/lib/api";
import { Logo } from "./Logo";
import { ThemeToggle } from "./theme";

function NavItem({
  href,
  label,
  icon,
  active,
}: {
  href: string;
  label: string;
  icon: React.ReactNode;
  active?: boolean;
}) {
  return (
    <Link
      href={href}
      className={`flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors ${
        active ? "bg-surface text-ink shadow-lift" : "text-ink-soft hover:bg-surface-2"
      }`}
    >
      <span className="text-ink-faint">{icon}</span>
      {label}
    </Link>
  );
}

const IconPlus = (
  <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.7">
    <path d="M10 4v12M4 10h12" strokeLinecap="round" />
  </svg>
);
const IconChats = (
  <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.7">
    <path d="M17 12a2 2 0 0 1-2 2H7l-4 3V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" strokeLinejoin="round" />
  </svg>
);
export function Sidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [health, setHealth] = useState<Health | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [list, h] = await Promise.all([api.listConversations(), api.health()]);
      setConversations(list.slice(0, 12));
      setHealth(h);
    } catch {
      /* the page itself surfaces backend errors; the rail stays quiet */
    }
  }, []);

  // Re-read on navigation so a conversation started from the hero appears here
  // without a reload.
  useEffect(() => {
    void refresh();
  }, [refresh, pathname]);

  async function startNew() {
    const created = await api.createConversation(null);
    router.push(`/conversations/${created.id}`);
  }

  return (
    <aside className="flex h-full w-64 shrink-0 flex-col border-r border-edge bg-bg/80 backdrop-blur">
      <div className="flex items-center gap-2 px-4 py-4">
        <Logo className="h-6 w-6" />
        <span className="font-serif text-lg tracking-tight">
          In<span className="text-flight">Flight</span>
        </span>
        <ThemeToggle className="ml-auto" />
      </div>

      <div className="space-y-0.5 px-2">
        <button
          onClick={startNew}
          className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm text-ink-soft transition-colors hover:bg-surface-2"
        >
          <span className="text-ink-faint">{IconPlus}</span>
          New chat
        </button>
        <NavItem href="/" label="All chats" icon={IconChats} active={pathname === "/"} />
      </div>

      <div className="mt-6 min-h-0 flex-1 overflow-y-auto px-2">
        <p className="px-2.5 pb-1 text-xs font-medium text-ink-faint">Recents</p>
        {conversations.length === 0 ? (
          <p className="px-2.5 py-1 text-xs text-ink-faint">Nothing yet.</p>
        ) : (
          <ul>
            {conversations.map((conversation) => (
              <li key={conversation.id}>
                <Link
                  href={`/conversations/${conversation.id}`}
                  className={`block truncate rounded-lg px-2.5 py-1.5 text-sm transition-colors ${
                    pathname === `/conversations/${conversation.id}`
                      ? "bg-surface text-ink shadow-lift"
                      : "text-ink-soft hover:bg-surface-2"
                  }`}
                >
                  {conversation.title ?? "Untitled chat"}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="border-t border-edge px-4 py-3">
        <div className="flex items-center gap-2 text-xs text-ink-faint">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              health?.status === "ok" ? "bg-complete" : "bg-failed"
            }`}
          />
          {health ? health.generation_model : "connecting…"}
        </div>
      </div>
    </aside>
  );
}
