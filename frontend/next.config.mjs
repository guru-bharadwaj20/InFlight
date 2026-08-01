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
          // Content-Security-Policy is NOT here: it carries a per-request script
          // nonce, so it has to be built per request in middleware.ts. Static
          // headers that need no nonce stay in this list.
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
