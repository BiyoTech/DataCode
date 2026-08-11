"""Run endpoints: status query, logs, cancellation."""

from fastapi import APIRouter

from src.api.models.responses import RunInfo, RunLogEvent
from src.core.run_manager import get_run_manager

router = APIRouter(prefix="/api/v1/dagster/runs", tags=["runs"])


@router.get("/{run_id}", response_model=RunInfo)
async def get_run(run_id: str) -> RunInfo:
    """Return status metadata for a run."""
    status = await get_run_manager().get_status(run_id)
    return RunInfo(
        run_id=status["run_id"],
        status=status["status"],
        start_time=status.get("start_time"),
        end_time=status.get("end_time"),
        job_name=status.get("job_name"),
    )


@router.get("/{run_id}/logs", response_model=list[RunLogEvent])
async def get_run_logs(run_id: str) -> list[RunLogEvent]:
    """Return structured log events for a run."""
    events = await get_run_manager().get_logs(run_id)
    return [
        RunLogEvent(
            message=e.get("message"),
            timestamp=e.get("timestamp"),
            level=e.get("level"),
            step_key=e.get("stepKey"),
        )
        for e in events
    ]


@router.post("/{run_id}/cancel", response_model=RunInfo)
async def cancel_run(run_id: str) -> RunInfo:
    """Terminate a run."""
    result = await get_run_manager().cancel(run_id)
    return RunInfo(run_id=result["run_id"], status=result["status"])
