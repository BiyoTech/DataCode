# Gateway image (design 03 §2, P7) — a cell's edge.
#
# Build context is the datacode/ directory, NOT gateway/: the module replaces
# github.com/datacode/shared with the local ../shared/go package, so both must
# be inside the context (see deploy/docker-compose.cell.yml):
#   docker build -f gateway/Dockerfile -t datacode/gateway:<ver> .
#
# Deliberately stateless beyond Redis: verifies tokens minted by the GLOBAL
# identity service, routes by the token's cell_id claim, then proxies. One
# image serves every cell — cell identity arrives via DATACODE_CELL_ID and the
# mounted config directory, never via the build.

FROM golang:1.25-alpine AS build

WORKDIR /src

# The replace target (../shared/go from gateway/) must exist for module graph
# resolution; the package is tiny (go.mod + the lease key algebra).
COPY shared/go/ shared/go/
COPY gateway/go.mod gateway/go.sum gateway/
RUN cd gateway && go mod download

COPY gateway/ gateway/
# -p limits concurrent package compiles: the compile step is memory-bound, and
# on small builder VMs (e.g. a 2 GiB colima default) even -p 2 gets packages
# OOM-killed mid-build. 1 is slow but safe everywhere; raise with
# --build-arg GO_BUILD_PARALLEL=N on hosts that can afford it.
ARG GO_BUILD_PARALLEL=1
RUN cd gateway && CGO_ENABLED=0 go build -trimpath -p ${GO_BUILD_PARALLEL} -o /out/gateway ./cmd/server

FROM alpine:3.20
# ca-certificates: outbound TLS must not fail on missing roots.
RUN apk add --no-cache ca-certificates \
    && addgroup -S -g 10001 app \
    && adduser -S -u 10001 -G app app
WORKDIR /app
COPY --from=build /out/gateway /app/gateway
USER 10001:10001

# Config is mounted at runtime via CONFIG_PATH -> /app/conf (deploy/conf-cell);
# nothing is baked in. The gateway writes no files — logs go to stdout.
EXPOSE 8000

# Shallow liveness on /health only (busybox wget ships with alpine): the deep
# upstream probe is opt-in via ?deep=1, deliberately NOT used here — the
# orchestrator must not restart the edge because a backend is flapping. The
# compose stacks' `depends_on: condition: service_healthy` (frontend on
# gateway) reads this signal.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=6 \
  CMD wget -q -O /dev/null http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/gateway"]
