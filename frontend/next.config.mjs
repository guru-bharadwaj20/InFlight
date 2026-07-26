/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Lets the production Docker build (Dockerfile's `runner` stage) ship a
  // self-contained server directory instead of the whole node_modules tree.
  output: "standalone",
};

export default nextConfig;
