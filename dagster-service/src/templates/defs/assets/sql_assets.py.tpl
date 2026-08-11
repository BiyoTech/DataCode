"""SQLMesh source assets for workspace {{ workspace_name }}.

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
