"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Conversation } from "@/lib/api";
import { useAuth } from "@/lib/useAuth";
import { Logo } from "./Logo";
import { ThemeToggle } from "./theme";

/**
 * One chat in the rail: a link, a star marker, and a 3-dot menu offering
 * exactly star / rename / delete. Rename swaps the label for an input in place;
 * delete confirms first, since it cannot be undone.
 */
function ChatRow({
  conversation,
  active,
  onChanged,
  onDeleted,
}: {
  conversation: Conversation;
  active: boolean;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(conversation.title ?? "");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (renaming) inputRef.current?.select();
  }, [renaming]);

  async function commitRename() {
    setRenaming(false);
    const title = draft.trim();
    if (title && title !== conversation.title) {
      await api.updateConversation(conversation.id, { title });
      onChanged();
    }
  }

  async function toggleStar() {
    setMenuOpen(false);
    await api.updateConversation(conversation.id, { starred: !conversation.starred });
    onChanged();
  }

  async function remove() {
    setMenuOpen(false);
    if (!window.confirm("Delete this chat? This cannot be undone.")) return;
    await api.deleteConversation(conversation.id);
    onDeleted();
  }

  if (renaming) {
    return (
      <li>
        <input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commitRename}
          onKeyDown={(e) => {
            if (e.key === "Enter") void commitRename();
            if (e.key === "Escape") setRenaming(false);
          }}
          className="w-full rounded-lg border border-flight bg-bg px-2.5 py-1.5 text-sm outline-none"
        />
      </li>
    );
  }

  return (
    <li className="group relative">
      <Link
        href={`/conversations/${conversation.id}`}
        className={`flex items-center gap-1.5 rounded-lg py-1.5 pl-2.5 pr-8 text-sm transition-colors ${
          active ? "bg-surface text-ink shadow-lift" : "text-ink-soft hover:bg-surface-2"
        }`}
      >
        {conversation.starred && (
          <svg viewBox="0 0 20 20" className="h-3 w-3 shrink-0 text-ember" fill="currentColor" aria-hidden>
            <path d="M10 1.6l2.6 5.3 5.8.8-4.2 4.1 1 5.8L10 15l-5.2 2.7 1-5.8L1.6 7.7l5.8-.8z" />
          </svg>
        )}
        <span className="truncate">{conversation.title ?? "Untitled chat"}</span>
      </Link>

      <button
        onClick={() => setMenuOpen((v) => !v)}
        aria-label="Chat options"
        className={`absolute right-1 top-1/2 -translate-y-1/2 rounded-md p-1 text-ink-faint transition-opacity hover:bg-surface-2 hover:text-ink ${
          menuOpen ? "opacity-100" : "opacity-0 group-hover:opacity-100 focus:opacity-100"
        }`}
      >
        <svg viewBox="0 0 20 20" className="h-4 w-4" fill="currentColor" aria-hidden>
          <circle cx="10" cy="4" r="1.5" />
          <circle cx="10" cy="10" r="1.5" />
          <circle cx="10" cy="16" r="1.5" />
        </svg>
      </button>

      {menuOpen && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setMenuOpen(false)} />
          <div className="absolute right-1 top-8 z-20 w-40 overflow-hidden rounded-xl border border-edge bg-surface py-1 shadow-composer">
            <MenuItem onClick={toggleStar}>
              <StarIcon filled={conversation.starred} />
              {conversation.starred ? "Unstar" : "Star"}
            </MenuItem>
            <MenuItem
              onClick={() => {
                setMenuOpen(false);
                setDraft(conversation.title ?? "");
                setRenaming(true);
              }}
            >
              <PencilIcon /> Rename
            </MenuItem>
            <MenuItem onClick={remove} danger>
              <TrashIcon /> Delete
            </MenuItem>
          </div>
        </>
      )}
    </li>
  );
}

function MenuItem({
  children,
  onClick,
  danger,
}: {
  children: React.ReactNode;
  onClick: () => void;
  danger?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-sm transition-colors hover:bg-surface-2 ${
        danger ? "text-failed" : "text-ink"
      }`}
    >
      {children}
    </button>
  );
}

const StarIcon = ({ filled }: { filled?: boolean }) => (
  <svg viewBox="0 0 20 20" className="h-4 w-4" fill={filled ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.5">
    <path d="M10 1.8l2.5 5.1 5.6.8-4 4 1 5.5L10 14.6 4.9 17.2l1-5.5-4-4 5.6-.8z" strokeLinejoin="round" />
  </svg>
);
const PencilIcon = () => (
  <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.5">
    <path d="M13.5 3.5l3 3L7 16l-3.5.5L4 13z" strokeLinejoin="round" />
  </svg>
);
const TrashIcon = () => (
  <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.5">
    <path d="M4 6h12M8 6V4h4v2M6 6l.7 10h6.6L14 6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

const IconPlus = (
  <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.7">
    <path d="M10 4v12M4 10h12" strokeLinecap="round" />
  </svg>
);

export function Sidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const [conversations, setConversations] = useState<Conversation[]>([]);

  const refresh = useCallback(async () => {
    try {
      setConversations(await api.listConversations());
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
    await refresh();
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

      <div className="px-2">
        <button
          onClick={startNew}
          className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm font-medium text-ink transition-colors hover:bg-surface-2"
        >
          <span className="text-flight">{IconPlus}</span>
          New chat
        </button>
      </div>

      <div className="mt-5 min-h-0 flex-1 overflow-y-auto px-2">
        <p className="px-2.5 pb-1 text-xs font-medium text-ink-faint">Recents</p>
        {conversations.length === 0 ? (
          <p className="px-2.5 py-1 text-xs text-ink-faint">Nothing yet.</p>
        ) : (
          <ul className="space-y-0.5">
            {conversations.map((conversation) => (
              <ChatRow
                key={conversation.id}
                conversation={conversation}
                active={pathname === `/conversations/${conversation.id}`}
                onChanged={refresh}
                onDeleted={() => {
                  if (pathname === `/conversations/${conversation.id}`) router.push("/");
                  void refresh();
                }}
              />
            ))}
          </ul>
        )}
      </div>

      <UserFooter />
    </aside>
  );
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "?";
}

/** Bottom-left profile — name and avatar only, no plan or billing. */
function UserFooter() {
  const { user, logout } = useAuth();
  const router = useRouter();
  const [open, setOpen] = useState(false);

  if (!user) return null;

  return (
    <div className="relative border-t border-edge p-2">
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute bottom-full left-2 z-20 mb-1 w-52 overflow-hidden rounded-xl border border-edge bg-surface py-1 shadow-composer">
            <button
              onClick={() => {
                setOpen(false);
                logout();
                router.replace("/login");
              }}
              className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm text-failed hover:bg-surface-2"
            >
              <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.7">
                <path d="M13 14v1a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v1M9 10h8m0 0-2.5-2.5M17 10l-2.5 2.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              Log out
            </button>
          </div>
        </>
      )}

      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-surface-2"
      >
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-gradient-to-br from-flight to-ember text-xs font-semibold text-white">
          {initials(user.name)}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm text-ink">{user.name}</span>
          <span className="block truncate text-xs text-ink-faint">{user.email}</span>
        </span>
      </button>
    </div>
  );
}
