# Gateway API Reference

> Frontend-facing API documentation. All timestamps are UTC ISO 8601.  
> Base URL: `/api/v1`

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
- [Workspace Logs](#workspace-logs)
- [Error Codes](#error-codes)

---

## Authentication

All endpoints require the `X-User-ID` header (injected by gateway middleware). Some operations also accept `user_id` in the request body for credential resolution.

---

## SQLMesh Operations

All SQLMesh operations are proxied to the SQLMesh Service via `/api/v1/sqlmesh/*`.

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

### Destroy Environment

Release the SQLMesh views, worktree and branch. The registry row and its events
are kept for audit. Production is rejected.

```
DELETE /api/v1/environments/{environment_id}?delete_remote_branch=true
```

Requires `workspace:env:delete`.

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

### Create Workspace

```
POST /api/v1/workspaces
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | ✓ | - | Workspace name |
| `dialect` | string | | `"duckdb"` | SQL dialect |
| `user_id` | string | | `""` | User ID |
| `git_repo_url` | string | | `""` | Git repo to clone |

### List Workspaces

```
GET /api/v1/workspaces?user_id=...
```

### Bind Git Repo

```
POST /api/v1/workspaces/bind-git
```

### Sync Workspace

```
POST /api/v1/workspaces/sync
```

### Ensure Local

```
POST /api/v1/workspaces/{workspaceID}/ensure-local
```

### Delete Workspace

```
DELETE /api/v1/workspaces/{workspaceID}?delete_remote=false
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
| POST | `/api/v1/git/pull` | Pull from remote (invalidates context) |
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
| POST | `/api/v1/files/write` | Write file (marks context dirty) |
| POST | `/api/v1/files/delete` | Delete file (marks context dirty) |
| POST | `/api/v1/files/mkdir` | Create directory |
| POST | `/api/v1/files/rename` | Rename/move file (marks context dirty) |

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

`sources` and `levels` are comma-separated. A source is a file origin such as
`sqlmesh-service`, `dagster-service` or `sqlmesh-cli` (SQLMesh's own text logs,
exposed read-only). Paginate by passing the previous response's `next_cursor`.

An entry has the shape:

```json
{
  "id": "sqlmesh-service:sqlmesh-service.2026-08-21.ndjson:42",
  "ts": "2026-08-21T09:30:12.123Z",
  "level": "error",
  "source": "sqlmesh-service",
  "service": "sqlmesh-service",
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

## Error Codes

All errors follow this structure:

```json
{
  "code": "PLAN_FAILED",
  "message": "Failed to create plan preview",
  "detail": "Connection refused..."
}
```

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
