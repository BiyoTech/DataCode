# identity-service

The platform's GLOBAL source of truth for identity, authorization and tenant
placement (design 02 §2.2, ADR-007). Delivered by design 06 P2.

## Why it is a service and not gateway code

- **Identity is global, gateways are per-cell.** Identity tables cannot travel
  with a cell, so they cannot live in the gateway's database.
- **Non-HTTP entry points had no authorization path.** Dagster schedules,
  reconcilers and CLI shims never pass through gateway middleware; they need a
  callable authorization API.
- **The schema had no safe change procedure.** The DDL used to run as ad-hoc
  `CREATE TABLE IF NOT EXISTS` at gateway boot, so adding a column to `users`
  had no versioned, reviewable path. Migrations here are managed.

## Surfaces

| Surface | Paths | Auth |
|---|---|---|
| Public | `/auth/{register,login,select-tenant,refresh}` | none (whitelisted) |
| Authenticated | `/auth/{logout,change-password,memberships,members,tenant-members}`, `/me`, `/me/preferences`, `/rbac/*` | JWT |
| Internal | `/internal/authz/resolve`, `/internal/tenants/{id}/placement`, `/internal/tenants/{id}/rbac/role-drift`, `/internal/cells` | `X-Internal-Token`, **fail closed** |

`/internal/*` is deliberately outside `/api/v1`, so no gateway forwards it and
it is unreachable from the user side.

## Who owns what in RBAC

| Data | Source of truth | Who may change it |
|---|---|---|
| Which permission codes EXIST | code — `shared/rbac-catalog.json`, generated into `shared/go/rbac` | a release |
| Which permissions a ROLE grants | this service's `roles` / `role_permissions` | the tenant, including for built-in roles |
| Who HOLDS a role | `tenant_members` / `workspace_members` | an administrator |

The catalog is synchronized into the `permissions` table on startup
(`Store.SyncPermissions`), which makes that table a PROJECTION: it renders the
role editor and keeps historical role compositions readable after a code is
retired (retired codes are flagged `deprecated`, never deleted). It is never
written through the API, so a permission code cannot be invented by writing to
the database — and `validatePermissions` rejects any code outside the catalog.

Built-in roles are seeded as templates (`tenant_id = ''`) and cloned per tenant
once, so a tenant's edit survives every restart. The cost of that rule is that a
permission added to a template never reaches a role the tenant customized:
`EnsureTenantRoles` reports those as `builtin_role_permission_drift` and
`/internal/tenants/{id}/rbac/role-drift` lists the backlog on demand. Silently
backfilling would revoke the tenant's own grants; staying silent would leave a
shipped permission ungrantable with a 403 as the only symptom.

## The authorization snapshot

`GET /internal/authz/resolve?user_id&tenant_id` returns everything the gateway
needs to decide locally: the tenant role and its permissions, the caller's
workspace bindings, and the permission set of each role those bindings
reference (keyed by role, so many workspaces on one role cost one set).

A per-check RPC was rejected: the gateway authorizes every request, so that
would put identity on the hot path of the whole platform. Cache correctness
rests on `perms_version` (carried in the JWT) rather than on the TTL alone — a
revocation therefore takes effect on the caller's next request.

## Tokens

Access tokens carry `cell_id` and `perms_version` in addition to the usual
claims. The global edge routes on `cell_id` by reading the token alone, with no
database lookup on the hot path.

## Migrations

Versioned SQL under `internal/migrate/sql/NNNN_*.sql`, recorded in
`schema_migrations`, applied in order, each at most once.

- `0001` is the identity baseline. It uses `IF NOT EXISTS` so it is adoptable
  on a database created by the pre-P2 gateway (it becomes a no-op there and
  only records itself).
- `0002` adds `cells`, `tenant_placement`, `tenant_perms_version` and the
  `users.timezone` / `users.locale` preference columns.

`ADD COLUMN` is not idempotent outside PostgreSQL, so a statement may declare a
guard the migrator evaluates through `dbx` introspection:

```sql
-- +migrate ifNoColumn users.timezone
ALTER TABLE users ADD COLUMN timezone VARCHAR(64) NOT NULL DEFAULT 'UTC';
```

The service **refuses to serve** when migrations are pending: running
application code against an older schema produces wrong answers rather than
errors. `IDENTITY_SERVICE_AUTO_MIGRATE=1` applies them at boot (dev); in
production the deployment pipeline applies them as a separate step so replicas
do not race during a rolling update.

## Environment

```
GW_JWT_SECRET                      required, SHARED with the gateway
IDENTITY_SERVICE_INTERNAL_TOKEN    required for /internal/*, shared with the gateway
IDENTITY_SERVICE_PORT              default 8003
IDENTITY_SERVICE_AUTO_MIGRATE      1 to migrate on boot (dev only)
IDENTITY_SERVICE_DB_PATH           sqlite override (dev); otherwise conf/platform.yaml
DB_PASSWORD                        for mysql / postgresql
```

## Run

```
go test ./...
GW_JWT_SECRET=dev-secret \
IDENTITY_SERVICE_INTERNAL_TOKEN=dev-itok \
IDENTITY_SERVICE_AUTO_MIGRATE=1 \
IDENTITY_SERVICE_DB_PATH=.data/identity.db \
go run ./cmd/server
```
