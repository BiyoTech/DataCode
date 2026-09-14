"""Canonical asset-dag (Dagster code location) skeleton.

Modern Dagster ``dg`` auto-discovery layout: the ``asset-dag/`` directory is the
code location's working directory (NOT a Python package, so its hyphenated name
is safe). ``definitions.py`` is the single entrypoint; it calls
``load_defs(defs_root=defs)`` to auto-discover everything under the importable
``defs/`` package. Tests live in a sibling ``tests/`` package and are NOT part of
auto-discovery.

Inside modules, use importable absolute imports (``from defs.jobs.example_job
import ...``); never import the ``asset-dag`` directory.

Ported from the legacy sqlmesh-service (P5). NOTE: keep this skeleton in sync
with ``src/templates/``, which re-renders any missing file when a code location
is ensured. The inline constants below MUST match those templates verbatim.
"""

import os

from src.utils.logging import get_logger

logger = get_logger(__name__)

_DEFINITIONS_PY = '''"""Dagster Definitions entrypoint for workspace {{ workspace_name }}.

Loaded by the runtime Code Location gRPC server. This is the single
entrypoint: it auto-discovers every definition under the ``defs/`` package
(assets, jobs, schedules, sensors) and assembles them into one ``Definitions``.

Auto-discovery rules (Dagster ``dg`` convention):
- Top-level ``@asset`` / job / schedule / sensor objects in any ``defs/`` module
  are discovered automatically.
- Resources are NOT auto-discovered as bare objects; wire them by returning a
  ``Definitions(resources={...})`` from a ``@dg.definitions`` function (see
  ``defs/resources/example_resource.py``).

This code location serves ONE environment: the runtime launches one per
environment, each loading that environment's own Git branch. The deployment
identity is injected as environment variables (``DATACODE_ENVIRONMENT``,
``DATACODE_DEPLOYMENT``, ``DATACODE_DEPLOYMENT_TYPE``,
``DATACODE_WORKSPACE_ID``, ``DATACODE_SCHEDULES_ENABLED``) alongside the
SQLMesh bridge settings (``WORKSPACE_RUNTIME_URL``, ``DAGSTER_WORKSPACE_PATH``,
``SQLMESH_GATEWAY``); read it through ``defs/deployment.py``, which also gates
schedule and sensor statuses on it.
"""

import os

import dagster as dg

import defs as defs_root

# ``project_root`` must point at the directory holding pyproject.toml (this
# file's directory, i.e. asset-dag/). Passing it explicitly avoids Dagster
# walking parent directories to locate the project root.
defs = dg.load_defs(
    defs_root=defs_root,
    project_root=os.path.dirname(os.path.abspath(__file__)),
)
'''

_PYPROJECT_TOML = """# THE INSTALL LIST for this working copy's third-party Python packages.
#
# Declare what your assets import under [project].dependencies, then press Reload
# in the asset page (or run `dc deps sync`). The declaration is shared by everyone
# working on this workspace and installed once for the whole working copy.
#
# `pip install` is refused on purpose: a package installed by hand disappears with
# the sandbox that ran the command and nothing records why it was there.
#
# Platform packages cannot be overridden — asking for a version the platform does
# not ship fails the sync instead of installing a copy that would shadow the
# platform's. Run `dc deps baseline` to see what it provides, with the exact
# string to declare for each.

[project]
name = "asset-dag"
version = "0.1.0"
description = "Data+AI pipeline (Dagster + Daft) for workspace {{ workspace_name }}"
requires-python = ">={{ python_version }}"
dependencies = [
{{ platform_dependencies }}
]

[tool.dagster]
module_name = "definitions"
"""

_README_MD = """# asset-dag — Dagster code location for workspace {{ workspace_name }}

This is the Dagster project for your Data+AI pipeline. It follows the modern
Dagster (`dg`) layout: a single `definitions.py` entrypoint auto-discovers all
definitions under the `defs/` package.

This working copy belongs to ONE environment. Every environment of the workspace
runs its own Dagster deployment from its own Git branch, all loading this same
project — which is why the code must never assume it is running in production.

## Layout

```
asset-dag/
├── definitions.py        # entrypoint: auto-discovers defs/ into Definitions
├── pyproject.toml        # project metadata; [tool.dagster] module_name
├── defs/                 # ALL definitions live here (auto-discovery root)
│   ├── deployment.py     # MANAGED: deployment identity + schedule gate
│   ├── assets/           # asset definitions (SQLMesh bridge + Daft example)
│   ├── resources/        # resources, wired via @dg.definitions
│   ├── jobs/             # asset jobs
│   ├── schedules/        # schedules
│   └── sensors/          # sensors
└── tests/                # tests — sibling of defs/, NOT auto-discovered
```

## Conventions

- **Everything under `defs/` is auto-discovered.** Add a new module with a
  top-level `@asset`, job, schedule, or sensor and it shows up automatically.
- **Gate schedules and sensors on the deployment.** Use
  `default_status=DEFAULT_SCHEDULE_STATUS` (or `DEFAULT_SENSOR_STATUS`) from
  `defs.deployment`. Only a deployment the platform allows to be scheduled runs
  them; hard-coding `RUNNING` makes a branch drive production data on every tick.
- **`defs/deployment.py` is platform-managed.** It is regenerated whenever it
  drifts from the template, so edits there are lost. It also exposes the current
  `ENVIRONMENT`, `DEPLOYMENT_NAME` and `WORKSPACE_ID` for your own code.
- **Resources are wired explicitly.** Auto-discovery does not pick up bare
  resource objects; return them from a `@dg.definitions` function that yields a
  `Definitions(resources={...})` (see `defs/resources/example_resource.py`).
- **Use importable absolute imports** inside modules, e.g.
  `from defs.resources.example_resource import ...`. Never import the
  `asset-dag` directory (its hyphenated name is not a valid Python package).
- **Keep tests in `tests/`, not in `defs/`.** Modules under `defs/` are treated
  as definitions; test modules must stay out of the auto-discovery root.

## Adding a third-party package

Declare it in `pyproject.toml` under `[project].dependencies`, then press
**Reload** in the asset page (or run `dc deps sync`). The platform installs it
once for the whole working copy and shows the install as it happens; it never
starts the code location with the packages missing, because your assets would
then fail to import. `dc deps status` reports what is declared and installed.

`pip install` is refused: a package installed by hand disappears with the sandbox
that ran the command. Platform packages (`dagster`, `sqlmesh`, ...) cannot be
overridden — the sync fails with the shipped version named instead of installing a
copy that would shadow the platform's.

## Running

The runtime launches this as a gRPC code location with:

```
dagster api grpc -f definitions.py --location-name <deployment>
```

with `cwd` set to this directory (so `import defs` resolves) and the deployment
identity injected as environment variables. The location name is the deployment
name, `{workspace_id}__{environment}`.
"""

_DEFS_INIT_PY = '''"""Auto-discovery root for workspace {{ workspace_name }}'s Dagster definitions.

Every module under this package is scanned by ``load_defs`` in ``definitions.py``.
Organize definitions by concept: assets/, resources/, jobs/, schedules/, sensors/.
"""
'''

_DEPLOYMENT_PY = '''"""Deployment identity and scheduling gate for workspace {{ workspace_name }}.

PLATFORM-MANAGED FILE. The runtime regenerates it whenever its content
drifts from the platform template, so local edits are lost. Put your own code in
the sibling packages (``defs/assets``, ``defs/jobs``, ``defs/schedules``, ...).

What this module is for
-----------------------
Every environment of a workspace runs its own Dagster deployment, and all of
them load THE SAME code from their own Git branch. Nothing in the code itself can
therefore tell production apart from a feature branch — that distinction is
injected by the runtime as environment variables when it launches the code
location, and this module is where the code reads it.

The consequence that matters is scheduling: without a gate, a sandbox
environment's schedule would be picked up by the daemon and drive production data
on every tick. Declare schedules and sensors with the statuses exported here::

    import dagster as dg

    from defs.deployment import DEFAULT_SCHEDULE_STATUS

    daily = dg.ScheduleDefinition(
        name="daily_refresh",
        job=my_job,
        cron_schedule="0 2 * * *",
        execution_timezone="UTC",
        default_status=DEFAULT_SCHEDULE_STATUS,
    )

Enabling schedules for a non-production environment is a platform decision, not a
code one: the environment registry sets it on the environment and the runtime
injects it here. Hard-coding ``RUNNING`` bypasses that decision.

Why this lives under ``defs/`` and not in ``definitions.py``: ``definitions.py``
imports the ``defs`` package, so a module under ``defs`` cannot import back from
it without a circular import.
"""

import os

import dagster as dg

# ── Deployment identity (injected by the runtime) ──────────────────
# Empty values mean the code was loaded outside a platform deployment (a local
# `dagster dev`, a unit test); the gate then falls back to the safest behavior.
ENVIRONMENT = os.getenv("DATACODE_ENVIRONMENT", "")
DEPLOYMENT_NAME = os.getenv("DATACODE_DEPLOYMENT", "")
DEPLOYMENT_TYPE = os.getenv("DATACODE_DEPLOYMENT_TYPE", "sandbox")
WORKSPACE_ID = os.getenv("DATACODE_WORKSPACE_ID", "")
TENANT_ID = os.getenv("DATACODE_TENANT_ID", "")

# Whether this deployment is the workspace's production line.
IS_PRODUCTION = DEPLOYMENT_TYPE == "production"

# Whether the daemon may run this deployment's schedules and sensors. Granted to
# production automatically; to any other environment only explicitly.
SCHEDULES_ENABLED = os.getenv("DATACODE_SCHEDULES_ENABLED", "") == "1"

# ── Scheduling gate ────────────────────────────────────────────────────
DEFAULT_SCHEDULE_STATUS = (
    dg.DefaultScheduleStatus.RUNNING
    if SCHEDULES_ENABLED
    else dg.DefaultScheduleStatus.STOPPED
)

DEFAULT_SENSOR_STATUS = (
    dg.DefaultSensorStatus.RUNNING
    if SCHEDULES_ENABLED
    else dg.DefaultSensorStatus.STOPPED
)


def run_tags() -> dict[str, str]:
    """Platform tags identifying this deployment, for runs launched from code.

    Runs launched through the platform API already carry these; use this helper
    when your own code launches a run (a sensor's ``RunRequest``) so it stays
    attributable to the environment that produced it.
    """
    tags = {
        "datacode/environment": ENVIRONMENT,
        "datacode/workspace_id": WORKSPACE_ID,
        "datacode/deployment": DEPLOYMENT_NAME,
    }
    return {key: value for key, value in tags.items() if value}
'''

_ASSETS_INIT_PY = '"""Asset definitions for workspace {{ workspace_name }}."""\n'

_SQL_ASSETS_PY = '''"""SQLMesh source assets for workspace {{ workspace_name }}.

Bridges SQLMesh models into the Dagster asset graph as external assets so the
Data+AI pipeline can depend on warehouse tables. Model metadata is fetched from
the workspace-runtime REST API at code-location load time.

Asset key convention: SQLMesh display name ``schema.table`` -> Dagster key
``["sqlmesh", "schema", "table"]``. The display name matches the ``name`` field
returned by ``/api/v1/sqlmesh/models`` so the Asset DAG and the SQLMesh UI stay
consistent. The full-qualified name (FQN) is only used internally to resolve the
display name via the lineage ``nodes`` metadata.

Auto-discovered by ``definitions.py``: the module-level ``sqlmesh_source_assets``
list is picked up as external assets.
"""

import os

import httpx
from dagster import AssetSpec

# Injected by the runtime when launching this code location. The default is
# this workspace-runtime itself (port 8002).
WORKSPACE_RUNTIME_URL = os.environ.get("WORKSPACE_RUNTIME_URL", "http://localhost:8002")
WORKSPACE_PATH = os.environ.get("DAGSTER_WORKSPACE_PATH", "")
SQLMESH_GATEWAY = os.environ.get("SQLMESH_GATEWAY", "")

SQLMESH_PREFIX = "sqlmesh"


def _name_to_key(name: str) -> list[str]:
    """Convert a SQLMesh display name ``schema.table`` into a Dagster asset key."""
    parts = [p.strip('"') for p in name.split(".") if p]
    return [SQLMESH_PREFIX, *parts]


def _fetch_sqlmesh_specs() -> list[AssetSpec]:
    """Fetch SQLMesh lineage and turn each model into an external AssetSpec.

    The lineage response carries both the adjacency ``graph`` (keyed by FQN) and
    a ``nodes`` metadata map (FQN -> {name, ...}). Asset keys are derived from the
    display ``name`` so they match the SQLMesh model list; when a name mapping is
    missing we fail soft and fall back to the FQN.
    """
    if not WORKSPACE_PATH:
        return []
    try:
        resp = httpx.get(
            f"{WORKSPACE_RUNTIME_URL.rstrip('/')}/api/v1/lineage",
            params={"workspace_path": WORKSPACE_PATH, "gateway": SQLMESH_GATEWAY},
            timeout=10.0,
        )
        resp.raise_for_status()
        payload = resp.json() or {}
    except httpx.HTTPError:
        # Fail soft: an unreachable runtime should not break code load.
        return []

    graph: dict = payload.get("graph", {}) or {}
    nodes: dict = payload.get("nodes", {}) or {}

    def _display_name(fqn: str) -> str:
        node = nodes.get(fqn) or {}
        return node.get("name") or fqn

    specs: list[AssetSpec] = []
    for fqn, upstreams in graph.items():
        specs.append(
            AssetSpec(
                key=_name_to_key(_display_name(fqn)),
                deps=[_name_to_key(_display_name(up)) for up in (upstreams or [])],
                group_name="sqlmesh",
                kinds={"sqlmesh"},
                description=f"SQLMesh model {_display_name(fqn)}",
            )
        )
    return specs


# Bare AssetSpecs passed to Definitions act as external (non-executable)
# assets; no wrapper helper is needed on Dagster >= 1.9.
sqlmesh_source_assets = _fetch_sqlmesh_specs()
'''

_DAFT_ASSETS_PY = '''"""Daft feature-engineering assets for workspace {{ workspace_name }}.

Example Data+AI asset showing how to compute features with Daft. Replace the
body with your own transforms; the pattern (return a materialized result and
emit metadata) stays the same.

Auto-discovered by ``definitions.py``: the top-level ``@asset`` is picked up
directly, no manual registration needed.
"""

from dagster import AssetExecutionContext, MaterializeResult, asset


@asset(group_name="daft", kinds={"daft"})
def example_features(context: AssetExecutionContext) -> MaterializeResult:
    """Compute example features with Daft.

    This is a scaffold: it builds a tiny in-memory DataFrame so the asset runs
    out of the box. Swap ``from_pydict`` for ``read_parquet`` / ``read_csv``
    against your warehouse outputs.
    """
    import daft

    df = daft.from_pydict({"id": [1, 2, 3], "value": [10.0, 20.0, 30.0]})
    df = df.with_column("value_scaled", df["value"] / df["value"].sum())
    df = df.collect()

    row_count = df.count_rows()
    context.log.info(f"example_features computed {row_count} rows")
    return MaterializeResult(metadata={"row_count": row_count})
'''

_RESOURCES_INIT_PY = '"""Resource definitions for workspace {{ workspace_name }}."""\n'

_EXAMPLE_RESOURCE_PY = '''"""Example resource wiring for workspace {{ workspace_name }}.

Resources are NOT auto-discovered as bare objects. To make a resource available
to assets/jobs, return it from a ``@dg.definitions`` function that yields a
``Definitions(resources={...})``; ``load_defs`` merges these into the top-level
Definitions.

Assets consume a resource by declaring a parameter with the resource type, e.g.::

    @dg.asset
    def my_asset(example_api: ExampleApiResource) -> None:
        data = example_api.fetch("/path")

Replace ``ExampleApiResource`` with your real resources (databases, clients...).
"""

import dagster as dg


class ExampleApiResource(dg.ConfigurableResource):
    """Minimal configurable resource scaffold.

    Configuration is validated by Dagster/Pydantic. Keep secrets in env vars and
    pull them via ``dg.EnvVar`` rather than hard-coding.
    """

    base_url: str = "http://localhost"

    def fetch(self, path: str) -> str:
        # Placeholder: implement real client logic here.
        return f"{self.base_url}{path}"


@dg.definitions
def resources() -> dg.Definitions:
    """Wire this workspace's resources into the auto-discovered Definitions."""
    return dg.Definitions(
        resources={
            "example_api": ExampleApiResource(),
        }
    )
'''

_JOBS_INIT_PY = '"""Job definitions for workspace {{ workspace_name }}."""\n'

_EXAMPLE_JOB_PY = '''"""Example asset job for workspace {{ workspace_name }}.

An asset job materializes a selection of assets together. This one targets the
Daft example asset; broaden the selection as your pipeline grows.

Auto-discovered by ``definitions.py``: the top-level job object is picked up
directly.
"""

import dagster as dg

# Materialize the Daft example feature asset. Use ``AssetSelection`` to widen or
# narrow the set (e.g. ``dg.AssetSelection.groups("daft")``).
example_job = dg.define_asset_job(
    name="example_job",
    selection=dg.AssetSelection.assets("example_features"),
)
'''

_SCHEDULES_INIT_PY = '"""Schedule definitions for workspace {{ workspace_name }}."""\n'

_EXAMPLE_SCHEDULE_PY = '''"""Example daily schedule for workspace {{ workspace_name }}.

Runs ``example_job`` once a day. The cron expression is interpreted in UTC
(``execution_timezone="UTC"``) to keep scheduling consistent with the backend's
UTC-only policy; presentation-layer timezone conversion happens in the UI.

``default_status`` comes from ``defs.deployment``: this schedule runs only in a
deployment the platform allows to be scheduled. Every environment loads this same
code from its own branch, so a hard-coded ``RUNNING`` would let a sandbox drive
production data on every tick.

Auto-discovered by ``definitions.py``: the top-level schedule object is picked
up directly.
"""

import dagster as dg

from defs.deployment import DEFAULT_SCHEDULE_STATUS
from defs.jobs.example_job import example_job

# 00:00 UTC every day. Adjust the cron string to your cadence.
daily_schedule = dg.ScheduleDefinition(
    name="daily_example_job",
    job=example_job,
    cron_schedule="0 0 * * *",
    execution_timezone="UTC",
    default_status=DEFAULT_SCHEDULE_STATUS,
)
'''

_SENSORS_INIT_PY = '"""Sensor definitions for workspace {{ workspace_name }}."""\n'

_EXAMPLE_SENSOR_PY = '''"""Example sensor for workspace {{ workspace_name }}.

Sensors poll for external conditions and can launch runs when they are met.
This scaffold always skips; replace the body with your trigger logic (e.g. a
new file in object storage, an upstream table refresh, a webhook cursor).

``default_status`` comes from ``defs.deployment`` for the same reason schedules
gate on it: a sensor in a branch deployment would otherwise launch runs against
production on its own.

Auto-discovered by ``definitions.py``: the top-level sensor object is picked up
directly.
"""

import dagster as dg

from defs.deployment import DEFAULT_SENSOR_STATUS
from defs.jobs.example_job import example_job


@dg.sensor(job=example_job, default_status=DEFAULT_SENSOR_STATUS)
def example_sensor(context: dg.SensorEvaluationContext) -> dg.SkipReason:
    """Skip by default; implement a real trigger condition here."""
    context.log.info("example_sensor evaluated (scaffold: always skips)")
    return dg.SkipReason("Scaffold sensor: no trigger condition implemented yet.")
'''

_TESTS_INIT_PY = '''"""Tests for workspace {{ workspace_name }}'s Dagster definitions.

This package is a SIBLING of ``defs/`` and is NOT part of auto-discovery.
"""
'''

_TEST_ASSETS_PY = '''"""Smoke tests for workspace {{ workspace_name }}'s definitions.

Run with ``pytest`` from the ``asset-dag`` directory. These validate that the
code location loads and that the example asset materializes.
"""

import dagster as dg

from defs.assets.daft_assets import example_features


def test_example_features_materializes() -> None:
    """The Daft example asset should materialize and report a row count."""
    result = dg.materialize([example_features])
    assert result.success

    materialization = result.asset_materializations_for_node("example_features")
    assert materialization, "expected an AssetMaterialization for example_features"
'''

# Relative path inside asset-dag/ -> file content template. Keep this in sync
# with src/codeloc/scaffold.py::_SCAFFOLD_FILES.
_SKELETON_FILES = {
    "definitions.py": _DEFINITIONS_PY,
    "pyproject.toml": _PYPROJECT_TOML,
    "README.md": _README_MD,
    # defs/ auto-discovery root
    os.path.join("defs", "__init__.py"): _DEFS_INIT_PY,
    os.path.join("defs", "deployment.py"): _DEPLOYMENT_PY,
    os.path.join("defs", "assets", "__init__.py"): _ASSETS_INIT_PY,
    os.path.join("defs", "assets", "sql_assets.py"): _SQL_ASSETS_PY,
    os.path.join("defs", "assets", "daft_assets.py"): _DAFT_ASSETS_PY,
    os.path.join("defs", "resources", "__init__.py"): _RESOURCES_INIT_PY,
    os.path.join("defs", "resources", "example_resource.py"): _EXAMPLE_RESOURCE_PY,
    os.path.join("defs", "jobs", "__init__.py"): _JOBS_INIT_PY,
    os.path.join("defs", "jobs", "example_job.py"): _EXAMPLE_JOB_PY,
    os.path.join("defs", "schedules", "__init__.py"): _SCHEDULES_INIT_PY,
    os.path.join("defs", "schedules", "example_schedule.py"): _EXAMPLE_SCHEDULE_PY,
    os.path.join("defs", "sensors", "__init__.py"): _SENSORS_INIT_PY,
    os.path.join("defs", "sensors", "example_sensor.py"): _EXAMPLE_SENSOR_PY,
    # tests/ (sibling of defs/, not auto-discovered)
    os.path.join("tests", "__init__.py"): _TESTS_INIT_PY,
    os.path.join("tests", "test_assets.py"): _TEST_ASSETS_PY,
}


def scaffold_asset_dag(path: str, workspace_name: str) -> None:
    """Write the canonical asset-dag skeleton into ``path``.

    Fill-missing only: existing files are never overwritten, so user edits
    survive re-scaffolding. The runtime performs the same fill-missing
    render from its own templates when a code location is ensured.

    Placeholder substitution is delegated to ``codeloc.scaffold`` so both paths
    produce identical files — in particular the same platform-provided
    dependency block, generated from the running image's baseline rather than
    written by hand in two places.
    """
    from src.codeloc.scaffold import render_placeholders

    os.makedirs(path, exist_ok=True)
    for rel_path, template in _SKELETON_FILES.items():
        file_path = os.path.join(path, rel_path)
        if os.path.exists(file_path):
            continue
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        content = render_placeholders(template, workspace_name)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
    logger.info("asset_dag_scaffolded", path=path, workspace=workspace_name)
