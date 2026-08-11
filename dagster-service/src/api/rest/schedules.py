"""Schedule endpoints: list schedules for a workspace's code location."""

from fastapi import APIRouter, Query

from src.api.deps import WorkspacePath
from src.api.models.responses import ScheduleInfo
from src.core.code_location_manager import get_code_location_manager
from src.core.dagster_client import get_dagster_client

router = APIRouter(prefix="/api/v1/dagster/schedules", tags=["schedules"])


@router.get("", response_model=list[ScheduleInfo])
async def list_schedules(
    workspace_path: WorkspacePath,
    repository_name: str = Query("__repository__", description="Repository name"),
) -> list[ScheduleInfo]:
    """List schedules for a workspace's code location."""
    location_name = get_code_location_manager().location_name_for(workspace_path)
    results = await get_dagster_client().list_schedules(location_name, repository_name)
    return [
        ScheduleInfo(
            name=s.get("name"),
            cron_schedule=s.get("cronSchedule"),
            pipeline_name=s.get("pipelineName"),
            status=(s.get("scheduleState") or {}).get("status"),
        )
        for s in results
    ]
