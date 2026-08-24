# DataCode - AI Native DataOps Platform

An AI-native Data+AI development platform built on **SQLMesh + Daft + Dagster + OpenAI Agents SDK**,
enabling conversational data model development and Data+AI pipeline orchestration through natural language.

## Architecture

```
Frontend (Vue 3) → API Gateway (Go) → Agent Service (Python)
                                     → SQLMesh Service (Python, incl. workspace/git/files)
                                     → Ontology Service (Python)
                                     → Dagster Service (Python, pipeline orchestration + Daft)
```

Each user **Workspace** contains two core project directories plus a semantic layer:

- **`sql-warehouse/`** — SQLMesh project (data modeling / ELT / data quality).
- **`asset-dag/`** — Dagster project (Data+AI pipeline orchestration, Daft distributed DataFrame compute).
- **`ontology/`** — semantic layer.

## Quick Start

### Prerequisites

- Go 1.22+
- Python 3.11+
- Node.js 20+
- Docker & Docker Compose (optional, for `INFRA_MODE=docker`)
- [buf](https://buf.build/docs/installation) (optional, for protobuf generation)

### Setup

**Default (zero Docker):** uses embedded SQLite + DuckDB.

```bash
make dev
```

**With Docker infrastructure** (PostgreSQL + Gitea):

```bash
INFRA_MODE=docker make dev
```

Optional: `make proto` generates gRPC contract code to `gen/` (not required for HTTP dev).

### Accounts & Authentication

The gateway owns authentication: it issues and verifies JWTs, then injects the verified identity
into every proxied request (client-supplied `X-User-ID` / `user_id` values are always overwritten).

- **Sign up** creates an organization (tenant) and makes the registrant its administrator.
- An account may belong to **several tenants** — its own (primary account) plus any it was invited
  to (sub account). When more than one identity exists, the login page asks which one to use, and
  the active identity can be switched at any time from the user menu.
- Roles inside a tenant: `admin` (can manage members) / `developer` / `viewer`.
- Credentials are a short-lived **access token** plus a rotating **refresh token**; the frontend
  refreshes expired sessions transparently.

**Required environment variable**: `GW_JWT_SECRET` — the JWT signing secret. Secrets are env-only
and the gateway refuses to start without it. The dev scripts generate one on first run and store it
in `.data/gateway_jwt_secret` (gitignored). Account data lives in the gateway's own SQLite database
(`auth.db_path` in `conf/gateway.yaml`, default `.data/gateway.db`); delete that file to reset all
accounts during development.

## Project Structure

```
DataCode/
├── proto/                 # Protobuf IDL definitions (contract drafts; HTTP REST in use today)
├── gateway/               # API Gateway (Go - Chi)
├── agent-service/         # Agent Service (Python - openai-agents SDK)
├── sqlmesh-service/       # SQLMesh Service (Python - FastAPI, workspace/git/files)
├── ontology-service/      # Ontology Service (Python - semantic layer)
├── dagster-service/       # Dagster Service (Python - pipeline orchestration + Daft)
├── frontend/              # Frontend (Vue 3 + TypeScript + Vite)
├── deploy/                # Docker Compose & infrastructure configs
├── scripts/               # Development scripts
└── doc/                   # Design documents
```

Each Workspace on disk follows a dual-directory layout:

```
{base}/{user_id}/{workspace}/
├── sql-warehouse/         # SQLMesh project (config.yaml, models/, ...)
├── asset-dag/             # Dagster project (definitions.py, assets/, ...)
└── ontology/              # Semantic layer
```

**Planned (not yet implemented):** `platform/` (tenant/RBAC), `workspace-manager/` (split from sqlmesh-service), `gen/` (generated protobuf), gRPC internal APIs.

## Dagster Service (Pipeline Orchestration)

The Dagster Service (FastAPI, REST port `8004`) orchestrates Data+AI pipelines defined in each
Workspace's `asset-dag/` directory, and integrates [Daft](https://www.getdaft.io/) for distributed
DataFrame computation. It bridges SQLMesh models into Dagster as source assets, so data lineage flows
end-to-end from SQL Warehouse into the Asset DAG.

### Dagster components

| Component | Role | Location |
|-----------|------|----------|
| **Code Location** (gRPC server) | Loads user Python code from `asset-dag/definitions.py`, executes runs | **Always local** (needs Workspace file access) |
| **DagsterInstance** | Metadata store (runs / events / schedules) | SQLite (local) or PostgreSQL (remote) |
| **dagster-daemon** | Scheduler / sensors, creates run requests | Local (local mode) or remote cluster |
| **dagster-webserver** (Dagit) | Web UI + GraphQL API | Local (local mode) or remote cluster |

> **Key design point:** the Code Location gRPC server is *always* run locally by `dagster-service`,
> because it must load user Python code from the Workspace filesystem. Only the DagsterInstance /
> daemon / webserver can be remote.

### Local vs. Remote mode

Configured via `conf/dagster-service.yaml` (`dagster.mode: local | remote`), or the
`DAGSTER_SERVICE_DAGSTER_MODE` environment variable.

- **`local`** (default, zero external dependency): `dagster-service` starts three subprocesses —
  `dagster-daemon`, `dagster-webserver` (port `3001`), and the Code Location gRPC server (port `4000`).
  Metadata is stored in SQLite under `~/.datacode/dagster_home`.
- **`remote`**: `dagster-service` starts only the Code Location gRPC server (still local). The daemon,
  webserver, and PostgreSQL-backed DagsterInstance run on a remote cluster. Set
  `dagster.remote_graphql_url` to the remote webserver GraphQL endpoint; the auth token is read from
  the `DAGSTER_SERVICE_DAGSTER_REMOTE_TOKEN` environment variable (never hard-coded).

```yaml
# conf/dagster-service.yaml (excerpt)
dagster:
  mode: "local"                 # "local" | "remote"
  code_location_port: 4000      # gRPC server for user code — always local
  webserver_port: 3001          # local mode only
  home: "~/.datacode/dagster_home"
  remote_graphql_url: ""        # remote mode only, e.g. http://dagster-prod:3000/graphql
```

All Dagster REST endpoints are exposed through the gateway under `/api/v1/dagster/*`.

## Domain Model

See [doc/design/domain-model.md](doc/design/domain-model.md) for the complete domain model and ubiquitous language.

### Key Concepts

| Term | English | Definition |
|------|---------|-----------|
| Project | Project | A SQLMesh data engineering project, mapped to a Git repository |
| Model | Model | A data transformation unit defined in SQL |
| Gateway | Gateway | Database connection configuration for model execution |
| Workspace | Workspace | User-level isolated development environment |
| Environment | Environment | SQLMesh virtual environment (dev/staging/prod) |
| Plan | Plan | Change plan describing model change impact and execution strategy |
| ChatSession | ChatSession | An AI conversation context with full dialog history |
| Artifact | Artifact | Code files generated by AI Agent |
| Asset | Asset | A Dagster data/ML/feature asset defined in the Workspace `asset-dag/` project |
| Code Location | Code Location | A gRPC server loading a Workspace's Dagster Python definitions (always local) |
| Run | Run | A single execution of a Dagster pipeline/asset job (metadata stored in UTC) |
| Schedule | Schedule | A cron-based trigger that launches Dagster runs automatically |

## License

Private - All rights reserved.
