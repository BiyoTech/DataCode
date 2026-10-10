# control-service

The control plane (Python, `:8004`): environment registry, reconciliation,
workspace metadata, Git credentials, quotas, capacity orchestration — and the
platform's ONLY alembic chain. Delivered by design 06 P5, extended by P7/P8.

## Deliberately light dependencies

No SQLMesh engine, no Context pool, no filesystem access to working copies.
Every Git/SQLMesh/workspace side effect is DISPATCHED to workspace-runtime over
`/internal/env-control/*` (`src/runtime/client.py`, `X-Internal-Token`): the
gateway decides WHETHER a request is allowed, control decides WHAT the platform
should do, runtime is the only one that touches the files. That is what lets
control scale on DB QPS alone.

## Layout

```
src/
├── environments/  registry, reconciler (5 drift kinds), reaper, sweep,
│                  git/sqlmesh binders (commands sent to runtime)
├── workspace/     metadata ORM (the authoritative copy), manager, identity
├── quota/         workspace/environment quota enforcement + Redis publication
├── records/       task_records / lease_history / tenant_usage (retention)
├── capacity/      LeaseController: heartbeat-watermark scaling, drain-first
├── api/rest/      metadata routes, quotas, knowledge list, cell migration
│                  (migration moves the SIX tables this service owns and
│                  delegates the runtime's three to it — one creator per table)
├── runtime/       RuntimeClient — the one channel to workspace-runtime
├── core/          env locks (sm:env_lock:* — shared namespace with runtime's
│                  apply path: cross-service mutual exclusion)
└── db_migrate.py  `python -m src.db_migrate` — create_all + alembic head
```

## Table ownership (enforced, not documented)

Control OWNS and creates: `workspaces`, `environments`, `environment_events`,
`user_git_credentials` (+ alembic 0005's quota/audit tables). Runtime creates
only its own three tables — two `create_all` runs racing on the shared
database was an intermittent boot failure, and `db_migrate` being the single
creator is the fix (see `tests/test_db_migrate.py`).

Every deployment's migrate step runs the same entry point: dev's
`run_db_migrations`, the compose one-shot `migrate` service, the Kubernetes
`control-migrate` Job. Replicas run with `AUTO_MIGRATE=0`.

Ownership covers writes, not reads. The runtime READS `workspaces`,
`user_git_credentials` and one `environments` row directly — those reads are on
its hot path, tolerate a stale row and cannot corrupt anything. For the two
facts it authors (an apply's watermark, a merge's audit row) it calls
`/internal/env-registry/*` (`src/api/rest/env_registry.py`,
`X-Internal-Token`), so this service stays the sole writer. That surface takes
FACTS, never column names: there is no generic "update this row" endpoint
through which the registry's own whitelist could be bypassed.

## What a destroy may take (and what it may not)

Destroying an environment releases only state the PLATFORM created: the SQLMesh
virtual environment, every member's working copy, the local branch, the Dagster
deployment, and the registry status. **The remote branch is never touched.** It
lives in the user's repository — which may predate the workspace, be shared with
tooling outside DataCode, and hold the only durable copy of that work — so
deleting it as a side effect of releasing derived state is not the platform's
call. The same reasoning already governed workspace deletion; environments now
follow it.

The verifiable form of that promise: this repository contains no
`git push --delete`, and no endpoint or binder method through which one could be
requested (`tests/test_binders_http.py`, `scripts/verify/verify-environment-branch-reuse.py`).

Two consequences worth knowing:

* **A preserved branch is an asset, not a leftover.** Creating an environment
  whose remote branch still exists RESUMES it (reported as
  `branch_source=reused_remote`) instead of re-cutting one from the base, and
  the reconciler must not report it as `stale_resources` or `orphan_branch` —
  an archived row still claims its own branch.
* **Unpushed commits are the only code a destroy can lose**, because the local
  branch deletion is forced. They are reported by
  `GET /environments/{id}/pending-work` before the user confirms and recorded
  afterwards in the destroy event; they never refuse the request. Uncommitted
  changes still do, unless `force=true` (which the TTL reaper depends on).

## Run

```
python3.13 -m venv .venv && ./.venv/bin/pip install -e ../shared/python -e ".[dev]"
./.venv/bin/python -m pytest -q
./.venv/bin/python -m src.db_migrate          # migrate step (idempotent)
./.venv/bin/uvicorn src.main:app --port 8004
```
