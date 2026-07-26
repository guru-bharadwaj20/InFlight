// CSP source expressions match by origin; a path component (e.g. the /ws in
// NEXT_PUBLIC_WS_URL) would be interpreted as a path restriction, not just
// ignored, so both are normalized down to scheme+host+port.
function originOf(value, fallback) {
  try {
    return new URL(value ?? fallback).origin;
  } catch {
    return fallback;
  }
}

const apiOrigin = originOf(process.env.NEXT_PUBLIC_API_BASE_URL, "http://localhost:8000");
const wsOrigin = originOf(process.env.NEXT_PUBLIC_WS_URL, "ws://localhost:8000");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Lets the production Docker build (Dockerfile's `runner` stage) ship a
  // self-contained server directory instead of the whole node_modules tree.
  output: "standalone",
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          // Scoped rather than a strict default-src: the app only ever talks
          // to the configured backend origin (REST + WebSocket) and itself,
          // and framer-motion/Next's own runtime need normal script/style
          // execution, so this constrains network egress without trying to
          // (and risking breaking the app by) lock down every directive.
          {
            key: "Content-Security-Policy",
            // raw.githubusercontent.com is the GitHub-import feature in
            // Composer.tsx, which fetches directly from the browser.
            value: `connect-src 'self' ${apiOrigin} ${wsOrigin} https://raw.githubusercontent.com; frame-ancestors 'none';`,
          },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
