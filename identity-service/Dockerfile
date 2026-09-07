# Identity service image (design 03 §2, P7) — the GLOBAL tier.
#
# Build context is the datacode/ directory, NOT identity-service/: the module
# replaces github.com/datacode/shared with the local ../shared/go package
# (the RBAC catalog and lease key algebra, both cross-service contracts with
# the gateway), so both must be inside the context:
#   docker build -f identity-service/Dockerfile -t datacode/identity:<ver> .
#
# Copying shared/go/ is not optional: go.mod's `replace` points at a path
# outside the module root, so without it `go mod download` fails with
# "replacement directory ../shared/go does not exist".
#
# The only writer of the global identity tables: users, tenants, roles and
# tenant_placement (which cell a tenant lives in). Reached on the login path
# and for placement/registry calls — never on a cell's hot request path, which
# is why one instance serves the whole installation.

FROM golang:1.25-alpine AS build

WORKDIR /src

# The replace target (../shared/go from identity-service/) must exist for
# module graph resolution; the package is tiny (go.mod + rbac/lease/httperr).
COPY shared/go/ shared/go/
COPY identity-service/go.mod identity-service/go.sum identity-service/
RUN cd identity-service && go mod download

COPY identity-service/ identity-service/
# CGO off works because the SQLite driver is modernc.org/sqlite (pure Go).
# Container deployments use Postgres (deploy/conf-global); SQLite remains a
# host/dev fallback only. -p limits concurrent package compiles: modernc's
# libc/sqlite packages are large, and on small builder VMs (e.g. a 2 GiB
# colima default) even -p 2 gets OOM-killed mid-build. 1 is slow but safe
# everywhere; raise with --build-arg GO_BUILD_PARALLEL=N on hosts that can
# afford it.
ARG GO_BUILD_PARALLEL=1
RUN cd identity-service && CGO_ENABLED=0 go build -trimpath -p ${GO_BUILD_PARALLEL} -o /out/identity ./cmd/server

FROM alpine:3.20
# ca-certificates: TLS to global-pg and to JWT-relevant HTTPS endpoints must
# not fail on missing roots.
RUN apk add --no-cache ca-certificates \
    && addgroup -S -g 10001 app \
    && adduser -S -u 10001 -G app app
WORKDIR /app
COPY --from=build /out/identity /app/identity
USER 10001:10001

# SQL migrations are //go:embed-ed into the binary — nothing ships alongside.
# Config is mounted via CONFIG_PATH -> /app/conf (deploy/conf-global).
EXPOSE 8003

# Shallow liveness on /health (busybox wget ships with alpine). The generous
# start-period covers single-replica boots that run AUTO_MIGRATE first: the
# compose stacks' `depends_on: condition: service_healthy` gates the gateway
# on this signal, so a healthcheck the service cannot satisfy in time would
# deadlock the whole stack's startup.
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=6 \
  CMD wget -q -O /dev/null http://localhost:8003/health || exit 1

ENTRYPOINT ["/app/identity"]
