"use client";

import { useEffect, useRef, useState } from "react";

/**
 * The dropdown behaviour that was hand-rolled five times.
 *
 * AttachMenu, ModelPicker, the sidebar's per-chat menu and the user footer each
 * wrote their own `useState(false)` plus a `fixed inset-0` click-catcher. Being
 * copies, they shared the same omissions rather than differing in interesting
 * ways:
 *
 *   - Escape did not close them. ConfirmDialog was the only overlay in the app
 *     that handled it, so the behaviour was inconsistent as well as missing.
 *   - The trigger carried no aria-expanded or aria-haspopup, so a screen reader
 *     announced a button with no indication it opens anything, or whether it is
 *     currently open.
 *   - The panel had no role, so its contents were not announced as a menu.
 *   - Focus was never returned to the trigger on close, leaving keyboard focus
 *     stranded on a removed element.
 *   - The backdrop was a full-viewport div that swallowed the *first* click
 *     anywhere on the page, so dismissing a menu and pressing a button took two
 *     clicks.
 *
 * Fixing those once is the point of this component; every fix below then applies
 * everywhere a menu is used.
 */
export function Popover({
  trigger,
  children,
  align = "left",
  panelClassName = "",
  triggerClassName = "",
  label,
}: {
  /** Rendered inside the trigger button. Receives the current open state. */
  trigger: (open: boolean) => React.ReactNode;
  /** Panel contents. `close` lets an item dismiss the menu after acting. */
  children: (close: () => void) => React.ReactNode;
  align?: "left" | "right";
  panelClassName?: string;
  triggerClassName?: string;
  label: string;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;

    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      // Escape should hand focus back to what opened the menu, not drop it.
      triggerRef.current?.focus();
    };
    // Dismiss on any outside interaction, captured on the document rather than
    // via a full-viewport backdrop div. The backdrop approach ate the click that
    // dismissed it, so acting on something else always took two clicks.
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };

    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open]);

  return (
    <div ref={rootRef} className="relative">
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        onClick={() => setOpen((v) => !v)}
        className={triggerClassName}
      >
        {trigger(open)}
      </button>

      {open && (
        <div
          role="menu"
          aria-label={label}
          className={`absolute z-20 overflow-hidden rounded-xl border border-edge bg-surface py-1 shadow-composer ${
            align === "right" ? "right-0" : "left-0"
          } ${panelClassName}`}
        >
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  );
}
