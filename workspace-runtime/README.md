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
├── codeloc/     Dagster code locations, deployments, ports, GraphQL client,
│                reload jobs (reload_jobs.py — the asynchronous reload)
├── deps/        the working copy's third-party Python packages: manifest +
│                injection decision (manifest.py) and the install orchestration
│                (service.py)
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

## Working-copy Python dependencies

A working copy declares its third-party packages ONCE, in
`asset-dag/pyproject.toml` under `[project].dependencies`. They are installed
once per copy into `.datacode/deps/site-packages` by a `deps.sync` task (profile
`L3_deps_install`: writable copy, egress to the package index only, zero
credentials) and prepended to `PYTHONPATH` in three places — the code location,
the local sandbox backend and the Kubernetes sandbox Job.

Six decisions worth knowing before changing anything here:

* **`pip install --target`, not a virtualenv.** The same copy is a host path to
  this service and `/workspace` inside a sandbox; a virtualenv pins absolute paths
  into `pyvenv.cfg` and every shebang, so it stops working through a second mount
  point. A `--target` tree is path-independent.
* **The baseline is THIS service's interpreter, and it travels in the plan.**
  A code location imports user code on the workspace-runtime interpreter, so the
  set importable without any overlay is exactly the set this process has
  (`src/deps/baseline.py`). The installer runs elsewhere — the task-runner image,
  or a hardened child under the local backend — which is why the baseline is
  DATA carried by `InstallPlan` rather than something the installer looks up.
  An installer that introspected its own interpreter would install a second
  `dagster` over the platform's and skip packages the code location lacks, and
  neither is visible in development, where both interpreters are the same
  process. A plan without a baseline is REFUSED, not treated as "the platform
  provides nothing".
* **Platform packages cannot be overridden, and that is decided before pip runs.**
  `sys.path` is `'' -> PYTHONPATH -> stdlib -> site-packages`, so an overlay always
  shadows the image's own `dagster` / `sqlmesh`. A declaration asking for a version
  the platform does not ship is REFUSED (`deps_platform_conflict`, with the
  rewrite that would fix each line) — no ordering can honour it.
* **Transitive resolution is bounded by generated constraints, which are
  LAYERED.** The baseline is written to
  `.datacode/deps/platform-constraints.txt` and passed as `--constraint`, so a
  declared package cannot drag in a numpy/pyarrow other than the one the code
  location runs on. But pinning all several hundred of them would give each a
  veto over every declaration, and `feast` needing `tenacity<9` is not the same
  problem as it needing `numpy<1.25`. So the runtime derives the yieldable
  subset — no compiled extension, not part of the platform's own runtime,
  adjusted by `deps.protected_packages` / `deps.relaxable_packages` — and on a
  refused resolution the installer releases exactly the pins pip implicated, if
  all of them are yieldable, and retries once. What yielded is recorded
  (`relaxed[]` in the manifest, the reload envelope and `/deps/status`) because
  the copy now diverges from the platform on those packages. Anything shadowing
  the platform that was NOT yielded still FAILS the install — it is unreachable
  through any declaration, so it means the two images are out of step.
* **`ensure` never installs.** It is on the asset page's load path and reports
  drift and platform conflicts only; the install runs inside a reload job the
  user triggered, and a failed install FAILS that reload rather than starting a
  location whose assets cannot import.
* **Every failure is archived** to `{workspace}/logs/errors/`, and the code
  location's own stdout/stderr goes to `{workspace}/logs/codeloc/server.log` — its
  tail is the import traceback a failed reload reports.

The baseline is also VISIBLE, which is what makes a compatible declaration
writable: `GET /api/v1/deps/baseline` (no `workspace_path` — it describes the
image, not a copy) and `dc deps baseline [--all]` list every package with the
exact string to declare for it. Saving `asset-dag/pyproject.toml` returns the
same verdict inline, so a bad version bound is reported while the cursor is
still on the line instead of after a multi-minute reload. The scaffolded
declaration is GENERATED from the baseline for the same reason — a hand-written
`dagster>=1.8.0` drifts the moment the image is upgraded.

Configuration lives under `deps:` in `conf/workspace-runtime.yaml`; `index_url`
comes from platform configuration on purpose, never from the working copy, and
`protected_packages` / `relaxable_packages` adjust which baseline pins may yield
(the failure message names the second key when only an operator can unblock an
install).

## Run

```
pip install ./shared/python -e ".[dev]"
uvicorn src.main:app --port 8002
```
