"""Canonical asset-dag (Dagster code location) skeleton.

Modern Dagster ``dg`` auto-discovery layout: the ``asset-dag/`` directory is the
code location's working directory (NOT a Python package, so its hyphenated name
is safe). ``definitions.py`` is the single entrypoint; it calls
``load_defs(defs_root=defs)`` to auto-discover everything under the importable
``defs/`` package. Tests live in a sibling ``tests/`` package and are NOT part
of auto-discovery.

Inside modules, use importable absolute imports (``from defs.jobs.example_job
import ...``); never import the ``asset-dag`` directory.

NOTE: keep this skeleton in sync with dagster-service/src/templates/, which
re-renders any missing file when a code location is ensured. The inline
constants below MUST match those templates verbatim.
"""

import os

from src.utils.logging import get_logger

logger = get_logger(__name__)

_WORKSPACE_PLACEHOLDER = "{{ workspace_name }}"

_DEFINITIONS_PY = '''"""Dagster Definitions entrypoint for workspace {{ workspace_name }}.

Loaded by the dagster-service Code Location gRPC server. This is the single
entrypoint: it auto-discovers every definition under the ``defs/`` package
(assets, jobs, schedules, sensors) and assembles them into one ``Definitions``.

Auto-discovery rules (Dagster ``dg`` convention):
- Top-level ``@asset`` / job / schedule / sensor objects in any ``defs/`` module
  are discovered automatically.
- Resources are NOT auto-discovered as bare objects; wire them by returning a
  ``Definitions(resources={...})`` from a ``@dg.definitions`` function (see
  ``defs/resources/example_resource.py``).

NOTE: SQLMESH_SERVICE_URL / DAGSTER_WORKSPACE_PATH / SQLMESH_GATEWAY are injected
as env vars by the dagster-service when it launches this code location.
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

_PYPROJECT_TOML = """[project]
name = "asset-dag"
version = "0.1.0"
description = "Data+AI pipeline (Dagster + Daft) for workspace {{ workspace_name }}"
requires-python = ">=3.11"
dependencies = [
    "dagster>=1.8.0",
    "getdaft>=0.3.0",
    "httpx>=0.27.0",
]

[tool.dagster]
module_name = "definitions"
"""

_README_MD = """# asset-dag — Dagster code location for workspace {{ workspace_name }}

This is the Dagster project for your Data+AI pipeline. It follows the modern
Dagster (`dg`) layout: a single `definitions.py` entrypoint auto-discovers all
definitions under the `defs/` package.

## Layout

```
asset-dag/
├── definitions.py        # entrypoint: auto-discovers defs/ into Definitions
├── pyproject.toml        # project metadata; [tool.dagster] module_name
├── defs/                 # ALL definitions live here (auto-discovery root)
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
- **Resources are wired explicitly.** Auto-discovery does not pick up bare
  resource objects; return them from a `@dg.definitions` function that yields a
  `Definitions(resources={...})` (see `defs/resources/example_resource.py`).
- **Use importable absolute imports** inside modules, e.g.
  `from defs.resources.example_resource import ...`. Never import the
  `asset-dag` directory (its hyphenated name is not a valid Python package).
- **Keep tests in `tests/`, not in `defs/`.** Modules under `defs/` are treated
  as definitions; test modules must stay out of the auto-discovery root.

## Running

The dagster-service launches this as a gRPC code location with:

```
dagster api grpc -f definitions.py --location-name <name>
```

with `cwd` set to this directory, so `import defs` resolves.
"""

_DEFS_INIT_PY = '''"""Auto-discovery root for workspace {{ workspace_name }}'s Dagster definitions.

Every module under this package is scanned by ``load_defs`` in ``definitions.py``.
Organize definitions by concept: assets/, resources/, jobs/, schedules/, sensors/.
"""
'''

_ASSETS_INIT_PY = '"""Asset definitions for workspace {{ workspace_name }}."""\n'

_SQL_ASSETS_PY = '''"""SQLMesh source assets for workspace {{ workspace_name }}.

Bridges SQLMesh models into the Dagster asset graph as external assets so the
Data+AI pipeline can depend on warehouse tables. Model metadata is fetched from
the sqlmesh-service REST API at code-location load time.

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

# Injected by dagster-service when launching this code location.
SQLMESH_SERVICE_URL = os.environ.get("SQLMESH_SERVICE_URL", "http://localhost:8002")
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
            f"{SQLMESH_SERVICE_URL.rstrip('/')}/api/v1/lineage",
            params={"workspace_path": WORKSPACE_PATH, "gateway": SQLMESH_GATEWAY},
            timeout=10.0,
        )
        resp.raise_for_status()
        payload = resp.json() or {}
    except httpx.HTTPError:
        # Fail soft: an unreachable sqlmesh-service should not break code load.
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

Auto-discovered by ``definitions.py``: the top-level schedule object is picked
up directly.
"""

import dagster as dg

from defs.jobs.example_job import example_job

# 00:00 UTC every day. Adjust the cron string to your cadence.
daily_schedule = dg.ScheduleDefinition(
    name="daily_example_job",
    job=example_job,
    cron_schedule="0 0 * * *",
    execution_timezone="UTC",
    default_status=dg.DefaultScheduleStatus.STOPPED,
)
'''

_SENSORS_INIT_PY = '"""Sensor definitions for workspace {{ workspace_name }}."""\n'

_EXAMPLE_SENSOR_PY = '''"""Example sensor for workspace {{ workspace_name }}.

Sensors poll for external conditions and can launch runs when they are met.
This scaffold always skips; replace the body with your trigger logic (e.g. a
new file in object storage, an upstream table refresh, a webhook cursor).

Auto-discovered by ``definitions.py``: the top-level sensor object is picked up
directly.
"""

import dagster as dg

from defs.jobs.example_job import example_job


@dg.sensor(job=example_job, default_status=dg.DefaultSensorStatus.STOPPED)
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
# with dagster-service/src/core/scaffold.py::_SCAFFOLD_FILES.
_SKELETON_FILES = {
    "definitions.py": _DEFINITIONS_PY,
    "pyproject.toml": _PYPROJECT_TOML,
    "README.md": _README_MD,
    # defs/ auto-discovery root
    os.path.join("defs", "__init__.py"): _DEFS_INIT_PY,
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
    survive re-scaffolding. The dagster-service performs the same fill-missing
    render from its own templates when a code location is ensured.
    """
    os.makedirs(path, exist_ok=True)
    for rel_path, template in _SKELETON_FILES.items():
        file_path = os.path.join(path, rel_path)
        if os.path.exists(file_path):
            continue
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        content = template.replace(_WORKSPACE_PLACEHOLDER, workspace_name)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
    logger.info("asset_dag_scaffolded", path=path, workspace=workspace_name)
