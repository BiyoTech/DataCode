"""Asset endpoints: list assets + build the unified asset DAG graph.

The DAG merges native Dagster assets (from the code location, via GraphQL) with
SQLMesh source assets (from the bridge) so the frontend renders one graph.
"""

from fastapi import APIRouter, Query

from src.api.dagster.responses import AssetDagGraph, AssetInfo
from src.api.deps import WorkspacePath
from src.codeloc.bridge import SQLMESH_ASSET_PREFIX
from src.codeloc.client import get_dagster_client
from src.codeloc.deployments import DeploymentSpec, get_deployment_manager
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


async def _deployment_assets(
    workspace_path: str, repository_name: str
) -> tuple[DeploymentSpec, list[dict]]:
    """Ensure this working copy's deployment and fetch its asset nodes.

    Ensuring first is idempotent (no respawn when already alive) and guarantees
    the webserver knows this environment's repository before we query it —
    otherwise a freshly opened environment would resolve to
    ``RepositoryNotFoundError`` and render as empty.
    """
    manager = get_deployment_manager()
    spec = manager.resolve(workspace_path)
    await manager.ensure(spec)
    nodes = await get_dagster_client().list_assets(spec.deployment_name, repository_name)
    return spec, nodes


@router.get("", response_model=list[AssetInfo])
async def list_assets(
    workspace_path: WorkspacePath,
    repository_name: str = Query("__repository__", description="Repository name"),
) -> list[AssetInfo]:
    """List asset nodes for the deployment backing this working copy."""
    _, nodes = await _deployment_assets(workspace_path, repository_name)
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
        "", description="Environment the view is scoped to (defaults to the working copy's)"
    ),
) -> AssetDagGraph:
    """Return the asset dependency graph (nodes + edges) for one environment.

    The graph is scoped to the deployment of ``workspace_path``. Since a working
    copy is per-environment and each environment has its own deployment loading
    its own branch, native Dagster assets now DO vary by environment — the graph
    is the branch's own orchestration view, not a shared instance-wide one.

    ``environment`` is accepted so the requested scope is explicit and captured
    in logs; the working copy remains authoritative, and the SQLMesh-bridged
    lineage subgraph the frontend fetches lazily reflects the same environment.
    """
    spec, nodes = await _deployment_assets(workspace_path, repository_name)

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
        deployment=spec.deployment_name,
        environment=spec.environment,
        requested_environment=environment or spec.environment,
        nodes=len(graph_nodes),
        edges=len(edges),
    )
    return AssetDagGraph(nodes=graph_nodes, edges=edges)
