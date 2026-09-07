# Frontend image — static hosting behind nginx.
#
# Build context is the datacode/ directory, uniform with the service images:
#   docker build -f frontend/Dockerfile -t datacode/frontend:<ver> .
#
# Stage 1 builds the SPA; stage 2 serves it. The nginx config (frontend/nginx.conf,
# copied in below) reverse-proxies /api and /ws to the gateway, so the browser
# talks to ONE origin and the gateway is never exposed directly.

FROM node:20-alpine AS build

WORKDIR /app

# Dependencies first: layer caching keyed on the lockfile alone.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# VITE_API_BASE_URL is baked at build time; empty keeps every request on the
# same origin, which is what the nginx proxy below provides.
# vue-tsc + vite peak well past the ~2 GiB V8 default, and node does NOT size
# its heap from the cgroup memory limit — raise the ceiling explicitly. (Same
# lesson as gateway's GO_BUILD_PARALLEL: builder VMs smaller than this need
# not apply.)
RUN NODE_OPTIONS=--max-old-space-size=4096 npm run build

FROM nginx:1.27-alpine

# nginx.conf is a TEMPLATE: ${GATEWAY_UPSTREAM} is substituted from the
# environment at container start, so one image serves every topology (compose
# service name, in-cluster Service DNS). The default matches the compose stack.
COPY frontend/nginx.conf /etc/nginx/templates/default.conf.template
ENV GATEWAY_UPSTREAM=gateway:8000
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# /healthz is served by the nginx config (a constant 200); the container needs
# no other health signal — nginx fails only by exiting.
HEALTHCHECK --interval=15s --timeout=3s --retries=4 \
  CMD wget -q -O /dev/null http://localhost/healthz || exit 1
