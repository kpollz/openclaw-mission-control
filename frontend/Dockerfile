# syntax=docker/dockerfile:1

FROM node:20-alpine AS deps
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

FROM node:20-alpine AS builder
WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY . ./

# Build-time args for NEXT_PUBLIC_* — these get inlined into JS bundles.
# NEXT_PUBLIC_API_URL=auto means the browser resolves the host dynamically
# and reads the backend port from window.__RUNTIME_CONFIG__ (no rebuild needed
# when BACKEND_PORT changes).
ARG NEXT_PUBLIC_API_URL=auto
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}
ARG NEXT_PUBLIC_AUTH_MODE
ENV NEXT_PUBLIC_AUTH_MODE=${NEXT_PUBLIC_AUTH_MODE}

RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production

# Runtime env vars — passed via docker-compose `environment:`.
# BACKEND_PORT is read by the entrypoint script and injected into the browser
# at startup via public/runtime-config.js — no image rebuild needed.
ARG NEXT_PUBLIC_AUTH_MODE
ENV NEXT_PUBLIC_AUTH_MODE=${NEXT_PUBLIC_AUTH_MODE}

# Entrypoint generates runtime config before starting the server.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Create non-root user before COPY so --chown can reference it.
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
  && addgroup -S appgroup && adduser -S -G appgroup appuser \
  && chown appuser:appgroup /app

COPY --from=builder --chown=appuser:appgroup /app/.next ./.next
# `public/` is optional in Next.js apps; repo may not have it.
# Avoid failing the build when the directory is absent.
COPY --from=builder --chown=appuser:appgroup /app/package.json ./package.json
COPY --from=builder --chown=appuser:appgroup /app/node_modules ./node_modules
COPY --from=builder --chown=appuser:appgroup /app/next.config.ts ./next.config.ts

USER appuser

EXPOSE 3000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["npm", "run", "start"]
