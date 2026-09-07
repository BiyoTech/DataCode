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

## Run

```
python3.13 -m venv .venv && ./.venv/bin/pip install -e ../shared/python -e ".[dev]"
./.venv/bin/python -m pytest -q
./.venv/bin/python -m src.db_migrate          # migrate step (idempotent)
./.venv/bin/uvicorn src.main:app --port 8004
```
