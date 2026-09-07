"""Deployment endpoints — the Dagster side of a platform environment.

One environment = one Git branch = one SQLMesh virtual environment = one Dagster
deployment. These endpoints are how the environment registry (and, until the
registry drives everything, the UI) brings a deployment up, refreshes it after a
code change and tears it down.

They replace the former ``/pipelines/code-location/{ensure,reload}``: a code
location is an implementation detail of a deployment, and naming it from the
working-copy path produced names that carried neither the workspace nor the
environment (design §7.1, §8.4).

Reloading is deliberately an EXPLICIT action. Nothing here watches the
filesystem: a code change becomes live through an audited action (apply,
promote, or a user-triggered refresh), never because a file was saved.
"""

from fastapi import APIRouter, Query

from src.api.dagster.requests import DeploymentRequest
from src.api.dagster.responses import DeploymentDiff, DeploymentInfo, OperationResult
from src.codeloc.deployments import DeploymentSpec, get_deployment_manager
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/dagster/deployments", tags=["deployments"])


def _spec(req: DeploymentRequest) -> DeploymentSpec:
    """Validate the working copy and derive the deployment identity + policy."""
    from src.api.deps import validate_workspace_path

    workspace_path = validate_workspace_path(req.workspace_path)
    return get_deployment_manager().resolve(
        workspace_path,
        environment=req.environment,
        env_type=req.env_type,
        schedules_enabled=req.schedules_enabled,
    )


@router.post("/ensure", response_model=DeploymentInfo)
async def ensure_deployment(req: DeploymentRequest) -> DeploymentInfo:
    """Idempotently make an environment's deployment ready to serve.

    Safe to call on every page load: an already-running code location is not
    respawned (which would kill in-flight queries), only its policy is refreshed.
    """
    info = await get_deployment_manager().ensure(_spec(req))
    return DeploymentInfo(**info.as_dict())


@router.post("/reload", response_model=DeploymentInfo)
async def reload_deployment(req: DeploymentRequest) -> DeploymentInfo:
    """Re-import a deployment's code after its working copy changed."""
    info = await get_deployment_manager().reload(_spec(req))
    return DeploymentInfo(**info.as_dict())


@router.get("", response_model=list[DeploymentInfo])
async def list_deployments() -> list[DeploymentInfo]:
    """List the deployments this service serves.

    The registry reconciles against this list: a deployment it expects and does
    not find here is missing, one it archived and finds here is stale.
    """
    return [DeploymentInfo(**info.as_dict()) for info in get_deployment_manager().list()]


@router.get("/diff", response_model=DeploymentDiff)
async def diff_deployments(
    deployment: str = Query(..., description="Deployment whose changes are inspected"),
    base: str = Query(..., description="Deployment to compare against (usually production)"),
) -> DeploymentDiff:
    """Compare two deployments' asset and schedule definitions.

    The orchestration half of an environment diff: what the branch's code changes
    in the asset graph, independent of what SQLMesh changes in the data. Both
    deployments must be loaded — a definition set can only be read from a running
    code location, and assuming an empty one would report every asset as removed.
    """
    result = await get_deployment_manager().diff(
        deployment_name=deployment, base_deployment_name=base
    )
    return DeploymentDiff(**result)


@router.delete("/{deployment_name}", response_model=OperationResult)
async def destroy_deployment(deployment_name: str) -> OperationResult:
    """Stop a deployment and drop it from the Dagster instance.

    Reports whether a code location was actually running, so the environment
    destroy orchestration can record an honest partial outcome instead of
    claiming a cleanup it did not perform.
    """
    stopped = await get_deployment_manager().destroy(deployment_name)
    return OperationResult(
        success=True,
        detail={"deployment_name": deployment_name, "code_location_stopped": stopped},
    )
