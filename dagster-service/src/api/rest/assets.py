"""Asset endpoints: list assets + build the unified asset DAG graph.

The DAG merges native Dagster assets (from the code location, via GraphQL) with
SQLMesh source assets (from the bridge) so the frontend renders one graph.
"""

from fastapi import APIRouter, Query

from src.api.deps import WorkspacePath
from src.api.models.responses import AssetDagGraph, AssetInfo
from src.bridge.sqlmesh_bridge import SQLMESH_ASSET_PREFIX
from src.core.code_location_manager import get_code_location_manager
from src.core.dagster_client import get_dagster_client
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/dagster/assets", tags=["assets"])


def _key_str(path: list[str]) -> str:
    return "__".join(path)


def _display_name(key_path: list[str]) -> str:
    """Compute a human-friendly display name for an asset key path.

    SQLMesh-bridged assets carry the ``sqlmesh`` prefix (key path
    ``["sqlmesh", "schema", "table"]``); their display name is the SQLMesh model
    name ``schema.table`` matching /api/v1/sqlmesh/models. Native Dagster assets
    have no such prefix, so we fall back to the joined asset key string.
    """
    if key_path and key_path[0] == SQLMESH_ASSET_PREFIX:
        return ".".join(key_path[1:]) or _key_str(key_path)
    return _key_str(key_path)


async def _location_assets(workspace_path: str, repository_name: str) -> list[dict]:
    """Fetch asset nodes scoped to a single workspace's code location.

    Ensuring the location first is idempotent (no respawn when already alive)
    and guarantees the webserver knows this workspace's repository before we
    query it — otherwise a freshly switched-to workspace would resolve to
    ``RepositoryNotFoundError`` and render as empty.
    """
    location_name = get_code_location_manager().location_name_for(workspace_path)
    await get_code_location_manager().ensure_location(workspace_path)
    return await get_dagster_client().list_assets(location_name, repository_name)


@router.get("", response_model=list[AssetInfo])
async def list_assets(
    workspace_path: WorkspacePath,
    repository_name: str = Query("__repository__", description="Repository name"),
) -> list[AssetInfo]:
    """List asset nodes for the given workspace's code location."""
    nodes = await _location_assets(workspace_path, repository_name)
    result: list[AssetInfo] = []
    for node in nodes:
        key_path = node.get("assetKey", {}).get("path", [])
        result.append(
            AssetInfo(
                asset_key=key_path,
                asset_key_str=_key_str(key_path),
                name=_display_name(key_path),
                group_name=node.get("groupName"),
                description=node.get("description"),
                deps=[_key_str(d.get("path", [])) for d in node.get("dependencyKeys", [])],
            )
        )
    return result


@router.get("/graph", response_model=AssetDagGraph)
async def asset_graph(
    workspace_path: WorkspacePath,
    repository_name: str = Query("__repository__", description="Repository name"),
    environment: str = Query(
        "", description="SQLMesh virtual environment (prod/dev/…) the view is scoped to"
    ),
) -> AssetDagGraph:
    """Return the asset dependency graph (nodes + edges) for one workspace.

    The graph is scoped to ``workspace_path``'s code location so switching the
    active workspace in the UI yields that workspace's own assets rather than a
    shared instance-wide graph.

    ``environment`` names the selected SQLMesh virtual environment. Native
    Dagster assets are defined at the code-location level and do not vary by
    environment; the SQLMesh-bridged lineage subgraph (fetched lazily by the
    frontend from /api/v1/sqlmesh/lineage) is what actually reflects the chosen
    environment. The parameter is accepted here so the contract is explicit and
    the scope is captured in logs.
    """
    nodes = await _location_assets(workspace_path, repository_name)

    graph_nodes: list[AssetInfo] = []
    edges: list[dict[str, str]] = []
    for node in nodes:
        key_path = node.get("assetKey", {}).get("path", [])
        key_str = _key_str(key_path)
        deps = [_key_str(d.get("path", [])) for d in node.get("dependencyKeys", [])]
        graph_nodes.append(
            AssetInfo(
                asset_key=key_path,
                asset_key_str=key_str,
                name=_display_name(key_path),
                group_name=node.get("groupName"),
                description=node.get("description"),
                deps=deps,
            )
        )
        for dep in deps:
            edges.append({"source": dep, "target": key_str})

    logger.info(
        "asset_graph_built",
        workspace_path=workspace_path,
        environment=environment or "local",
        nodes=len(graph_nodes),
        edges=len(edges),
    )
    return AssetDagGraph(nodes=graph_nodes, edges=edges)
