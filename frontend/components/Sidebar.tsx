"use client";

import Link from "next/link";
import { AnimatePresence, motion } from "framer-motion";
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
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [draft, setDraft] = useState(conversation.title ?? "");
  const [rowError, setRowError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (renaming) inputRef.current?.select();
  }, [renaming]);

  async function commitRename() {
    setRenaming(false);
    const title = draft.trim();
    if (title && title !== conversation.title) {
      try {
        await api.updateConversation(conversation.id, { title });
        onChanged();
      } catch (err) {
        setRowError(err instanceof Error ? err.message : String(err));
      }
    }
  }

  async function toggleStar() {
    setMenuOpen(false);
    try {
      await api.updateConversation(conversation.id, { starred: !conversation.starred });
      onChanged();
    } catch (err) {
      setRowError(err instanceof Error ? err.message : String(err));
    }
  }

  async function confirmDelete() {
    setDeleting(true);
    try {
      await api.deleteConversation(conversation.id);
      onDeleted();
    } catch (err) {
      setRowError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeleting(false);
      setConfirmingDelete(false);
    }
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
            <MenuItem
              onClick={() => {
                setMenuOpen(false);
                setConfirmingDelete(true);
              }}
              danger
            >
              <TrashIcon /> Delete
            </MenuItem>
          </div>
        </>
      )}

      <ConfirmDialog
        open={confirmingDelete}
        title="Delete chat"
        body="Are you sure you want to delete this chat? This cannot be undone."
        confirmLabel={deleting ? "Deleting…" : "Delete"}
        busy={deleting}
        onConfirm={confirmDelete}
        onCancel={() => setConfirmingDelete(false)}
      />

      {rowError && (
        <p
          className="mt-0.5 truncate px-2.5 text-[11px] text-failed"
          onClick={() => setRowError(null)}
          title={rowError}
        >
          {rowError}
        </p>
      )}
    </li>
  );
}

/** Themed replacement for window.confirm — a centered card with a backdrop, an
 *  Escape-to-cancel handler, and a destructive (red) confirm button. */
function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  busy,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  body: string;
  confirmLabel: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
        >
          <div
            className="absolute inset-0 bg-black/40 backdrop-blur-sm"
            onClick={onCancel}
          />
          <motion.div
            role="dialog"
            aria-modal="true"
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: 8 }}
            transition={{ duration: 0.15, ease: "easeOut" }}
            className="relative w-full max-w-sm rounded-2xl border border-edge bg-surface p-5 shadow-composer"
          >
            <h2 className="text-base font-semibold text-ink">{title}</h2>
            <p className="mt-2 text-sm text-ink-soft">{body}</p>
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={onCancel}
                disabled={busy}
                className="rounded-lg border border-edge px-3.5 py-1.5 text-sm font-medium text-ink transition-colors hover:bg-surface-2 disabled:opacity-60"
              >
                Cancel
              </button>
              <button
                onClick={onConfirm}
                disabled={busy}
                className="rounded-lg bg-failed px-3.5 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-60"
              >
                {confirmLabel}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
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
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
    if (creating) return;
    setCreating(true);
    setError(null);
    try {
      const created = await api.createConversation(null);
      // Navigate straight away — the pathname change re-fires refresh() above,
      // so the new row appears without a second blocking round-trip here.
      router.push(`/conversations/${created.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setCreating(false);
    }
  }

  // Clear the "creating" latch once the URL has actually changed.
  useEffect(() => {
    setCreating(false);
  }, [pathname]);

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
          disabled={creating}
          className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm font-medium text-ink transition-colors hover:bg-surface-2 disabled:opacity-60"
        >
          <span className="text-flight">{IconPlus}</span>
          {creating ? "Starting…" : "New chat"}
        </button>
        {error && (
          <p className="mt-1.5 px-2.5 text-xs text-failed">{error}</p>
        )}
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
