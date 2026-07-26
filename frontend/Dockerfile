FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .

EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD wget -q -O /dev/null http://localhost:3000/ || exit 1

# Dev server: compose bind-mounts ./frontend so edits hot-reload.
CMD ["npm", "run", "dev"]
