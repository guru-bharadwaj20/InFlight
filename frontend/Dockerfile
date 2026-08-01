# Local dev is the default target so `docker compose build` (no --target)
# keeps working unchanged: docker-compose.yml bind-mounts ./frontend over it
# and runs the dev server for hot reload.
#
# Production is a separate target this file didn't have before:
#   docker build --target runner -t inflight-frontend .
# It runs `next build` with output: "standalone" (next.config.mjs) and ships
# only the self-contained server output, not the dev toolchain or node_modules.

FROM node:22-alpine AS base
WORKDIR /app
COPY package.json package-lock.json* ./

FROM base AS dev
# npm ci, not npm install: the lockfile is committed and the builder stage below
# already respects it, so using install here meant the dev container could
# silently resolve a different dependency tree than the one CI and production
# build against.
RUN npm ci
COPY . .
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD wget -q -O /dev/null http://localhost:3000/ || exit 1
CMD ["npm", "run", "dev"]

FROM base AS builder
RUN npm ci
COPY . .
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
RUN addgroup -S nodejs && adduser -S nextjs -G nodejs
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD wget -q -O /dev/null http://localhost:3000/ || exit 1
CMD ["node", "server.js"]
