# Gateway API Reference

> Frontend-facing API documentation. All timestamps are UTC ISO 8601.  
> Base URL: `/api/v1`

## Routing Overview

The gateway authenticates every request, authorizes it against an RBAC
snapshot, and proxies to exactly one upstream per route group:

| Route group | Upstream | Notes |
|-------------|----------|-------|
| `/auth/*`, `/me/*`, `/rbac/*` | identity-service (8003) | Identity, RBAC, tenant placement |
| `/models/*`, `/sessions/*` | agent-service (8001) | Agent chat & models |
| `/workspaces/*`, `/environments/*`, `/git/credentials*`, `/quotas` | control-service (8004) | Metadata plane: registry, workspace metadata, quotas |
| `/sqlmesh/*`, `/files/*`, `/git/*` (panel), `/knowledge/*`, `/ontology/*`, `/dagster/*`, `/wip/*`, `/logs/*` | workspace-runtime (8002) | Working-copy plane, lease-routed to the owning replica |

Working-copy requests (`workspace_path`) are routed to the runtime replica
that holds the workspace's lease — never load-balanced across replicas.

---

## Table of Contents

- [Authentication](#authentication)
- [SQLMesh Operations](#sqlmesh-operations)
  - [Plan Preview](#plan-preview)
  - [Plan Apply](#plan-apply)
  - [Plan Cancel](#plan-cancel)
  - [List Models](#list-models)
  - [Get Model Detail](#get-model-detail)
  - [Render Model](#render-model)
  - [Validate Models](#validate-models)
  - [Evaluate Model](#evaluate-model)
  - [Execute Query](#execute-query)
  - [Get Lineage](#get-lineage)
  - [Get Column Lineage](#get-column-lineage)
  - [List Environments](#list-environments)
  - [Delete Environment](#delete-environment)
  - [Run Tests](#run-tests)
  - [Table Diff](#table-diff)
  - [Schema Discovery](#schema-discovery)
- [Workspace Management](#workspace-management)
- [Git Operations](#git-operations)
- [File Operations](#file-operations)
- [Asset Deployments and Reload](#asset-deployments-and-reload)
- [Workspace Logs](#workspace-logs)
- [Quotas](#quotas)
- [Error Codes](#error-codes)

---

## Authentication

Requests authenticate with a JWT access token in the `Authorization` header:

```
Authorization: Bearer <access_token>
```

The gateway verifies the token (issued by identity-service), then injects the
verified identity into every proxied request. Client-supplied `X-User-ID`
headers and `user_id` body fields are always **overwritten** — they cannot be
forged.

### Auth Endpoints

Proxied to identity-service; these are the only unauthenticated routes.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/register` | Create an account + tenant; registrant becomes admin |
| POST | `/auth/login` | Exchange credentials for access + refresh tokens |
| POST | `/auth/refresh` | Rotate the refresh token, issue a new access token |
| POST | `/auth/logout` | Revoke the refresh token |

An account may hold several tenant identities (its own plus invited ones);
login with more than one asks which to use via a short-lived pre-auth token.

### Current User

| Method | Path | Description |
|--------|------|-------------|
| GET | `/me` | Verified identity payload (tenant, role, display name) |
| GET/PUT | `/me/preferences` | Per-user UI preferences |

Roles inside a tenant: `admin` (manages members) / `developer` / `viewer`.

---

## SQLMesh Operations

All SQLMesh operations are proxied to workspace-runtime via `/api/v1/sqlmesh/*`.

### Plan Preview

Create a plan preview without applying. Returns change details and a state fingerprint for optimistic locking.

```
POST /api/v1/sqlmesh/plan
```

**Request Body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `workspace_path` | string | ✓ | - | Target workspace path |
| `environment` | string | | `"dev"` | Target environment name |
| `gateway` | string | | `null` | Gateway override |
| `plan_dates` | object | | `null` | Date range for the plan |
| `plan_dates.start` | string | | `null` | Plan start date/time |
| `plan_dates.end` | string | | `null` | Plan end date/time |
| `plan_options` | object | | `null` | Plan options |
| `plan_options.skip_tests` | bool | | `false` | Skip tests |
| `plan_options.skip_backfill` | bool | | `false` | Skip backfill |
| `plan_options.no_gaps` | bool | | `false` | Check for gaps |
| `plan_options.forward_only` | bool | | `false` | Forward-only plan |
| `plan_options.no_auto_categorization` | bool | | `false` | Disable auto categorization |
| `plan_options.include_unmodified` | bool | | `false` | Include unmodified models |
| `plan_options.create_from` | string | | `null` | Create from environment |
| `plan_options.restate_models` | string[] | | `null` | Models to restate |

**Response:**

```json
{
  "environment": "dev",
  "has_changes": true,
  "requires_backfill": true,
  "changes": {
    "added": [
      {"name": "model_name", "view_name": "catalog.schema.model_name", "node_type": "model"}
    ],
    "removed": [
      {"name": "old_model", "view_name": "catalog.schema.old_model", "node_type": "model"}
    ],
    "modified": {
      "direct": [
        {
          "name": "changed_model",
          "view_name": "catalog.schema.changed_model",
          "node_type": "model",
          "diff": "@@ -1,5 +1,5 @@ ...",
          "change_category": "breaking",
          "indirect": [],
          "direct": []
        }
      ],
      "indirect": [
        {"name": "downstream_model", "view_name": "...", "node_type": "model"}
      ],
      "metadata": []
    }
  },
  "backfills": [
    {
      "name": "changed_model",
      "view_name": "catalog.schema.changed_model",
      "interval": ["2024-01-01 -> 2024-01-02"],
      "batches": 1
    }
  ],
  "state_fingerprint": "abc123def456",
  "start": "2024-01-01",
  "end": "2024-12-31"
}
```

---

### Plan Apply

Create and apply a plan. Supports optimistic locking via `expected_fingerprint`.

```
POST /api/v1/sqlmesh/apply
```

**Request Body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `workspace_path` | string | ✓ | - | Target workspace path |
| `environment` | string | | `"dev"` | Target environment name |
| `gateway` | string | | `null` | Gateway override |
| `plan_dates` | object | | `null` | Same as plan preview |
| `plan_options` | object | | `null` | Same as plan preview |
| `categories` | object | | `null` | Model name → change category mapping |
| `expected_fingerprint` | string | | `null` | State fingerprint from plan preview |

**`categories` example:** `{"my_model": "breaking", "other_model": "non_breaking"}`

**Response:**

```json
{
  "applied": true,
  "has_changes": true,
  "requires_backfill": true,
  "environment": "dev"
}
```

**Conflict (409):** Returned when `expected_fingerprint` doesn't match current workspace state.

---

### Plan Cancel

Cancel a running plan application.

```
POST /api/v1/sqlmesh/cancel
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `workspace_path` | string | ✓ | Target workspace path |
| `gateway` | string | | Gateway override |

**Response:**

```json
{
  "cancelled": true,
  "message": "Cancellation signal sent"
}
```

---

### List Models

List all models in a workspace.

```
GET /api/v1/sqlmesh/models?workspace_path=...&gateway=...
```

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `workspace_path` | string | ✓ | Target workspace path |
| `gateway` | string | | Gateway override |

**Response:**

```json
{
  "models": [
    {
      "name": "model_name",
      "fqn": "catalog.schema.model_name",
      "path": "models/model_name.sql",
      "dialect": "duckdb",
      "type": "sql",
      "kind": "INCREMENTAL_BY_TIME_RANGE",
      "description": "Model description",
      "owner": "data-team",
      "tags": ["important"],
      "cron": "@daily"
    }
  ]
}
```

---

### Get Model Detail

Get detailed info for a single model including columns, details, and rendered SQL.

```
GET /api/v1/sqlmesh/models/detail?workspace_path=...&model_name=...&gateway=...
```

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `workspace_path` | string | ✓ | Target workspace path |
| `model_name` | string | ✓ | Model FQN or name |
| `gateway` | string | | Gateway override |

**Response:**

```json
{
  "name": "model_name",
  "fqn": "catalog.schema.model_name",
  "path": "models/model_name.sql",
  "full_path": "/workspace/models/model_name.sql",
  "dialect": "duckdb",
  "type": "sql",
  "columns": [
    {"name": "id", "type": "INT", "description": "Primary key"},
    {"name": "created_at", "type": "TIMESTAMP", "description": null}
  ],
  "description": "Model description",
  "details": {
    "owner": "data-team",
    "kind": "INCREMENTAL_BY_TIME_RANGE",
    "cron": "@daily",
    "start": "2024-01-01",
    "time_column": "created_at | %Y-%m-%d %H:%M:%S",
    "partitioned_by": "created_at",
    "cron_prev": "2024-01-14 00:00:00",
    "cron_next": "2024-01-15 00:00:00"
  },
  "sql": "SELECT id, created_at FROM source_table",
  "default_catalog": "my_catalog",
  "hash": "abc123"
}
```

---

### Render Model

Render a model's SQL with macro expansion. Supports expanding referenced models.

```
POST /api/v1/sqlmesh/render
```

**Request Body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `workspace_path` | string | ✓ | - | Target workspace path |
| `model_name` | string | ✓ | - | Model name |
| `gateway` | string | | `null` | Gateway override |
| `start` | string | | `null` | Start time |
| `end` | string | | `null` | End time |
| `execution_time` | string | | `null` | Execution time |
| `expand` | bool \| string[] | | `false` | Expand referenced models |
| `pretty` | bool | | `true` | Pretty print SQL |
| `dialect` | string | | `null` | Override dialect |

**Response:**

```json
{
  "sql": "SELECT id, name FROM expanded_source_table WHERE dt BETWEEN '2024-01-01' AND '2024-12-31'",
  "dialect": "duckdb"
}
```

---

### Validate Models

Validate all model files in the workspace by reloading the context.

```
POST /api/v1/sqlmesh/validate
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `workspace_path` | string | ✓ | Target workspace path |
| `gateway` | string | | Gateway override |

**Response:**

```json
{
  "valid": true,
  "errors": [],
  "warnings": []
}
```

---

### Evaluate Model

Evaluate a model and return result rows.

```
POST /api/v1/sqlmesh/evaluate
```

**Request Body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `workspace_path` | string | ✓ | - | Target workspace path |
| `model` | string | ✓ | - | Model name |
| `gateway` | string | | `null` | Gateway override |
| `start` | string | | `null` | Start time |
| `end` | string | | `null` | End time |
| `execution_time` | string | | `null` | Execution time |
| `limit` | int | | `1000` | Max rows |

**Response:**

```json
{
  "columns": ["id", "name", "value"],
  "rows": [[1, "foo", 100], [2, "bar", 200]],
  "total_rows": 2,
  "execution_time_ms": 150
}
```

---

### Execute Query

Execute an ad-hoc SQL query with safe LIMIT enforcement.

```
POST /api/v1/sqlmesh/query
```

**Request Body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `workspace_path` | string | ✓ | - | Target workspace path |
| `sql` | string | ✓ | - | SQL query |
| `gateway` | string | | `null` | Gateway override |
| `row_limit` | int | | `0` | Max rows (0 = use default) |

**Response:** Same as Evaluate Model.

---

### Get Lineage

Get model dependency DAG.

```
GET /api/v1/sqlmesh/lineage?workspace_path=...&model_name=...&gateway=...
```

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `workspace_path` | string | ✓ | Target workspace path |
| `model_name` | string | | Filter to specific model |
| `gateway` | string | | Gateway override |

**Response:**

```json
{
  "graph": {
    "catalog.schema.model_a": ["catalog.schema.model_b", "catalog.schema.model_c"],
    "catalog.schema.model_b": ["catalog.schema.model_d"]
  }
}
```

---

### Get Column Lineage

Get column-level lineage as adjacency list.

```
GET /api/v1/sqlmesh/lineage/column?workspace_path=...&model_name=...&column_name=...&models_only=false&gateway=...
```

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `workspace_path` | string | ✓ | Target workspace path |
| `model_name` | string | ✓ | Model name |
| `column_name` | string | ✓ | Column name |
| `models_only` | bool | | Only model-level deps (default: false) |
| `gateway` | string | | Gateway override |

**Response:**

```json
{
  "lineage": {
    "catalog.schema.model_a": {
      "total_amount": {
        "source": "SELECT SUM(amount) AS total_amount FROM orders",
        "expression": "SUM(amount)",
        "models": {
          "catalog.schema.orders": {"amount"}
        }
      }
    }
  }
}
```

---

### List Environments (Registry)

List a workspace's environments. The registry is the source of truth: the list is
identical for every member, and each entry carries the lifecycle metadata the
SQLMesh engine does not know about (type, status, TTL, last applied commit).

```
GET /api/v1/workspaces/{workspace_id}/environments?include_archived=false
```

Requires `workspace:env:read`.

**Response:**

```json
{
  "workspace_id": "ws-t1-sales",
  "environments": [
    {
      "environment_id": "ws-t1-sales:prod",
      "name": "prod",
      "branch": "main",
      "env_type": "production",
      "base_env": "prod",
      "status": "active",
      "deployment_name": "ws_t1_sales__prod",
      "schedules_enabled": true,
      "is_production": true,
      "last_applied_commit": "a1b2c3d",
      "last_applied_at": "2026-08-17T09:12:00+00:00",
      "ttl_hours": 0,
      "expires_at": "",
      "created_at": "2026-07-01T00:00:00+00:00",
      "updated_at": "2026-08-17T09:12:00+00:00"
    }
  ]
}
```

All timestamps are UTC ISO 8601; the frontend renders them in the user's timezone.

---

### Create Environment

Provision a development line: registry row, Git branch, worktree and SQLMesh
virtual environment, as one unit.

```
POST /api/v1/workspaces/{workspace_id}/environments
{ "name": "feature_orders", "env_type": "shared", "base_env": "prod", "ttl_hours": 168 }
```

Requires `workspace:env:create`. Publishing the branch additionally needs
`workspace:git:push`; when the push fails the environment stays usable locally
and its `status` reports it.

The response carries `branch_source`, which says where the code came from:

| Value | Meaning |
|-------|---------|
| `created_from_base` | A new branch was cut from `base_env`. |
| `reused_remote` | The remote already had `env/{name}` — usually because a previous environment of this name was destroyed (destroy preserves the branch) — so this environment RESUMES that line of development and the working copy holds its code, not the base's. |
| `adopted_local` | A leftover local branch held commits that were never pushed, so it was kept as-is rather than realigned to the remote. |

`branch_source` describes this creation only and is deliberately absent from the
environment list: replaying it later would report a past decision as current
state.

---

### Open Environment

Materialize the caller's worktree and return the `workspace_path` every other
endpoint expects. Idempotent.

```
POST /api/v1/environments/{environment_id}/open
```

Requires `workspace:read`. `environment_id` is `{workspace_id}:{environment}`.

**Response:**

```json
{
  "environment_id": "ws-t1-sales:dev_alice",
  "environment": "dev_alice",
  "branch": "env/dev_alice",
  "workspace_path": "/data/workspaces/t1/ws-t1-sales/u1/envs/dev_alice",
  "head_commit": "a1b2c3d",
  "materialized": true
}
```

---

### Pending Work (pre-flight for deletion)

What destroying an environment would lose. Read-only, takes no environment
lock, and has no side effects — call it BEFORE showing a delete confirmation,
while the user can still push.

```
GET /api/v1/environments/{environment_id}/pending-work
```

Requires `workspace:env:read`.

**Response:**

```json
{
  "environment_id": "ws-t1-sales:dev_alice",
  "branch": "env/dev_alice",
  "uncommitted": 0,
  "unpushed": 3,
  "members": [{ "user_id": "u1", "uncommitted": 0, "unpushed": 3 }]
}
```

`unpushed` is the number that matters: the remote branch survives a destroy, so
committed-but-unpushed commits are the only code that can still disappear. It
is a warning, never a precondition — the deletion stays available either way.
`uncommitted` is returned too because that is what the DELETE below refuses
over, letting one dialog explain both outcomes.

Members with nothing pending are omitted. A failure here must not block a
deletion: it only enriches the confirmation.

---

### Destroy Environment

Release the state the PLATFORM owns: SQLMesh views, every member's working
copy, the local branch and the Dagster deployment. The registry row and its
events are kept for audit. Production is rejected.

```
DELETE /api/v1/environments/{environment_id}?force=false
```

Requires `workspace:env:delete`.

**The remote branch is never deleted.** The remote repository belongs to the
user — it may predate the workspace, be shared with tooling outside DataCode,
and hold the only durable copy of the code. Preserving it is also what lets a
same-named environment resume the same line of development later (see
`branch_source` above). There is no parameter to opt out, and the platform
contains no `git push --delete` at all.

`force=true` discards **uncommitted** changes in the working copies; without it
a dirty copy answers `409` and names how many changes would be lost. Unpushed
commits never block the request — they are reported by `pending-work` before
the user confirms, and recorded afterwards in the destroy event's
`unpushed_commits_discarded`.

**Response:**

```json
{
  "success": true,
  "environment_id": "ws-t1-sales:dev_alice",
  "archived": true,
  "branch": "env/dev_alice",
  "remote_branch_preserved": true,
  "virtual_environment_released": true,
  "worktrees_removed": 2,
  "unpushed_commits_discarded": 0
}
```

A skipped release (no local copy anywhere, an unreachable runtime) is reported
honestly as `false` / `0` rather than masquerading as a full cleanup; the
reconciler finishes it later.

---

### Environment Events

```
GET /api/v1/environments/{environment_id}/events?limit=50
```

Requires `workspace:env:read`. Lifecycle timeline, newest first.

---

### List Engine Environments (troubleshooting)

The environments that actually exist in the SQLMesh state. Used by the reconciler
and for troubleshooting only — it does not carry platform metadata, so the UI
reads the registry instead.

```
GET /api/v1/sqlmesh/engine/environments?workspace_path=...&gateway=...
```

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `workspace_path` | string | ✓ | Target working copy |
| `gateway` | string | | Gateway override |

---

### Run Tests

Run one or all model tests.

```
GET /api/v1/sqlmesh/test?workspace_path=...&test=...&verbosity=...&gateway=...
```

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `workspace_path` | string | ✓ | Target workspace path |
| `test` | string | | Specific test path (empty = all) |
| `verbosity` | string | | `default`, `verbose`, or `quiet` |
| `gateway` | string | | Gateway override |

**Response:**

```json
{
  "tests_run": 5,
  "errors": [],
  "failures": [
    {"name": "test_orders", "path": "tests/test_orders.yaml", "tb": "AssertionError: ..."}
  ],
  "skipped": [],
  "successes": [
    {"name": "test_customers", "path": "tests/test_customers.yaml"}
  ]
}
```

---

### Table Diff

Calculate differences between two tables (schema + row level).

```
GET /api/v1/sqlmesh/table-diff?workspace_path=...&source=...&target=...&on=...&gateway=...
```

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `workspace_path` | string | ✓ | Target workspace path |
| `source` | string | ✓ | Source table |
| `target` | string | ✓ | Target table |
| `on` | string | | Join condition |
| `model_or_snapshot` | string | | Filter to specific model |
| `where` | string | | WHERE clause |
| `temp_schema` | string | | Temp schema for diff |
| `limit` | int | | Sample limit (default: 20) |
| `gateway` | string | | Gateway override |

**Response:**

```json
{
  "schema_diff": {
    "source": "prod.model_a",
    "target": "dev.model_a",
    "source_schema": {"id": "INT", "name": "VARCHAR"},
    "target_schema": {"id": "INT", "name": "VARCHAR", "email": "VARCHAR"},
    "added": {"email": "VARCHAR"},
    "removed": {},
    "modified": {}
  },
  "row_diff": {
    "source_count": 1000,
    "target_count": 1050,
    "count_pct_change": 5.0,
    "stats": {}
  },
  "on": [["id", "id"]]
}
```

---

### Schema Discovery

Discover table schemas from the data source.

```
POST /api/v1/sqlmesh/schema/discover
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `workspace_path` | string | ✓ | Target workspace path |
| `gateway` | string | | Gateway override |
| `schema_name` | string | | Filter by schema name |
| `table_name` | string | | Filter by table name |

**Response:**

```json
{
  "tables": [
    {
      "schema": "my_schema",
      "table_name": "my_table",
      "columns": [
        {"name": "id", "data_type": "INT"},
        {"name": "name", "data_type": "VARCHAR(255)"}
      ]
    }
  ]
}
```

---

## Workspace Management

Workspace metadata and Git credentials are owned by control-service; creating a
workspace also records the owner role binding in identity-service (a failure
rolls the workspace back so nothing is left nobody may open).

### Create Workspace

```
POST /api/v1/workspaces
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | ✓ | - | Workspace name |
| `dialect` | string | | `"duckdb"` | SQL dialect |
| `user_id` | string | | `""` | User ID |
| `git_repo_url` | string | | `""` | Git remote to bind (see below) |

`git_repo_url` is an INTENT, resolved before anything is written:

| Remote state | Outcome |
|---|---|
| does not exist | created as a **private** repository at exactly the owner in the URL, then seeded with the workspace template |
| exists, has branches | its code is **reused as-is**; no template is written and nothing is pushed |
| exists, no branches | reused as an **empty** working copy; no template is written |
| owner is an org the token may not create in | `422 GIT_REMOTE_ACCESS_DENIED` with `git_error_reason: repo_creation_denied` — never silently redirected to the token's own account |

Failures carry the remote's own wording (`git_detail`), so "repository not found"
reaches the user instead of a generic Git error.

### List Workspaces

```
GET /api/v1/workspaces?user_id=...
```

Only workspaces the caller is a member of; non-members get 404 (anti-enumeration).

### Bind Git Repo

```
POST /api/v1/workspaces/bind-git
```

### Sync Workspace

```
POST /api/v1/workspaces/sync
```

### Open Workspace

```
POST /api/v1/workspaces/{workspaceID}/open
```

Body carries `environment` (not `branch`): materializes the caller's worktree
for that environment on demand. Idempotent.

### Members

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/workspaces/{workspaceID}/members` | List members (reads the authz snapshot, no extra round trip) |
| POST | `/api/v1/workspaces/{workspaceID}/members` | Add a member (owner-only; writes via identity-service bindings) |
| DELETE | `/api/v1/workspaces/{workspaceID}/members/{userID}` | Remove a member (owner-only) |

### Remote Branches

```
GET /api/v1/workspaces/{workspaceID}/remote-branches
```

### Delete Workspace

```
DELETE /api/v1/workspaces/{workspaceID}
```

Takes no options. Deletion is complete and physical: leases are released first
(a holder is a live writer of the working copy), then environment resources
(SQLMesh virtual environments, working copies, Dagster deployments), then the
runtime purge — broadcast to every replica that held a lease, because each has
its own volume — then the environment and workspace rows.

The **remote Git repository is always preserved**, including its branches: it may
predate the workspace or be shared. Audit rows (`environment_events`,
`lease_history`, `task_records`) are kept by design.

Response reports what was actually released:

```json
{
  "success": true,
  "workspace_id": "ws-t1-shop",
  "leases_released": 2,
  "environments_released": 3,
  "environments_removed": 3,
  "replicas_purged": 2,
  "git_remote_preserved": true
}
```

---

## Git Operations

### Credentials

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/git/credentials?user_id=...` | List credentials |
| POST | `/api/v1/git/credentials` | Create credential |
| PUT | `/api/v1/git/credentials/{credentialID}` | Update credential |
| DELETE | `/api/v1/git/credentials/{credentialID}?user_id=...` | Delete credential |
| PATCH | `/api/v1/git/credentials/{credentialID}/default?user_id=...` | Set default |

### Git Panel

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/git/status?workspace_path=...` | Git status |
| GET | `/api/v1/git/init-status?workspace_path=...` | Check git init |
| POST | `/api/v1/git/init` | Initialize git |
| POST | `/api/v1/git/stage` | Stage files |
| POST | `/api/v1/git/unstage` | Unstage files |
| POST | `/api/v1/git/commit` | Commit changes |
| POST | `/api/v1/git/push` | Push to remote |
| POST | `/api/v1/git/pull` | Pull from remote (invalidates the metadata cache) |
| GET | `/api/v1/git/branches?workspace_path=...` | List branches (read-only; each entry carries `environment` + `managed`) |
| GET | `/api/v1/git/log?workspace_path=...&branch=...&limit=50` | Commit history |
| GET | `/api/v1/git/commit-files?workspace_path=...&commit_hash=...` | Files in commit |
| GET | `/api/v1/git/commit-file-diff?workspace_path=...&commit_hash=...&file_path=...` | File diff |
| POST | `/api/v1/git/publish` | Full publish flow |

---

## File Operations

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/files/list?workspace_path=...&directory=...&recursive=false` | List files |
| GET | `/api/v1/files/read?workspace_path=...&file_path=...` | Read file |
| POST | `/api/v1/files/write` | Write file (invalidates the metadata cache) |
| POST | `/api/v1/files/delete` | Delete file (invalidates the metadata cache) |
| POST | `/api/v1/files/mkdir` | Create directory |
| POST | `/api/v1/files/rename` | Rename/move file (invalidates the metadata cache) |

---

## Asset Deployments and Reload

A **deployment** is one environment's Dagster code location. Bringing it up is
`ensure`; making a code change live is `reload`.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/dagster/deployments/ensure` | Idempotently start the code location; reports dependency state |
| POST | `/api/v1/dagster/deployments/reload` | **202** — start (or join) a reload job |
| GET | `/api/v1/dagster/deployments/reload/status?workspace_path=...&job_id=...` | Authoritative job state |
| POST | `/api/v1/dagster/deployments/reload/cancel` | Withdraw the reload in flight |

### Reload is a job, not a request

A reload installs the working copy's declared Python packages
(`asset-dag/pyproject.toml`, `[project].dependencies`) **before** starting the
code location, so it can take minutes on a first install. `POST /reload`
therefore answers **202** with a job envelope and the work continues in the
background:

```json
{
  "job_id": "9f2c1d0a4b3e5f67",
  "workspace_path": "/data/workspaces/t1/ws-t1-shop/u1/envs/dev",
  "deployment_name": "ws_t1_shop__dev",
  "state": "running",
  "stage": "installing_deps",
  "stages": ["queued", "resolving_deps", "installing_deps",
             "starting_code_location", "loading_definitions", "ready"],
  "message": "installing 3 declared package(s)",
  "attached": false,
  "output_tail": "",
  "failure_code": "",
  "error_log_id": "",
  "deps_state": "drifted",
  "deployment": null
}
```

Contract points:

* **`workspace_path` is required on the status query.** The runtime is a headless
  Service and a job record lives in the working copy, so only the replica holding
  that copy's lease can answer. Without it the gateway answers **400** rather than
  routing to an arbitrary replica that would report "no such job".
* **A second trigger attaches.** Three buttons in the UI and the CLI express one
  intent, so a request arriving while a reload runs returns the SAME `job_id` with
  `attached: true`. A **409** `ENVIRONMENT_BUSY` is reserved for a real conflict —
  another operation holding the environment lock.
* **Progress is also published** on the SSE `deployment` topic of
  `GET /api/v1/events` (same payload shape, plus `line` for one new installer
  output line). The status endpoint remains the authoritative truth: a client may
  poll it exclusively and lose nothing.
* **A terminal state survives** a dropped connection, a page refresh and a runtime
  restart — records are written to `{workspace}/.datacode/deploy/reload-jobs/`.
* **A failed install fails the reload.** The code location is never started with
  its declared packages missing, and there is no automatic retry: a reload is a
  user action, and an unattended retry would hide the failure.

Failure reasons (`failure_code`), each with its own next action:

| Code | Meaning |
|------|---------|
| `deps_declaration_invalid` | `asset-dag/pyproject.toml` cannot be parsed — fix the syntax |
| `deps_platform_conflict` | a declared package is pinned to a version the platform does not ship. The declaration is valid; it just cannot be installed, because a workspace overlay shadows the platform's copy instead of coexisting with it. The envelope's `conflicts[]` carries `{requirement, name, platform_version, suggested}` per offending line |
| `deps_platform_transitive` | a declared package is compatible with the baseline but one of ITS dependencies is not (`feast` needs `tenacity<9`, the platform ships 9.1.4). No check of the declaration can see it, so it is only known after a resolve. Reported only for a pin the platform **cannot yield** — see *Yielding a platform pin* — so the envelope also names it. `transitive_conflicts[]` carries `{requirement, name, blockers[{name, required, platform_version, required_by}], suggested, resolved_version, protected[]}`; `required` is the version RANGE a dependency states and `required_by` the dependency stating it, neither of which appears in the user's file. `suggested` is empty when no release fits, and `protected[]` then names the pin only an operator can move |
| `deps_install_failed` | pip could not install the declared packages; `output_tail` carries its output |
| `deps_no_wheel` | the index HAS a version the declaration accepts, but it publishes no wheel this interpreter can install (only a source distribution, or wheels for other platforms). Installs are wheel-only: the sandbox has no compiler and never executes a package's build script. Diagnosed by asking the index directly (`pip index versions`), because pip reports a filtered-out candidate set as `(from versions: none)` — indistinguishable from "the version does not exist". No version bound fixes it: use a package that ships wheels, add it to the runtime image (`--build-arg RUNTIME_EXTRAS=...`), or, per package, allow source builds with `deps.allow_source_packages` |
| `deps_index_not_configured` | the deployment has no package index configured (`deps.index_url`) |
| `deps_shadow_platform` | the installed tree contains a package the platform provides. No declaration can cause this — it means the runtime and task-runner images are out of step |
| `deps_too_large` | the installed tree exceeds `deps.max_bytes` for one working copy |
| `code_load_failed` | the location started but could not import the definitions; `output_tail` is the traceback |
| `sandbox_unavailable` | no sandbox could be started; nothing was changed |
| `internal_error` | unexpected failure |

Every failure is also archived to `{workspace}/logs/errors/` and the envelope's
`error_log_id` names that record, so an agent can diagnose it with ordinary file
tools (see *Workspace Logs* below).

### Dependencies

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/v1/deps/baseline` | What a code location can import with no overlay at all |
| GET | `/api/v1/deps/status` | One working copy's dependency state, incl. platform conflicts |

**`/deps/baseline` takes no `workspace_path`** — deliberately. The baseline is a
property of the runtime image, identical for every working copy, so requiring
one would make the answer look workspace-specific and would send the request
through lease routing for nothing. Authenticated access is enough, matching
`/sqlmesh/meta` and `/dagster/mode`.

Each entry is `{name, version, suggested}`, where `suggested` is the requirement
string that accepts the shipped version (a lower bound, never a pin — a pin
would conflict again at the next image upgrade). `highlighted[]` is the subset
user code commonly imports; the full list runs to several hundred distributions.

Two properties worth stating, because both were defects before:

* **The baseline is the interpreter code locations run on**, not the one that
  performs the install. Those are different images in production and the same
  process in development, so getting it wrong is invisible until deployment.
* **A conflict is reported by the READ path**, so `/deps/status` and the
  `ensure` payload both carry `conflicts[]`. A reload that cannot possibly
  succeed is visible before the user spends minutes discovering it — and saving
  `asset-dag/pyproject.toml` through `POST /files/write` returns the same verdict
  as a `deps` field on the response, without ever refusing the save.

The read path compares the declaration against the baseline, so it can only see
conflicts the declaration names. **A declared package whose own dependencies
clash with the baseline is not visible there** — `feast` is compatible with the
platform, its dependency `tenacity<9` is not. That shape is diagnosed by the
installer and reaches the client as `transitive_conflicts[]` on a failed reload
envelope, never from `/deps/status`.

### Yielding a platform pin

Pinning all several hundred baseline distributions gives each of them a veto
over every declaration, and the cost of being overridden is not the same for
all of them:

* a **compiled** distribution (`numpy`, `pyarrow`, `pydantic-core`, `grpcio`) or
  one the platform's own machinery is built on (`dagster`, `sqlmesh`) breaks the
  platform at ABI level, inside a code location, with the traceback pointing at
  the user's asset;
* a **pure-Python leaf** (`tenacity`, `tqdm`) carries API-drift risk only, and
  only inside the one working copy that asked for it.

So the runtime derives the yieldable subset — everything with no compiled
extension, minus the platform's own runtime, adjusted by
`deps.protected_packages` / `deps.relaxable_packages` — and the installer spends
it as follows:

1. install with the FULL baseline pinned. This is the normal outcome and leaves
   the workspace identical to the platform;
2. if pip refused to RESOLVE and every pin it named as a party to the conflict
   is yieldable, release **those** pins and retry **once**;
3. otherwise fail with `deps_platform_transitive`, naming the pin that cannot
   move in `protected[]`.

A successful install that yielded is not silent. `relaxed[]` —
`{name, platform_version, overlay_version, required_by}` — appears on the reload
envelope (published as soon as pip finishes) AND on `/deps/status` and the
`ensure` payload, because the override outlives the install: from then on that
copy's code location and sandboxes import a different version than the
platform's own processes do.

### `ensure` never installs

`ensure` is on the asset page's load path, so it stays a read: it reports the
dependency state and leaves the install to a reload the user triggers.

```json
{ "deps": { "state": "ready", "declared": 3, "installed": 41,
            "message": "", "installed_at": "2026-09-11T09:12:00+00:00",
            "size_bytes": 41234567,
            "relaxed": [{ "name": "tenacity", "platform_version": "9.1.4",
                          "overlay_version": "8.5.0", "required_by": "feast" }] } }
```

`state` is one of `ready`, `drifted`, `missing`, `incompatible`, `failed`,
`empty`, `declaration_invalid`.

---

## Workspace Logs

Services mirror every workspace-attributable log line as NDJSON into
`{workspace}/logs/service/`, so these endpoints read files rather than a log
store. `workspace_path` is required on all of them: a log query with no
resolvable workspace is rejected, never widened.

Authorization is `workspace:logs:read`, separate from `workspace:code:read`
because a log line may quote SQL, data values or tool output.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/logs?workspace_path=...&sources=...&levels=...&q=...&since=...&limit=200&cursor=...` | Newest-first page of log entries, merged across sources |
| GET | `/api/v1/logs/stream?workspace_path=...&sources=...&levels=...&q=...` | SSE tail of lines appended after connecting |
| GET | `/api/v1/logs/errors?workspace_path=...&limit=50` | Archived error summaries, newest first |
| GET | `/api/v1/logs/errors/{error_id}?workspace_path=...` | One archived error with traceback and context |

Besides `logs/service/` (the NDJSON timeline) and `logs/errors/` (the archive), a
working copy also holds `logs/codeloc/server.log` — the raw stdout/stderr of its
code-location process. That file is plain text on purpose: it is the user's own
code talking, and its tail is the import traceback a failed reload reports.

`sources` and `levels` are comma-separated. A source is a file origin such as
`workspace-runtime`, `control-service` or `sqlmesh-cli` (SQLMesh's own text logs,
exposed read-only). Paginate by passing the previous response's `next_cursor`.

An entry has the shape:

```json
{
  "id": "workspace-runtime:workspace-runtime.2026-08-21.ndjson:42",
  "ts": "2026-08-21T09:30:12.123Z",
  "level": "error",
  "source": "workspace-runtime",
  "service": "workspace-runtime",
  "event": "git_remote_failed",
  "trace_id": "b3f1",
  "environment": "dev",
  "error_code": "GIT_REMOTE_ACCESS_DENIED",
  "error_log_id": "ab12-...",
  "fields": { "git_error_reason": "unknown", "host": "github.com" }
}
```

`error_log_id` links a timeline entry to its full archive record; `trace_id` is
issued by the gateway and forwarded downstream, so one user action can be
followed across services.

---

## Quotas

```
GET /api/v1/quotas
```

The caller's tenant limits and current usage (workspaces, environments,
concurrent leases). `current` is `null` (not 0) when usage cannot be read —
symmetric with the fail-open enforcement. Exceeding a limit answers **429**
`QUOTA_EXCEEDED` with `Retry-After` and `quota_resource`/`limit`/`current`
fields in the body.

---

## Error Codes

All errors follow this structure:

```json
{
  "code": "PLAN_FAILED",
  "message": "Failed to create plan preview",
  "detail": "Connection refused..."
}
```

### Domain codes

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `WORKSPACE_NOT_FOUND` | 404 | Workspace path does not exist |
| `WORKSPACE_PATH_INVALID` | 400 | Path outside allowed base or traversal attempt |
| `MODEL_NOT_FOUND` | 404 | Model name not found in context |
| `PLAN_FAILED` | 500 | Plan creation failed |
| `APPLY_FAILED` | 500 | Plan apply failed |
| `CONFLICT` | 409 | Workspace modified since plan preview |
| `QUERY_FAILED` | 500 | SQL query execution failed |
| `INTERNAL_ERROR` | 500 | Unexpected server error |

### Platform contract codes (design 08 §7)

Frozen platform-wide codes; every producer/consumer pair agrees on the status.

| Code | HTTP Status | Retry? | Description |
|------|-------------|--------|-------------|
| `LEASE_NOT_HELD` | 409 | No | Working copy is not held by this replica (stale route) |
| `LEASE_UNAVAILABLE` | 429 | Backoff | All capacity in use; honour `Retry-After` |
| `LEASE_SPLIT_BRAIN` | 409 | No | Two writers detected for one working copy |
| `WORKSPACE_MATERIALIZING` | 202 | Yes | Working copy is being materialized; poll again |
| `WORKSPACE_RELOCATING` | 503 | Yes | Working copy is moving to another replica; honour `Retry-After` |
| `QUOTA_EXCEEDED` | 409 / 429 | Caller decides | Counts → 409, rates → 429; body carries limit and current |
| `SANDBOX_UNAVAILABLE` | 503 | Yes | Sandbox could not be started |
| `SANDBOX_TIMEOUT` | 504 | No | Task exceeded its deadline |
| `SANDBOX_OOM` | 507 | No | Task killed by its memory limit |
| `EGRESS_DENIED` | 403 | No | Egress outside the task's allowlist |
| `CELL_MAINTENANCE` | 503 | Yes | The tenant's cell is in maintenance |
| `INSUFFICIENT_STORAGE` | 507 | No | Disk quota exhausted |
| `STATE_SCHEMA_MISMATCH` | 409 | No | Gateway config disagrees with the platform's state schema; repair endpoint offered |
| `ENVIRONMENT_BUSY` | 409 | No | Another state-mutating operation holds the environment lock |
