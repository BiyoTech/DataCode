"""Schedule endpoints: list the schedules of an environment's deployment.

The listed ``status`` is the instance-level schedule state. In a non-production
deployment it is STOPPED unless the environment was explicitly granted
scheduling: the scaffold gates ``default_status`` on the deployment identity
injected by dagster-service, so a branch deployment loading the very same code
does not drive production data on every tick (design §7.2).
"""

from fastapi import APIRouter, Query

from src.api.dagster.responses import ScheduleInfo
from src.api.deps import WorkspacePath
from src.codeloc.client import get_dagster_client
from src.codeloc.deployments import get_deployment_manager

router = APIRouter(prefix="/api/v1/dagster/schedules", tags=["schedules"])


@router.get("", response_model=list[ScheduleInfo])
async def list_schedules(
    workspace_path: WorkspacePath,
    repository_name: str = Query("__repository__", description="Repository name"),
) -> list[ScheduleInfo]:
    """List schedules for the deployment backing this working copy."""
    deployment_name = get_deployment_manager().name_for(workspace_path)
    results = await get_dagster_client().list_schedules(deployment_name, repository_name)
    return [
        ScheduleInfo(
            name=s.get("name"),
            cron_schedule=s.get("cronSchedule"),
            pipeline_name=s.get("pipelineName"),
            status=(s.get("scheduleState") or {}).get("status"),
        )
        for s in results
    ]
