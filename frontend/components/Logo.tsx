/** Three marks in motion, one already landed — the project in one glyph. */
export function Logo({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden fill="none">
      <path
        d="M3 7h9M3 12h13M3 17h7"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        opacity="0.35"
      />
      <circle cx="19" cy="7" r="2.2" fill="currentColor" opacity="0.45" />
      <circle cx="21" cy="12" r="2.2" fill="currentColor" />
      <circle cx="17" cy="17" r="2.2" fill="currentColor" opacity="0.25" />
    </svg>
  );
}
