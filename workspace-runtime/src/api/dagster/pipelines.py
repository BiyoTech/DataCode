"""Pipeline endpoints: launching runs in an environment's deployment.

Code location lifecycle moved to ``/api/v1/dagster/deployments/*`` (design §8.4);
what remains here is triggering a job run, which is addressed by the working copy
the caller is looking at.
"""

from fastapi import APIRouter

from src.api.dagster.requests import TriggerRunRequest
from src.api.dagster.responses import RunInfo
from src.codeloc.deployments import get_deployment_manager
from src.codeloc.runs import get_run_manager
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/dagster/pipelines", tags=["pipelines"])


@router.post("/run", response_model=RunInfo)
async def trigger_run(req: TriggerRunRequest) -> RunInfo:
    """Launch a job run in the deployment backing this working copy.

    The deployment is ensured first: a run submitted to an unloaded code location
    fails with a repository resolution error that says nothing about the cause.
    Every run carries the deployment's tags, which is what makes an execution
    answerable for the environment that produced it — the open-source Dagster
    instance shares one run store across all deployments (design §7.3).
    """
    from src.api.deps import validate_workspace_path

    workspace_path = validate_workspace_path(req.workspace_path)
    manager = get_deployment_manager()
    spec = manager.resolve(workspace_path)
    await manager.ensure(spec)

    result = await get_run_manager().launch(
        repository_location_name=spec.deployment_name,
        repository_name=req.repository_name,
        job_name=req.job_name,
        run_config=req.run_config,
        tags=spec.run_tags(),
    )
    return RunInfo(run_id=result["run_id"], status=result["status"], job_name=req.job_name)
