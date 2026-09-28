# The web portal (Next.js, standalone output). Ported from fishsense-lite@77e8f8e5
# apps/fishsense-lite-web/Dockerfile.
#
# Built with the **repo root** as context, like the Python images:
#
#   docker build -f apps/web/Dockerfile -t fishsense-web .
#
# Nothing is read at build time: every setting (the API URL, the tenant, the
# Authentik client, Label Studio) is read per request, so one image serves
# every environment.

FROM node:24-alpine AS deps
WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund --loglevel=error

FROM node:24-alpine AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY apps/web/ ./
RUN npm run build

FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
RUN addgroup -g 1001 -S nodejs && adduser -S -u 1001 -G nodejs nextjs
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
USER nextjs
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
CMD ["node", "server.js"]
