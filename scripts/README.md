# scripts/

Everything that is not application code. Grouped by WHO runs it and WHEN —
a script whose name is a roadmap phase (verify-p4a…) answers a question only
the person who wrote it can ask; these names answer "what does it check".

## dev/ — the host development loop

| Script | What it does | Typical use |
|---|---|---|
| `dev-setup.sh` | One-time: toolchain check, per-service venvs, Go deps, npm install, `.env` bootstrap, storage up | `make setup` |
| `dev-start.sh` | Full stack on the host: storage (auto via Docker), migrations, identity → control/agent/runtime → gateway → frontend, supervision | `make dev` / `make dev-backend` |
| `dev-frontend.sh` | Just the Vite server, under the same lifecycle (logs, port cleanup, readiness) | `make dev-frontend` |
| `dev-stop.sh` | Stop everything the dev stack started (process-group aware; never touches Docker infra) | `make stop` |
| `dev-lib.sh` | Shared library for the above — never executed directly. Owns the PostgreSQL password truth file (`.data/postgres_password`): adopted from a running instance or seeded on first use, then the single source every flow reads | — |
| `dev-svc-runner.py` | Internal: per-service process-group runner used by `start_svc` | — |
| `pg-password.sh` | Executable entry to the password truth file: `ensure` (create/adopt), `print`, `set <PW>` (ALTER USER + file — the only supported way to change it) | `make pg-set-password PASSWORD=...` (and used by every compose target) |

## verify/ — executable acceptance gates

Each is the runnable form of a design acceptance item: it executes what CAN be
executed on this machine and reports `REQUIRES-K8S`/`MANUAL` for what cannot —
a skipped check is a hole in the record, never a pass.

| Script | Verifies | Typical use |
|---|---|---|
| `verify-lease-routing.py` | Lease single-writer: concurrent-acquire race, crash takeover, drain handover | `make data-plane-up && python3 scripts/verify/verify-lease-routing.py` |
| `verify-sandbox-isolation.py` | P6 sandbox security (T1–T10) | `python3 scripts/verify/verify-sandbox-isolation.py [--k8s]` |
| `verify-capacity-control.py` | P8 capacity orchestration against a real cluster (needs kind + Redis) | `python3 scripts/verify/verify-capacity-control.py` |
| `verify-workspace-lifecycle.py` | Workspace create/delete contract over real git: error passthrough (missing remote → `repo_not_found` + remote wording), create-or-reuse (no template on existing/empty repos), hard purge (rows of the three owned tables, prefix-neighbour guard, remote untouched, idempotent) | `python3 scripts/verify/verify-workspace-lifecycle.py` |

## ops/ — operator tooling

| Script | What it does | Typical use |
|---|---|---|
| `cell-migrate.py` | Tenant migration between cells: plan / run / purge-source (drain → migrate → verify → stable) | `python3 scripts/ops/cell-migrate.py plan --tenant t1 --target cell-1` |
| `measure-lease-memory.py` | Measure a working copy's RSS footprint to calibrate lease memory budgets | `python3 scripts/ops/measure-lease-memory.py --workspace <path>` |

## Top-level

| Script | What it does | Typical use |
|---|---|---|
| `config-doctor.py` | Print the effective config per profile and check its invariants (no service needs to run) | `make doctor` |
| `gen-rbac-catalog.py` | Generate the Go and Python permission-catalog tables from `shared/rbac-catalog.json`; `--check` fails when either has drifted (runs in CI) | `python3 scripts/gen-rbac-catalog.py` |
| `build-images.sh` | Build all 8 images with one versioned tag (VERSION + git sha) | `make images` |

## sqlmesh-cli/

The `dc-sqlmesh` CLI shim delegating to workspace-runtime. See its own header.
