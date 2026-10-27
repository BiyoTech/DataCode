# SQLMesh Modeling Guide

Step-by-step guide for creating, modifying, and deploying SQLMesh data models.

---

## SQLMesh Model Format

```sql
MODEL (
    name schema_name.model_name,
    kind FULL,
    cron '@daily',
    grain (id),
    description 'Description of what this model does'
);

SELECT
    column1::TEXT, -- column1 description
    column1::DOUBLE AS revenue -- column2 description
    ...
FROM source_table
WHERE conditions;
```

Key MODEL() parameters:
- `name` — fully qualified: `schema_name.model_name` (snake_case)
- `kind` — model kind (see below)
- `cron` — schedule expression ('@daily', '@hourly', etc.)
- `grain` — primary key column(s) for uniqueness
- `description` — human-readable description (REQUIRED)
- Additional per-kind parameters — check `sqlmesh_reference` skill or `concepts/models/model_kinds.md`

---

## Model Kind Selection Guide

For detailed parameters and all available kinds, load the `sqlmesh_reference` skill or read `concepts/models/model_kinds.md`.

| Scenario | Recommended Kind |
|----------|-----------------|
| Large fact tables with time columns | `INCREMENTAL_BY_TIME_RANGE` |
| Dimension / lookup tables | `FULL` |
| Lightweight transformations, no persistence needed | `VIEW` |
| Slowly changing dimensions (Type 2) | `SCD_TYPE_2` |
| Additive accumulations (e.g., daily aggregates) | `INCREMENTAL_BY_UNIQUE_KEY` |
| Static seed data | `SEED` |
| External tables | `EXTERNAL` |

General rules:
- Prefer `INCREMENTAL_BY_TIME_RANGE` for large fact tables
- Use `FULL` for dimension tables that should be rebuilt each run
- Use `VIEW` for lightweight transformations only
- Never use `SELECT *` in production models
- Always include `grain()` and `description` in the MODEL() block

---

## Editing Files with apply_patch

When modifying an EXISTING file, always prefer `apply_patch` — it makes precise line-level edits and renders a diff card in the UI. Do NOT rewrite the whole file with `create_file` just to change a few lines.

**Workflow for editing:**
1. ALWAYS `read_file` first so your patch context lines match the current content
2. Construct the patch
3. Use `apply_patch` to apply it

**Patch format (V4A envelope):**

```
*** Begin Patch
*** Update File: models/staging/stg_users.sql
@@
 SELECT
-    old_col
+    new_col
 FROM source
*** End Patch
```

**Section headers:**
- `*** Add File: <path>` — create new file (every body line prefixed with `+`)
- `*** Update File: <path>` — modify existing file
- `*** Delete File: <path>` — remove file

**Line markers within Update File:**
- Context lines (unchanged): start with a single space
- Removed lines: start with `-`
- Added lines: start with `+`

**For Add File:** all body lines must be prefixed with `+`.
**Paths:** relative to the project root.

---

## Creating New Files

Use `create_file` for brand-new model files. Do NOT use it to modify existing files.

---

## Standard Workflow

> **Active environment**: Always target the environment shown in your Current Context
> (`Current Environment`), which is the one the user selected in the toolbar. Below,
> `<active_env>` denotes that environment (e.g. `dev`, `prod`). Never hardcode or guess
> a different environment. The platform enforces the active environment for `plan`/`apply`,
> so a mismatched environment passed on the command line is overridden.

1. **Discover**: Use `schema_discovery` to understand available data sources and table structures
2. **Read**: Use `read_file` to examine existing models and understand current project structure
3. **Design**: Plan the model SQL based on user requirements and discovered schemas
4. **Create**: Use `create_file` to create the new model in the appropriate directory
5. **Preview**: Run `shell(commands=["sqlmesh plan <active_env>"])` to show the impact of changes (which models will be added/modified/removed). This is read-only and never deploys.
6. **Confirm**: Present the preview to the user and ask for explicit confirmation to deploy. Do NOT proceed without a clear "yes".
7. **Apply**: ONLY after the user confirms, run `shell(commands=["sqlmesh apply <active_env>"])` to deploy changes. The platform serves this itself — the same code path as the Web deploy button — for unified governance (locking, optimistic concurrency, auditability).

---

## Task Planning with write_todo

For complex, multi-step requests, call `write_todo` FIRST to break the work into clear, actionable items.

- Only ONE item should be `in_progress` at a time
- Mark items `completed` IMMEDIATELY after finishing
- Keep content concise and user-facing (avoid internal implementation details)

Example:
```
write_todo(todos='[
  {"content":"Create stg_orders model","status":"in_progress"},
  {"content":"Add grain and description","status":"pending"},
  {"content":"Preview plan","status":"pending"}
]', merge=false)
```

---

## Important Rules

1. **Always check existing schemas before writing SQL** — use `schema_discovery` first
2. **Use proper naming**: `schema_name.model_name` in snake_case
3. **Include meaningful `grain()`** — primary key definitions for every model
4. **Add `description` to all models** — serve as documentation
5. **Prefer `INCREMENTAL_BY_TIME_RANGE`** for large fact tables
6. **Use `FULL`** for dimension tables
7. **Use `VIEW`** for lightweight transformations
8. **Preview before deploy, and ALWAYS get user confirmation** — run `sqlmesh plan <active_env>` (preview), show it to the user, and only run `sqlmesh apply <active_env>` after the user explicitly confirms. NEVER deploy without confirmation.
9. **Never use `SELECT *`** in production models
10. **Always include `grain()` and `description`** in the MODEL() block
11. **Always show generated SQL** before creating the file
12. **After file creation, suggest running `sqlmesh plan <active_env>` via shell**

---

## CLI Usage — sqlmesh Command Reference

Use the `shell` tool to run sqlmesh CLI commands directly. Pass a list of commands
to `shell` and it executes them sequentially inside the workspace with the Python
virtual environment activated.

**How to call**: `shell(commands=["sqlmesh <subcommand> <args>", "..."], timeout_ms=<ms>)`

**Working directory**: The SQLMesh project lives at `{workspace}/sql-warehouse/`.
The platform locates it automatically — run `sqlmesh test`, `sqlmesh audit`,
`sqlmesh render`, etc. directly from anywhere in the workspace. Do NOT prepend
`cd sql-warehouse` yourself.

**One command per string.** `plan`, `apply`, `create_external_models` and
`ontology` are served by the platform itself (environment lock, apply history,
audit). The platform can only do that for a SINGLE command — a string containing
`&&`, `;`, `|` or `$(...)` is executed in the sandbox instead, where those verbs
are REFUSED. So write `shell(commands=["sqlmesh plan dev", "sqlmesh test"])`,
never `shell(commands=["sqlmesh plan dev && sqlmesh test"])`.

| Command | Purpose | Risk |
|---------|---------|------|
| `sqlmesh plan <env>` | Preview changes without applying (read-only, never deploys) | Read-only |
| `sqlmesh apply <env>` | Deploy changes — **requires user confirmation first** | **Write** ⚠️ |
| `sqlmesh run <env>` | **DISABLED** — data refresh bypasses governance; use `sqlmesh apply` for versioned landing or `dc run <job>` for orchestrated refresh | **Blocked** ⚠️ |
| `sqlmesh test` | Run all unit tests for models | Read-only |
| `sqlmesh audit` | Run data quality audits | Read-only |
| `sqlmesh render <model_name>` | Render a model's compiled SQL (debugging) | Read-only |
| `sqlmesh table_diff <source> <target>` | Compare tables between environments | Read-only |
| `sqlmesh validate` | Validate model definitions | Read-only |
| `sqlmesh evaluate <model_name>` | Evaluate a model and show sample output | Read-only |
| `sqlmesh info` | Show project/schema info | Read-only |
| `sqlmesh dag` | Print model DAG (dependency graph) | Read-only |
| `sqlmesh format` | Auto-format SQL files | **Write** ⚠️ |
| `sqlmesh create_external_models` | Register external tables | **Write** ⚠️ |

> **`--auto-apply` is disabled.** Preview (`plan`) and deploy (`apply`) are two
> distinct commands. `sqlmesh plan <env> --auto-apply` is rejected so a preview
> can never accidentally deploy. Deployment must be an explicit,
> user-confirmed `sqlmesh apply <env>` call.

> **The environment argument does not choose the target.** Your working copy is
> checked out to exactly one environment, and that is where `plan`/`apply` act.
> Naming a different one is reported back to you and ignored.

**When to use CLI over tool calls:**
- `shell(commands=["sqlmesh plan <active_env>"])` — preview plan changes
- `shell(commands=["sqlmesh test"])` — run ALL tests (no equivalent tool exists)
- `shell(commands=["sqlmesh audit"])` — run ALL audits (no equivalent tool exists)
- `shell(commands=["sqlmesh render stg_orders"])` — debug compiled SQL (no equivalent tool)
- `shell(commands=["sqlmesh table_diff prod dev stg_orders"])` — compare tables (no equivalent tool)
- `shell(commands=["sqlmesh evaluate stg_orders"])` — sample query output (no equivalent tool)

**Workflow example — create model then deploy:**
```
1. create_file("models/staging/stg_orders.sql", ...)
2. shell(commands=["sqlmesh plan <active_env>"])   # preview (read-only)
3. shell(commands=["sqlmesh test"])                # run tests
4. <ask the user to confirm the deployment, show the preview>
5. shell(commands=["sqlmesh apply <active_env>"])  # deploy — ONLY after user confirms
```

**Warning**: `sqlmesh apply <env>` is served by the platform itself — the same
code path the Web deploy button uses — for unified governance, locking and
auditability. It is a destructive write operation and MUST only be run after the
user has explicitly confirmed the deployment. Always run `sqlmesh plan <env>` first
to preview. The legacy `plan --auto-apply` is disabled, and so is `sqlmesh run`:
data refresh outside platform governance is forbidden — land data with
`sqlmesh apply`, or trigger an orchestrated refresh with `dc run <job_name>`.

---

## Code Search

Use `search_code` to find patterns, references, or usages across the project before making changes.

---

## Reference Documentation

For specific SQLMesh syntax, model kind parameters, macro usage, or engine configuration:
- Load `sqlmesh_reference` skill for the complete documentation index
- The sqlmesh_docs are at `src/sqlmesh_docs/`
- Key files: `concepts/models/model_kinds.md`, `reference/cli.md`, `reference/model_configuration.md`
