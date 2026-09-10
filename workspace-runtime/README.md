# workspace-runtime

The sole owner of working copies (design 02, delivered by design 06 P3):
SQLMesh engine + Ontology + Dagster code locations + Git + file IO + gated
tool execution, merged into ONE process.

## Why one process

The three legacy services (sqlmesh-service, dagster-service,
ontology-service) read three sub-directories of the SAME working copy. That
forced a shared mutable PV, produced two service-to-service HTTP cycles
(sqlmesh <-> dagster, ontology -> sqlmesh) and made sandboxing impossible.
Merging them here turns those cycles into in-process calls — verified by the
merge: `src.codeloc.bridge` and `src.ontology.sqlmesh_metadata` now call
`src.sqlmesh.*` directly.

## Layout

```
src/
├── sqlmesh/     engine: Context pool, plan/apply, lineage, models, env locks
├── git/         git ops (ops.py, merge.py) — worktree lifecycle
├── files/       file IO (ops.py) + content search (search.py) + layout
├── ontology/    semantic-layer loading + cross-ref validation (in-process)
├── codeloc/     Dagster code locations, deployments, ports, GraphQL client
├── executor/    L3-proc: persistent child holding the EXECUTABLE Context
├── api/         REST: rest/* (sqlmesh+ontology+tools), dagster/* (data plane)
├── tasks/       task submission: sandbox backends, warm pool, egress allow-list
├── lease/       lease server/holder, placement, WIP snapshots, audit
├── workspace/   own tables (apply history, knowledge, drafts) + ORM mirrors
│                of control's registry tables (never created here — control
│                owns them; see src/workspace/models.py). This service also
│                MOVES its three tables across cells (api/rest/cell_migration.py):
│                whoever creates a table owns its migration, so no table ever
│                has a second creator or a mirrored definition free to drift.
├── environments/ environment guard + ORM mirrors of control's registry
├── logs/        workspace log reader
└── utils/       errors, logging, error archive
```

## The executor (design 04 §2.3, Q1 decision)

`executor/process.py` forks one persistent child per working copy with a
CLEARED environment, lowered rlimits and (with CAP_SETUID) a dedicated
unprivileged uid. The child (`executor/server.py`) speaks the FROZEN
protocol (`datacode_common.executor_protocol`) and holds the hot SQLMesh
Context — constructing it execs the user's Python models/macros, which must
never happen in this service's process (threat T2).

Enable per environment with `executor.enabled: true` in
`conf/workspace-runtime.yaml`. Verified behaviors: platform secrets absent
from the child environment, cwd pinned to the working copy, uid separation
reported honestly in the handshake.

## Run

```
pip install ./shared/python -e ".[dev]"
uvicorn src.main:app --port 8002
```
