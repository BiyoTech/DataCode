# DataCode - AI Native DataOps Platform

An AI-native Data+AI development platform built on **SQLMesh + Daft + Dagster + OpenAI Agents SDK**,
enabling conversational data model development and Data+AI pipeline orchestration through natural language.

## Architecture

Five resident services, split by what they own rather than by feature area:

```
Frontend (Vue 3)
      │
      ▼
API Gateway (Go, 8000) ── authenticates, authorizes, routes
      │
      ├─→ identity-service   (Go,     8003)  identity, RBAC, tenant placement   [global]
      ├─→ agent-service      (Python, 8001)  conversational agents
      ├─→ control-service    (Python, 8004)  environment registry, reconciler,
      │                                      workspace metadata, Git credentials
      └─→ workspace-runtime  (Python, 8002)  the working-copy owner:
                                             SQLMesh engine + ontology
                                             + Dagster data plane + git + files
```

Supporting planes (production):

- **task-runner** — the sandbox that executes user code, one Pod per task or per warm runner.
- **egress-proxy** — the only outbound path open to a sandbox, gated by a scoped per-task token.

Key ownership rules:

- A **working copy** is owned by exactly one `workspace-runtime` replica at a time (a Redis lease);
  the gateway routes every working-copy request to that replica.
- `control-service` owns all platform metadata and the Alembic chain; it never touches the
  filesystem itself — every side effect is dispatched to `workspace-runtime`.
- `identity-service` is the single writer of the identity dataset, which is what makes the
  Global/Cell split possible.

Each module carries its own README with its positioning, layout and run
instructions:

| Module | README | One-liner |
|---|---|---|
| `gateway/` | [README](gateway/README.md) | user-facing edge: authenticate, authorize, lease-route, cell-route — no database |
| `identity-service/` | [README](identity-service/README.md) | global identity/RBAC/placement, the single writer of the identity dataset |
| `agent-service/` | [README](agent-service/README.md) | conversational agents; tool calls go to the runtime, never local |
| `control-service/` | [README](control-service/README.md) | control plane: registry, reconciler, quotas, the only alembic chain |
| `workspace-runtime/` | [README](workspace-runtime/README.md) | the working-copy owner: engine + git + files + gated tools |
| `task-runner/` | [README](task-runner/README.md) | the sandbox execution plane for user code |
| `egress-proxy/` | [README](egress-proxy/README.md) | the only outbound path a sandbox gets |
| `frontend/` | [README](frontend/README.md) | the SPA; talks to the gateway and nothing else |
| `shared/` | [README](shared/README.md) | the cross-language contract layer (Go + Python, one implementation) |
| `conf/` | [README](conf/README.md) | base + profile configuration mechanism |
| `deploy/` | [README](deploy/README.md) | every deployment shape (compose stacks, Kustomize) |
| `scripts/` | [README](scripts/README.md) | dev loop, acceptance gates, operator tooling |

Each user **Workspace** contains two core project directories plus a semantic layer:

- **`sql-warehouse/`** — SQLMesh project (data modeling / ELT / data quality).
- **`asset-dag/`** — Dagster project (Data+AI pipeline orchestration, Daft distributed DataFrame compute).
- **`ontology/`** — semantic layer.

## Quick Start

Everything runs on the host; storage (PostgreSQL + Redis) starts
automatically via Docker when not already running. PostgreSQL everywhere —
the same engine the Docker and Kubernetes shapes run.

```bash
make setup             # one-time: deps, per-service venvs, .env, storage
make dev               # start everything (frontend + backend + storage)
make stop              # stop the services
```

No database password to configure: it lives in ONE file,
`.data/postgres_password` (gitignored), created on first run — adopted from
a running postgres if one exists, seeded otherwise — read by every flow (dev
scripts, make, the postgres containers themselves), and changed only via
`make pg-set-password PASSWORD=...`.

Backend only: `make dev-backend`. Frontend only: `make dev-frontend`.
Follow service logs: `make dev-logs` (files under `.data/logs/`, one per
service). Inspect the effective configuration of any profile: `make doctor`.

Migrations run automatically as part of `make dev` (control-service's
alembic chain, then identity's boot-time migrations); `make db-upgrade` runs
them manually. Platform secrets are auto-generated under `.data/` on first
run and persist across restarts.

Full runbook: [doc/ops/deploy-host.md](doc/ops/deploy-host.md).

### Docker: one machine, every service in a container

```bash
make up                              # bootstraps deploy/.env, builds the images;
                                     # DB_PASSWORD + GW_JWT_SECRET auto-injected
                                     # from .data/ truth files (shared with make dev)
open http://localhost:8080           # frontend (nginx) — API proxied to the gateway
make dagster-up                      # optional: the Dagster plane -> http://localhost:3000
make down
```

The same startup ordering as production, enforced by healthchecks: storage →
one-shot migrate → identity → control/agent/runtime → gateway → frontend.

The P7 cell topology (global tier + one or more cells) is also compose-based:

```bash
make global-up    # identity + global-pg
make cell-up      # a complete cell, joined to the global network
```

Full runbook: [doc/ops/deploy-docker.md](doc/ops/deploy-docker.md).

### Production: Kubernetes

Kustomize under `deploy/k8s/` — `base/` + `overlays/{global,cell,kind}`.
Configuration ships as a ConfigMap generated from the repository's `conf/`
directory (hash-suffixed, so profile edits trigger rolling updates);
migrations run as Jobs before deployments roll.

```bash
make k8s-apply OVERLAY=global   # the global tier (identity) first
make k8s-apply OVERLAY=cell     # then the cell
make k8s-migrate                # migration Jobs, before the deployments serve
```

Full runbook: [doc/ops/deploy-k8s.md](doc/ops/deploy-k8s.md).

### Accounts & Authentication

`identity-service` owns identity: it issues the tokens and is the single writer of the identity
dataset. The gateway verifies those tokens and injects the verified identity into every proxied
request (client-supplied `X-User-ID` / `user_id` values are always overwritten).

- **Sign up** creates an organization (tenant) and makes the registrant its administrator.
- An account may belong to **several tenants** — its own (primary account) plus any it was invited
  to (sub account). When more than one identity exists, the login page asks which one to use, and
  the active identity can be switched at any time from the user menu.
- Roles inside a tenant: `admin` (can manage members) / `developer` / `viewer`.
- Credentials are a short-lived **access token** plus a rotating **refresh token**; the frontend
  refreshes expired sessions transparently.

**Environment variables** (secrets are env-only; the full registry with
symptoms is in [doc/ops/configuration.md](doc/ops/configuration.md)):

| Variable | Used by | Purpose |
|----------|---------|---------|
| `DB_PASSWORD` | all DB-backed services | PostgreSQL password — locally sourced from `.data/postgres_password` (the single truth file) by the dev scripts and make; never set it by hand locally |
| `GW_JWT_SECRET` | gateway + identity | Token signing/verification — one value installation-wide; locally injected from `.data/gateway_jwt_secret`, never set it by hand |
| `IDENTITY_SERVICE_INTERNAL_TOKEN` | gateway + identity + control | Internal authz/bindings API |
| `WORKSPACE_RUNTIME_INTERNAL_TOKEN` | runtime + its callers | Env-control channel, lease placement, code locations |
| `DATACODE_TASK_TOKEN_SECRET` | runtime + egress-proxy | Per-task sandbox egress token |
| `AGENT_SERVICE_LLM_API_KEY` | agent-service | Global fallback LLM key |

The dev scripts load `.env` (your shell environment always wins) and
auto-generate the last four secrets' values under `.data/` (gitignored) on
first run, so they survive restarts. Identity data lives in the shared
PostgreSQL database (resolved through `conf/platform.yaml` +
`DATACODE_ENV`); drop and recreate the database for a full dev reset.

## Project Structure

```
DataCode/
├── gateway/               # API Gateway (Go - Chi): auth, authorization, routing, lease + cell routing
├── identity-service/      # Identity (Go): auth, RBAC, tenant placement                    [global]
├── agent-service/         # Agent Service (Python - openai-agents SDK)
├── control-service/       # Control plane (Python): environment registry, reconciler,
│                          #   workspace metadata, Git credentials, quotas, Alembic chain
├── workspace-runtime/     # Working-copy owner (Python): SQLMesh engine + ontology
│                          #   + Dagster data plane + git + files + task submission
├── task-runner/           # Sandbox executor image (runs user code, one-shot or warm)
├── egress-proxy/          # The only outbound path open to a sandbox (CONNECT + scoped token)
├── shared/                # Shared libraries (Go + Python: platform config, naming, redis,
│                          #   lease contract, error codes, isolation profiles)
├── frontend/              # Frontend (Vue 3 + TypeScript + Vite)
├── deploy/                # Docker Compose, K8s manifests & infrastructure configs
├── conf/                  # Service configuration (platform.yaml is the shared topology)
├── scripts/               # Development & verification scripts
└── doc/                   # Design documents
```

Each Workspace on disk is per-environment, so a working copy holds exactly one environment:

```
{base}/{tenant_id}/{workspace_id}/{user_id}/envs/{environment}/
├── sql-warehouse/         # SQLMesh project (config.yaml, models/, ...)
├── asset-dag/             # Dagster project (definitions.py, defs/, ...)
├── ontology/              # Semantic layer
├── .agent/                # Agent rules, memory, design notes, skills, per-user state
├── logs/                  # Platform runtime logs and the error archive
└── .datacode/             # Platform state: sandbox HOME, rendered projects
```

`.datacode/` and `logs/` are written by the platform, ignored by the generated
`.gitignore`, and hidden from both the file explorer and the agent's file tools.
One module decides that — `shared/python/datacode_common/workspace_layout.py` —
because the three surfaces disagreeing is silent: an artefact simply shows up in
a listing, a search result or a commit.

## Pipeline Orchestration (Dagster)

`workspace-runtime` owns the Dagster data plane: it orchestrates Data+AI pipelines defined in each
Workspace's `asset-dag/` directory and integrates [Daft](https://www.getdaft.io/) for distributed
DataFrame computation. SQLMesh models are bridged into Dagster as source assets, so lineage flows
end-to-end from the SQL warehouse into the Asset DAG.

### Components

| Component | Role | Location |
|-----------|------|----------|
| **Code Location** (gRPC server) | Loads user Python from `asset-dag/definitions.py`, executes runs | Co-located with the working copy (see vehicles below) |
| **DagsterInstance** | Metadata store (runs / events / schedules) | Local SQLite under `dagster.home` (local mode); the remote instance's PostgreSQL (remote mode) |
| **dagster-daemon** | Scheduler / sensors, creates run requests | Local (local mode) or remote cluster |
| **dagster-webserver** | Web UI + GraphQL API | Local (local mode) or remote cluster |

### Code-location vehicles

A code location loads user Python, so it always runs where the working copy is. *How* it runs is
configurable (`dagster.code_location_mode` in `conf/workspace-runtime.yaml`):

- **`process`** (dev / single node) — a child process of `workspace-runtime`, sharing its
  filesystem view and network namespace.
- **`pod`** (production) — one Pod per location on the user-code node pool, with the same security
  context, resource limits and narrowed working-copy mount a task sandbox gets. It requires a
  container isolation profile; the runtime **refuses to start** rather than silently degrading to
  the process vehicle.

### Local vs. Remote mode

Configured via `dagster.mode` in `conf/workspace-runtime.yaml`, or the
`WORKSPACE_RUNTIME_DAGSTER_MODE` environment variable.

- **`local`** (default, zero external dependency): the runtime starts `dagster-daemon`,
  `dagster-webserver` (port `3001`) and the code locations. Metadata is SQLite under
  `~/.datacode/dagster_home`.
- **`remote`**: the runtime starts code locations only; the daemon, webserver and
  PostgreSQL-backed DagsterInstance run on a remote cluster, which PULLS the locations from
  `/internal/code-locations`.

All Dagster REST endpoints are exposed through the gateway under `/api/v1/dagster/*`.

## Development

```bash
make lint      # linters for every service (Go vet + ruff)
make test      # test suites for every service
make clean     # caches and build artifacts
```

## Domain Model

See [doc/design/domain-model.md](doc/design/domain-model.md) for the complete domain model and
ubiquitous language.

### Key Concepts

| Term | Definition |
|------|-----------|
| Project | A SQLMesh data engineering project, mapped to a Git repository |
| Model | A data transformation unit defined in SQL |
| Gateway | Database connection configuration for model execution |
| Workspace | User-level isolated development environment |
| Working copy | One member's checkout of one environment — the unit a lease is held on |
| Environment | SQLMesh virtual environment (dev/staging/prod) |
| Plan | Change plan describing model change impact and execution strategy |
| Lease | The exclusive claim a runtime replica holds on a working copy |
| ChatSession | An AI conversation context with full dialog history |
| Artifact | Code files generated by AI Agent |
| Asset | A Dagster data/ML/feature asset defined in the Workspace `asset-dag/` project |
| Code Location | A gRPC server loading a Workspace's Dagster Python definitions |
| Run | A single execution of a Dagster pipeline/asset job (metadata stored in UTC) |
| Schedule | A cron-based trigger that launches Dagster runs automatically |
| Cell | A self-contained deployment unit holding a subset of tenants |

## License

Private - All rights reserved.
