# Configuration

One mechanism, three deployment shapes. This page is the complete reference:
how values resolve, what each profile selects, every environment variable,
and how to inspect the result.

## The resolution order

Every service — Python and Go alike — resolves its configuration through the
same four layers, first match wins:

```
1. code defaults        each service's Settings / Config struct
2. conf/platform.yaml   the neutral base: cross-environment invariants only
3. conf/platform.<PROFILE>.yaml   selected by DATACODE_ENV, deep-merged
4. environment vars     highest priority (secrets are env-ONLY, never YAML)
```

Then each service reads its own `conf/<service>.yaml`
(`workspace-runtime.yaml`, `agent-service.yaml`, `control-service.yaml`,
`gateway.yaml`) for service-specific settings, on the same terms: env vars
with the service's prefix win over the file.

**Where the files are found**: `$CONFIG_PATH` first, otherwise the
repository's `conf/` directory. Host dev relies on the latter; containers
and Kubernetes mount a directory at `/app/conf` and set `CONFIG_PATH=/app/conf`.
The mechanism never changes between environments — only which profile is
selected and which secrets are supplied.

## Why the base carries no addresses

`conf/platform.yaml` holds structure and invariants (pool sizes, timeouts,
the log-mirroring policy, the env-var registry) but **no host, port, URL or
path**. Every address lives in a profile.

This is deliberate. When the base held dev values, every container deployment
had to ship a complete replacement file, and those copies drifted (one lost
the `cells` section, another the `security` documentation). Profiles that
carry only their difference cannot drift like that — what they do not
mention, they inherit.

The consequence: **`DATACODE_ENV` must be set.** A process started without it
resolves a structurally valid config with no usable addresses, logs a
`config_profile_unset` warning at startup, and fails fast. `make doctor`
treats the same condition as an error.

## The six profiles

| Profile | For | Database | Redis | Notes |
|---|---|---|---|---|
| `dev` | host development | `localhost:5432/datacode` | `localhost:6379` | debug/text logging; no egress proxy (no NetworkPolicy locally) |
| `docker` | single-machine all-container stack (`make up`) | `postgres:5432` | `redis:6379` | compose service names |
| `cell` | one containerized Cell (`make cell-up`) | `cell-pg:5432` + `database.services.identity → global-pg` | `cell-redis:6379` | `expect_global_db: true` |
| `global` | the global tier (`make global-up`) | `global-pg:5432/identity` | disabled (single replica) | identity only; publishes no other URL |
| `k8s-cell` | a Cell in Kubernetes | `postgres.datacode.svc…` + identity → `global-pg.datacode-global.svc…` | `redis.datacode.svc…` | egress proxy + egress DNS declared (NetworkPolicies ship with the overlay) |
| `k8s-global` | the global tier in Kubernetes | `global-pg.datacode-global.svc…/identity` | `redis.datacode-global.svc…` | enabled here: multi-replica identity needs a shared rate limiter |

Global/Cell rule, enforced at startup: a profile that sets
`cells.expect_global_db: true` must also override `database.services.identity`
(and vice versa). `cell`/`k8s-cell`/`global`/`k8s-global` all declare both.

## Environment variables — the complete registry

`conf/platform.yaml`'s `security` section is the canonical list; this table is
the same information with symptoms attached.

### Required

| Variable | Used by | Without it |
|---|---|---|
| `DB_PASSWORD` | all DB-backed services | nothing connects; a wrong-but-present value crash-loops in ways that look like anything but auth. Locally the value is injected from `.data/postgres_password` (see below), so the shell only needs it on a bare `docker compose` run |
| `GW_JWT_SECRET` | gateway + identity-service | identity cannot mint tokens, the gateway cannot verify them. ONE value installation-wide; locally injected from `.data/gateway_jwt_secret` (truth file) |
| `IDENTITY_SERVICE_INTERNAL_TOKEN` | identity + gateway + control | identity's `/internal/*` disabled AND the gateway denies every authorization-requiring route (fail closed) |
| `WORKSPACE_RUNTIME_INTERNAL_TOKEN` | runtime + gateway + agent + workspace-sync | env-control channel, lease placement/accept/drain and code-location inventory all reject their callers. Without it, agent-service cannot place a lease either, so its calls fall back to the router's degraded path |
| `CONTROL_SERVICE_RUNTIME_INTERNAL_TOKEN` | control-service | control cannot dispatch any filesystem or engine side effect. Must equal `WORKSPACE_RUNTIME_INTERNAL_TOKEN` |

### Optional

| Variable | Used by | Notes |
|---|---|---|
| `DATACODE_TASK_TOKEN_SECRET` | runtime + egress-proxy | sandbox egress tokens; matters only where the egress plane runs. Deliberately NOT `GW_JWT_SECRET` |
| `REDIS_PASSWORD` | all services using Redis | only when your Redis uses requirepass |
| `CONTROL_SERVICE_CELL_MIGRATION_TOKEN` | control-service | cell-migration endpoints stay disabled when unset (fail closed) |
| `AGENT_SERVICE_LLM_API_KEY` | agent-service | global LLM fallback; per-model overrides exist (`conf/agent-service.yaml`) |

### Service-prefixed overrides

Per-service settings accept `<PREFIX>_<FIELD>` env overrides (highest
priority): `WORKSPACE_RUNTIME_LEASE_ENABLED`, `CONTROL_SERVICE_REST_PORT`,
`AGENT_SERVICE_WS_PORT`, `GW_HTTP_PORT`, … One caveat worth knowing: the
override must name a field the service's Settings actually has — a variable
naming a YAML-only key (or a typo) is silently ignored.

## Secrets handling

- Secrets are **env-only**. No profile or service YAML ever carries a value.
- The PostgreSQL password additionally has a **single truth file** locally:
  `.data/postgres_password` (gitignored). Every local flow reads it — the dev
  scripts export it as `DB_PASSWORD`, the Makefile injects it into every
  compose invocation, and the postgres containers mount it as
  `POSTGRES_PASSWORD_FILE` (an env-based `POSTGRES_PASSWORD` only takes effect
  on the volume's FIRST initialization, which is how a declared and an actual
  password used to drift apart). It is created on first use (adopting the
  password a running instance already accepts) and changed only via
  `make pg-set-password PASSWORD=...` (ALTER USER + file update, one step).
  A `DB_PASSWORD` set in the shell/`.env` is only consulted to seed that file
  and is ignored — with a warning — once the file exists.
- Host dev: `.env` (gitignored) for `AGENT_SERVICE_LLM_API_KEY`; the platform
  secrets are auto-generated under `.data/` on first `make dev` and persist
  across restarts.
- `GW_JWT_SECRET` has the same single-truth-file treatment locally:
  `.data/gateway_jwt_secret` (gitignored), created on demand by
  `scripts/jwt-secret.sh ensure`. `make dev` exports it and every make compose
  target injects it; because host dev and the container stacks share the
  file, a login token minted in one stack verifies in the other. Rotation =
  delete the file (every issued token dies with it — that IS the rotation
  semantics; there is deliberately no `set` subcommand).
- Docker: `deploy/.env` (gitignored; `deploy/.env.example` is the template)
  for the optional values only (`AGENT_SERVICE_LLM_API_KEY`, the cell port
  overrides) — the make targets inject `DB_PASSWORD` and `GW_JWT_SECRET`
  from the truth files themselves.
- Kubernetes: Secrets created with kubectl — see
  `deploy/k8s/base/secrets.example.yaml` for the exact commands and keys.

## Inspecting what a service will actually use

```bash
make doctor                     # all profiles: invariant checks, no secrets printed
make doctor PROFILE=dev SHOW=1  # one profile, full detail
make doctor PROFILE=k8s-cell --check-secrets   # + report which secrets the env supplies
```

The values come from running the REAL loader with `DATACODE_ENV` set — not a
re-implementation — so what it prints is what the process would use. It is a
CI step (`.github/workflows/ci.yml`) and a reasonable pre-release ritual.

## Common misconfigurations and their symptoms

| Symptom | Likely cause |
|---|---|
| `config_profile_unset` warning; services resolve `localhost`/empty URLs | `DATACODE_ENV` not set — start via `make dev` / the compose files, which set it |
| `unsupported protocol scheme ""` from the gateway per request | a profile is missing that service's URL entry; `make doctor` names it |
| Every auth request denied, identity unreachable on internal routes | `IDENTITY_SERVICE_INTERNAL_TOKEN` missing or mismatched between gateway and identity |
| Two cells show the same users | a cell profile's `database.services.identity` points at its own database — `expect_global_db` exists to make this a startup error, so it was unset or bypassed |
| env override appears to do nothing | the variable names a field the service's Settings lacks, or it is shadowed by a YAML value (report it — the loaders drop env-provided keys precisely to prevent this) |
