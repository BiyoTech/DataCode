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

from src.api.dagster.requests import DeploymentRequest, ReloadRequest
from src.api.dagster.responses import (
    DeploymentDiff,
    DeploymentInfo,
    OperationResult,
    ReloadJobInfo,
)
from src.api.deps import Caller
from src.codeloc.deployments import DeploymentSpec, get_deployment_manager
from src.codeloc.errors import ReloadJobNotFoundError
from src.codeloc.reload_jobs import get_reload_job_store
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


@router.post("/reload", response_model=ReloadJobInfo, status_code=202)
async def reload_deployment(req: ReloadRequest, caller: Caller) -> ReloadJobInfo:
    """Start a reload and return its job envelope immediately.

    A reload installs the working copy's declared Python packages before it starts
    the code location, so it can take minutes on a first install. Making it a job
    is what keeps that off the request path: progress arrives on the SSE
    ``deployment`` topic and the authoritative state is readable from
    ``/reload/status`` at any time — including after a dropped connection, a page
    refresh or a runtime restart.

    202, not 200: the work is accepted, not finished. A second request for the
    same working copy joins the running job (``attached=true``) instead of
    starting a competing one.
    """
    spec = _spec(req)
    job = await get_deployment_manager().start_reload_job(
        spec, source=req.source, actor_user_id=caller.user_id
    )
    return ReloadJobInfo(**job.to_dict())


@router.get("/reload/status", response_model=ReloadJobInfo)
async def reload_status(
    workspace_path: str = Query(
        ...,
        description=(
            "Environment working-copy path. REQUIRED: the runtime is a headless "
            "Service and only the replica holding this copy's lease knows its "
            "jobs, so the gateway routes on this value."
        ),
    ),
    job_id: str = Query(
        default="",
        description="Specific job. Omitted returns the copy's latest reload.",
    ),
) -> ReloadJobInfo:
    """The authoritative state of a reload.

    What a reconnecting tab, a refreshed page or a polling CLI reads. Answers from
    the working copy's own job records, so a terminal state survives the runtime
    that produced it.
    """
    from src.api.deps import validate_workspace_path

    resolved = validate_workspace_path(workspace_path)
    job = get_reload_job_store().find(resolved, job_id)
    if job is None:
        raise ReloadJobNotFoundError(job_id or "latest")
    return ReloadJobInfo(**job.to_dict())


@router.post("/reload/cancel", response_model=ReloadJobInfo)
async def cancel_reload(req: DeploymentRequest) -> ReloadJobInfo:
    """Cancel the reload running for a working copy.

    Marks the job cancelled and stops watching it. The install already in flight
    is left to finish or fail on its own: killing a pip mid-unpack is how a copy
    ends up with a tree that matches no fingerprint, and the next reload rebuilds
    it from scratch anyway.
    """
    from src.api.deps import validate_workspace_path

    resolved = validate_workspace_path(req.workspace_path)
    store = get_reload_job_store()
    job = store.latest(resolved)
    if job is None:
        raise ReloadJobNotFoundError("latest")
    cancelled = await store.cancel(job.job_id)
    return ReloadJobInfo(**(cancelled or job).to_dict())


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
