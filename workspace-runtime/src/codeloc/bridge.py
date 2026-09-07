"""SQLMesh -> Dagster asset bridge.

Maps SQLMesh models onto Dagster asset keys so the Asset DAG presents a
single, unified lineage graph spanning both the SQL warehouse (SQLMesh
models) and the Data+AI pipeline (Dagster assets).

P3 NOTE: this used to call the sqlmesh-service over HTTP, creating a
service-to-service cycle (sqlmesh-service -> dagster-service -> sqlmesh-service).
After the merge both sides live in workspace-runtime, so the calls below are
plain in-process service invocations — the cycle is gone by construction.

Mapping convention: a SQLMesh model display name such as ``marts.orders``
becomes the Dagster asset key path ``["sqlmesh", "marts", "orders"]`` (prefixed
so it never collides with native Dagster assets). The display name matches the
``name`` field returned by the model service and is resolved from the lineage
``nodes`` metadata (FQN -> name). Downstream Dagster assets reference these as
upstream ``SourceAsset`` / ``AssetSpec`` dependencies.
"""

from __future__ import annotations

from typing import Any, Optional

from src.config import Settings, settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Prefix applied to every SQLMesh-derived Dagster asset key.
SQLMESH_ASSET_PREFIX = "sqlmesh"


def model_name_to_asset_key(name: str) -> list[str]:
    """Convert a SQLMesh model display name into a prefixed Dagster asset key path."""
    # Strip quotes SQLMesh sometimes adds around identifiers, split on dots.
    parts = [p.strip('"') for p in name.split(".") if p]
    return [SQLMESH_ASSET_PREFIX, *parts]


def asset_key_to_str(key_path: list[str]) -> str:
    """Join an asset key path into the ``a__b__c`` form used by Dagster URLs."""
    return "__".join(key_path)


class SQLMeshBridge:
    """Reads SQLMesh metadata IN-PROCESS and maps it to Dagster asset specs."""

    def __init__(self, cfg: Optional[Settings] = None):
        self._settings = cfg or settings

    async def fetch_models(
        self, workspace_path: str, gateway: str = "", environment: str = ""
    ) -> dict[str, Any]:
        """Fetch the SQLMesh model list for a workspace / virtual environment."""
        from src.sqlmesh.model_service import get_model_service

        return await get_model_service().list_models(
            workspace_path=workspace_path,
            gateway=gateway or None,
            environment=environment or None,
        )

    async def fetch_lineage(
        self,
        workspace_path: str,
        model_name: str = "",
        gateway: str = "",
        environment: str = "",
    ) -> dict[str, Any]:
        """Fetch the SQLMesh model lineage adjacency graph for a workspace / env."""
        from src.sqlmesh.lineage_service import get_lineage_service

        return await get_lineage_service().get_lineage(
            workspace_path=workspace_path,
            model_name=model_name,
            gateway=gateway or None,
            environment=environment or None,
        )

    async def build_source_asset_specs(
        self, workspace_path: str, gateway: str = "", environment: str = ""
    ) -> list[dict[str, Any]]:
        """Build Dagster SourceAsset-like specs from SQLMesh models.

        Returns a list of plain dicts (transport-friendly for the REST layer and
        the frontend DAG). Each spec carries the Dagster asset key, upstream
        dependency keys, and a ``kind`` marker so the UI can style SQLMesh nodes
        distinctly. The actual ``SourceAsset`` objects are constructed inside the
        user's code location from these specs (see templates/).

        Asset keys are derived from each model's display ``name`` (matching
        ``/api/v1/sqlmesh/models``). The name is resolved from the lineage
        ``nodes`` metadata (FQN -> name); a missing mapping fails soft to the FQN.
        The lineage is scoped to ``environment`` (the selected SQLMesh virtual
        environment) so the built graph reflects that environment's snapshots.
        """
        lineage = await self.fetch_lineage(
            workspace_path, gateway=gateway, environment=environment
        )
        # sqlmesh-service returns {"graph": {fqn: [upstream_fqn, ...]},
        #                          "nodes": {fqn: {"name", ...}}}.
        payload: dict[str, Any] = lineage if isinstance(lineage, dict) else {}
        graph: dict[str, list[str]] = payload.get("graph", {}) or {}
        nodes: dict[str, dict[str, Any]] = payload.get("nodes", {}) or {}

        def _display_name(fqn: str) -> str:
            node = nodes.get(fqn) or {}
            return node.get("name") or fqn

        specs: list[dict[str, Any]] = []
        for fqn, upstreams in graph.items():
            name = _display_name(fqn)
            key_path = model_name_to_asset_key(name)
            specs.append(
                {
                    "asset_key": key_path,
                    "asset_key_str": asset_key_to_str(key_path),
                    "name": name,
                    "fqn": fqn,
                    "kind": SQLMESH_ASSET_PREFIX,
                    "deps": [
                        asset_key_to_str(model_name_to_asset_key(_display_name(up)))
                        for up in (upstreams or [])
                    ],
                }
            )
        logger.info(
            "sqlmesh_source_assets_built",
            workspace_path=workspace_path,
            count=len(specs),
        )
        return specs


_bridge: Optional[SQLMeshBridge] = None


def get_sqlmesh_bridge() -> SQLMeshBridge:
    """Return the process-wide SQLMesh bridge singleton."""
    global _bridge
    if _bridge is None:
        _bridge = SQLMeshBridge()
    return _bridge
