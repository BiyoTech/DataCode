"""Pipeline endpoints: code-location lifecycle + run triggering.

A "pipeline" here maps to a Dagster job/code-location. These endpoints manage
the per-workspace gRPC Code Location (ensure/reload) and launch runs.
"""

from fastapi import APIRouter

from src.api.deps import WorkspacePath
from src.api.models.requests import (
    EnsureCodeLocationRequest,
    ReloadCodeLocationRequest,
    TriggerRunRequest,
)
from src.api.models.responses import CodeLocationInfo, OperationResult, RunInfo
from src.core.code_location_manager import get_code_location_manager
from src.core.run_manager import get_run_manager
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/dagster/pipelines", tags=["pipelines"])


@router.post("/code-location/ensure", response_model=CodeLocationInfo)
async def ensure_code_location(req: EnsureCodeLocationRequest) -> CodeLocationInfo:
    """Ensure the gRPC Code Location for a workspace is running."""
    # Validate path defensively (routes accept it in the body, not as a dep).
    from src.api.deps import validate_workspace_path

    workspace_path = validate_workspace_path(req.workspace_path)
    result = await get_code_location_manager().ensure_location(workspace_path)
    return CodeLocationInfo(**result)


@router.post("/code-location/reload", response_model=OperationResult)
async def reload_code_location(req: ReloadCodeLocationRequest) -> OperationResult:
    """Reload a workspace's Code Location after user code changes."""
    from src.api.deps import validate_workspace_path

    workspace_path = validate_workspace_path(req.workspace_path)
    result = await get_code_location_manager().reload_location(workspace_path)
    return OperationResult(success=True, detail=result)


@router.get("/code-locations", response_model=list[CodeLocationInfo])
async def list_code_locations() -> list[CodeLocationInfo]:
    """List all active code locations managed by this service."""
    locations = get_code_location_manager().list_locations()
    return [
        CodeLocationInfo(
            location_name=loc["location_name"],
            host="",  # host is uniform; omitted here to keep the list compact
            port=loc["port"],
        )
        for loc in locations
    ]


@router.post("/run", response_model=RunInfo)
async def trigger_run(req: TriggerRunRequest) -> RunInfo:
    """Launch a job run for a workspace's code location."""
    from src.api.deps import validate_workspace_path

    validate_workspace_path(req.workspace_path)
    location_name = get_code_location_manager().location_name_for(req.workspace_path)
    result = await get_run_manager().launch(
        repository_location_name=location_name,
        repository_name=req.repository_name,
        job_name=req.job_name,
        run_config=req.run_config,
    )
    return RunInfo(run_id=result["run_id"], status=result["status"], job_name=req.job_name)
