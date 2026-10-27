# Asset Orchestration Guide (Dagster)

Step-by-step guide for developing the asset orchestration code under
`{workspace}/asset-dag/` and bringing changes live.

---

## Directory Layout

`asset-dag/` is a Dagster code location in the modern `dg` layout:

```
asset-dag/
├── definitions.py        # entrypoint: auto-discovers defs/ into Definitions
├── pyproject.toml        # project metadata; [tool.dagster] module_name
├── defs/                 # ALL definitions live here (auto-discovery root)
│   ├── deployment.py     # MANAGED: deployment identity + schedule gate
│   ├── assets/           # asset definitions (SQLMesh bridge + Daft assets)
│   ├── resources/        # resources, wired via @dg.definitions
│   ├── jobs/             # asset jobs
│   ├── schedules/        # schedules
│   └── sensors/          # sensors
└── tests/                # tests — sibling of defs/, NOT auto-discovered
```

Key rules:
- **Everything under `defs/` is auto-discovered.** A new module with a top-level
  `@asset`, job, schedule, or sensor object shows up automatically — no manual
  registration in `definitions.py`.
- **Tests live in `tests/`, never in `defs/`** — modules under `defs/` are
  treated as definitions; test modules must stay out of the auto-discovery root.
- **Use importable absolute imports** inside modules, e.g.
  `from defs.jobs.example_job import example_job`. NEVER import the `asset-dag`
  directory itself — its hyphenated name is not a valid Python package.

---

## Platform-Managed Files (RED LINE)

Two files are managed by the platform and regenerated whenever they drift
from the template — **local edits are silently lost**:

| File | Role | Why it is managed |
|------|------|-------------------|
| `defs/deployment.py` | Deployment identity + schedule gate | Injected env vars differ per environment |
| `defs/assets/sql_assets.py` | SQLMesh → Dagster asset bridge | Asset-key convention must match the platform |

Put your own code in the sibling packages (`defs/assets/`, `defs/jobs/`,
`defs/schedules/`, `defs/sensors/`, `defs/resources/`). Never edit the two
files above.

---

## The Scheduling Gate (RED LINE)

Every environment loads THE SAME code from its own Git branch, so the code
itself cannot tell production apart from a feature branch. Without a gate, a
sandbox environment's schedule would be picked up by the daemon and drive
production data on every tick.

**Always declare schedules and sensors with the gate constants:**

```python
import dagster as dg

from defs.deployment import DEFAULT_SCHEDULE_STATUS
from defs.jobs.example_job import example_job

daily_schedule = dg.ScheduleDefinition(
    name="daily_example_job",
    job=example_job,
    cron_schedule="0 2 * * *",
    execution_timezone="UTC",
    default_status=DEFAULT_SCHEDULE_STATUS,  # NEVER hardcode RUNNING
)
```

- Use `DEFAULT_SCHEDULE_STATUS` / `DEFAULT_SENSOR_STATUS` imported from
  `defs.deployment`. Hard-coding `dg.DefaultScheduleStatus.RUNNING` bypasses
  the platform's decision of which environments may be scheduled — forbidden.
- Use `execution_timezone="UTC"` to stay consistent with the backend's
  UTC-only scheduling policy.
- `defs.deployment` also exports `ENVIRONMENT`, `DEPLOYMENT_NAME`,
  `WORKSPACE_ID`, `IS_PRODUCTION`, and `run_tags()` (platform run tags for
  runs your own code launches, e.g. a sensor's `RunRequest`).

---

## Wiring Resources

Resources are NOT auto-discovered as bare objects. Wire them by returning a
`Definitions(resources={...})` from a `@dg.definitions` function:

```python
import dagster as dg


@dg.definitions
def resources() -> dg.Definitions:
    return dg.Definitions(
        resources={"my_api": MyApiResource()},
    )
```

Assets consume a resource by declaring a parameter with the resource type:

```python
@dg.asset
def my_asset(my_api: MyApiResource) -> None:
    data = my_api.fetch("/path")
```

Keep secrets in env vars and pull them via `dg.EnvVar` — never hard-code.

---

## Declaring Third-Party Python Packages (RED LINE)

`asset-dag/pyproject.toml` under `[project].dependencies` is the ONE place to
declare what your assets import. `pip install` is refused platform-wide: a
package installed by hand vanishes with the sandbox that ran the command.

**Before writing or editing a version bound, read the baseline:**

```
dc deps baseline            # what the platform already provides
dc deps baseline --all      # every distribution, including transitive ones
dc deps baseline --json     # machine-readable
```

The rules, and why each one exists:

- **A package in the baseline is already importable.** Declaring it is allowed
  and installs nothing. Omitting it is also fine — the import works either way.
- **NEVER pin a baseline package away from the version it ships.** A workspace
  overlay is prepended to `PYTHONPATH`, so it would shadow the platform's copy
  and break the code location. Such a declaration is REFUSED (failure code
  `deps_platform_conflict`), not installed.
  - Wrong: `"pyarrow<15.0.0,>=14.0.0"` when the platform ships pyarrow 25.0.1
  - Right: `"pyarrow>=25.0.1"` — or leave it out entirely
- **Use a lower bound (`>=`), not an exact pin (`==`).** The baseline moves with
  the platform image; a pin turns today's working declaration into tomorrow's
  conflict.
- **Transitive resolution is pinned to the baseline, with one exception.** A
  package you declare cannot drag in a different numpy/pyarrow/dagster than the
  code location runs on — those are ABI-bound or part of the platform itself.
  But a pure-Python baseline package (`tenacity`, `tqdm`) IS released and
  retried automatically when keeping it pinned would make your declaration
  unsatisfiable, because overriding it affects this working copy alone.
  - When that happens the install SUCCEEDS and `dc deps sync` / `dc deps status`
    print the overrides. Leave them alone unless something misbehaves; they are
    the reason the install worked.
  - When the pin in the way is NOT releasable, the install fails with
    `deps_platform_transitive` and names the package. Pick another release of
    what you declared (the failure carries a rewrite that pip verified), or tell
    the user an operator has to upgrade the baseline. Do not attempt to install
    the blocking package yourself — that is the one thing the platform refuses.
- **Only wheels are installed.** The install sandbox has no compiler and never
  runs a package's build script. A package that publishes only a source
  distribution therefore cannot be installed at any version, and fails with
  `deps_no_wheel` naming it — `pyspark` is the usual one (PyPI carries a single
  ~300 MB sdist per release). Do NOT try other version bounds: the failure says
  a matching version EXISTS, so changing the bound cannot help. Either use a
  package that ships wheels, or tell the user an operator must add it to the
  platform image (then it is importable without being declared at all).

**Check before reloading** (both are cheap and install nothing):

```
dc deps status              # state + any conflict, with the exact rewrite
dc deps sync                # install; does NOT reload the code location
```

`dc deps status` prints a `Fix :` line per conflict naming the requirement to
write instead. Apply it with `apply_patch` and re-run `dc deps status` — do not
reload to find out whether a bound works, the check above already knows.

---

## Relationship to sql-warehouse/

SQLMesh models appear in the Dagster asset graph as external assets with key
`["sqlmesh", "schema", "table"]` and group `sqlmesh`. The bridge
(`defs/assets/sql_assets.py`, platform-managed) fetches SQLMesh lineage from
the workspace runtime at code-location load time. Your Dagster assets can depend on
those keys — use the `list_models` tool to see which models exist and pick
asset keys accordingly.

---

## Verification Loop

Code changes become live ONLY through an explicit reload — nothing watches
the filesystem. After editing anything under `asset-dag/`:

1. **Reload**: `dc deployment reload` (write, confirm-gated) — re-imports the
   working copy's code into the running deployment
2. **Check the graph**: `dc assets` (read-only) — verify your new
   assets/jobs/schedules appear and dependencies resolve
3. **Trigger when needed**: `dc run <job_name>` (write, confirm-gated)

Committing code without reloading leaves the running deployment on stale
code; conversely, a reload picks up whatever is in the working copy.

---

## CLI Usage — dc Command Reference

`dc` is the DataCode platform CLI. Call it via the `shell` tool:

**How to call**: `shell(commands=["dc <subcommand> <args>"], timeout_ms=<ms>)`

| Command | Purpose | Risk |
|---------|---------|------|
| `dc assets [--json]` | Asset dependency graph of the active environment | Read-only |
| `dc deployment status [--json]` | Deployment state (idempotent — never respawns a live code location) | Read-only |
| `dc deployment reload` | Re-import orchestration code after `asset-dag/` edits | **Write** ⚠️ |
| `dc run <job_name> [--config-json '<json>']` | Launch a job run in the active environment | **Write** ⚠️ |
| `dc deps baseline [--all] [--json]` | Packages the platform provides, with the string to declare | Read-only |
| `dc deps status [--json]` | Declared vs installed, plus any platform conflict | Read-only |
| `dc deps sync` | Install the declared packages (no reload) | **Write** ⚠️ |

Notes:
- The active environment (toolbar) is sent as a confirmation field; the
  backend rejects a mismatch with the working copy. Do not try to target
  another environment — it cannot work.
- `--json` gives structured output (nodes/edges, deployment info) — prefer it
  when you need to parse the result programmatically.
- `sqlmesh run` is disabled platform-wide: data refresh is governed. Land
  data with `sqlmesh plan`/`apply`; trigger orchestrated refresh with
  `dc run <job_name>`.

---

## Important Rules

1. **Never edit `defs/deployment.py` or `defs/assets/sql_assets.py`** — platform-managed, edits are lost
2. **Never hardcode `RUNNING`** on schedules/sensors — always `DEFAULT_SCHEDULE_STATUS` / `DEFAULT_SENSOR_STATUS`
3. **Never import the `asset-dag` directory** — absolute imports from `defs.` only
4. **Keep tests in `tests/`**, not in `defs/`
5. **Reload before verifying** — `dc deployment reload`, then `dc assets`
6. **Prefer `apply_patch`** for editing existing files (precise edits, diff card in the UI)
7. **Never `pip install`** — declare in `asset-dag/pyproject.toml`, then `dc deps sync`
8. **Read `dc deps baseline` before writing a version bound** — never pin a
   platform package away from the version it ships
9. **Production environment**: when the active environment is production, do
   not edit orchestration code directly — develop in a non-prod environment
   and go through the promotion flow
