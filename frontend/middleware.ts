import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * Per-request Content-Security-Policy with a fresh script nonce.
 *
 * The policy this replaces set only `connect-src` and `frame-ancestors`, and
 * said so: it "constrains network egress without trying to lock down every
 * directive". That left the directive that actually matters unset. The session
 * token lives in localStorage, so any injected script could read it and post it
 * anywhere -- and with no `script-src` there was nothing stopping the injection
 * from executing in the first place. `connect-src` is not a containment boundary
 * either: an attacker who can run script can navigate the page or plant an image
 * to a host of their choosing.
 *
 * A nonce is needed because Next inlines its bootstrap and hydration scripts,
 * which a bare `script-src 'self'` would block. The nonce is minted here per
 * request; Next picks it up from this header for its own tags, and the theme
 * no-flash script in layout.tsx reads it back out of the request headers.
 * `strict-dynamic` then extends that trust to the chunks those scripts load, so
 * the policy does not need a host allowlist that an attacker could hunt for a
 * JSONP endpoint on.
 */
function cspFor(nonce: string): string {
  const apiOrigin = originOf(process.env.NEXT_PUBLIC_API_BASE_URL, "http://localhost:8000");
  const wsOrigin = originOf(process.env.NEXT_PUBLIC_WS_URL, "ws://localhost:8000");

  const directives = [
    "default-src 'self'",
    // 'unsafe-eval' only in development: React Refresh needs it, and shipping it
    // to production would hand back much of what the policy is here to prevent.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${
      process.env.NODE_ENV === "production" ? "" : " 'unsafe-eval'"
    }`,
    // Tailwind ships a stylesheet, but framer-motion animates by writing inline
    // style attributes on elements, which style-src governs. Nonces cannot cover
    // attribute-level styles, so this is the honest cost of the animation layer.
    "style-src 'self' 'unsafe-inline'",
    // data: for attachment thumbnails and camera captures, blob: for object URLs.
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "media-src 'self' blob:",
    `connect-src 'self' ${apiOrigin} ${wsOrigin} https://raw.githubusercontent.com`,
    "frame-ancestors 'none'",
    "frame-src 'none'",
    "object-src 'none'",
    // Stops an injected <base> silently repointing every relative script URL.
    "base-uri 'self'",
    "form-action 'self'",
  ];
  return directives.join("; ") + ";";
}

// CSP source expressions match by origin; a path component (e.g. the /ws in
// NEXT_PUBLIC_WS_URL) would be read as a path restriction rather than ignored,
// so both are normalised to scheme+host+port.
function originOf(value: string | undefined, fallback: string): string {
  try {
    return new URL(value ?? fallback).origin;
  } catch {
    return fallback;
  }
}

export function middleware(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const csp = cspFor(nonce);

  // Forwarded on the *request* so server components can read the nonce back.
  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("Content-Security-Policy", csp);

  const response = NextResponse.next({ request: { headers } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  // Skip static assets and image optimisation: they are not documents, so a
  // document policy buys nothing there, and minting a nonce per asset would
  // defeat caching.
  matcher: [
    {
      source: "/((?!_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
