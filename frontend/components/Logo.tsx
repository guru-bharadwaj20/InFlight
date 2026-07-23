/**
 * InFlight mark: an upward paper-plane cutting across a trail, blue fading to
 * orange — motion caught mid-air. The gradient id is suffixed so several marks
 * on one page don't collide.
 */
export function Logo({
  className = "h-6 w-6",
  id = "if",
}: {
  className?: string;
  id?: string;
}) {
  const grad = `inflight-grad-${id}`;
  return (
    <svg viewBox="0 0 24 24" className={className} fill="none" aria-hidden>
      <defs>
        <linearGradient id={grad} x1="3" y1="21" x2="21" y2="3" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="rgb(var(--flight))" />
          <stop offset="1" stopColor="rgb(var(--ember))" />
        </linearGradient>
      </defs>
      {/* vapour trail */}
      <path
        d="M3 20c4-1 6-3 8-6"
        stroke={`url(#${grad})`}
        strokeWidth="1.6"
        strokeLinecap="round"
        opacity="0.4"
      />
      {/* the plane */}
      <path
        d="M21 3.5 4.8 11.2l6.1 1.9 1.9 6.1L21 3.5Z"
        fill={`url(#${grad})`}
      />
      <path d="M11 13 21 3.5" stroke="rgb(var(--surface))" strokeWidth="1" strokeLinecap="round" opacity="0.55" />
    </svg>
  );
}
