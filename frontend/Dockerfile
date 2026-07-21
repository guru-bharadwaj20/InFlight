FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .

EXPOSE 3000

# Dev server: compose bind-mounts ./frontend so edits hot-reload.
CMD ["npm", "run", "dev"]
