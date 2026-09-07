# gateway

The platform's single entry point for USER traffic (Go, `:8000`). It
authenticates, authorizes, and routes — and does nothing else.

## What it owns, deliberately: nothing stateful

- **No database.** `go.mod` carries no SQL driver — the gateway connects to no
  database at all. This is verifiable in the file, which is the point: after
  P5.5 the gateway is a pure CONSUMER of identity-service (auth/rbac traffic is
  reverse-proxied there; binding writes are delegated).
- **Authorization is local.** The `Authorize` middleware decides every request
  against an authorization SNAPSHOT fetched from identity-service (ADR-007) —
  no per-request RPC, no database on the hot path. Cache correctness rests on
  `perms_version` carried in the JWT, so a revocation takes effect on the
  caller's next request.
- **Lease-aware routing.** A working copy is owned by exactly ONE
  workspace-runtime replica (a Redis lease). `internal/leaserouter` resolves
  working-copy requests to that replica, caches the decision for 10s, and
  invalidates on forward failure so the next attempt re-resolves.
- **Cell routing.** `internal/cellrouter` reads the VERIFIED `cell_id` claim
  and refuses (421) requests whose tenant lives in another cell, answering
  with that cell's address (P7).

## Route topology — four shapes

| Shape | Backend | Resolution |
|---|---|---|
| Identity proxy | identity-service `:8003` | plain (`/auth`, `/me`, `/rbac`) |
| Agent | agent-service `:8001` | load balanced (stateless service) |
| Control plane | control-service `:8004` | load balanced (database-backed state) |
| Working copy | workspace-runtime `:8002` | **lease-resolved** to the owning replica |

All working-copy groups share ONE `ProxyTarget` constructor in
`internal/router/router.go` — prefix handling is the only axis that varies, and
a single constructor is what guarantees a lease-semantics change reaches every
group at once.

## Layout

```
cmd/server/          entrypoint
internal/
├── auth/            JWT verification only (identity-service mints)
├── authz/           authorization snapshot resolver + binding writes (delegated)
├── cellx/           this instance's cell identity (log lines, Redis keys)
├── cellrouter/      refuse cross-cell requests (421 + target cell address)
├── config/          platform.yaml + profile loading (base + DATACODE_ENV)
├── handler/         proxies (generic, validated, SSE), identity proxy, workspace handlers
├── leaserouter/     working-copy → replica resolution (cache + invalidation)
├── middleware/      JWT auth, identity sanitize, Authorize
├── rbac/            route → permission map + workspace path parsing (pure
│                    decision tables; the permission CATALOG itself lives in
│                    shared/go/rbac, so identity-service cannot disagree)
├── redisx/          Redis client wiring
└── router/          chi setup: routes, CORS, middleware order
```

## Run

```
go test ./...
go run ./cmd/server        # needs GW_JWT_SECRET, IDENTITY_SERVICE_INTERNAL_TOKEN
```

See the root README for the full dev loop (`make dev`) and
`doc/api/gateway-api.md` for the route surface and error codes.
