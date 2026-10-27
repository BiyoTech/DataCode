"""Health + mode info endpoints."""

from fastapi import APIRouter

from src.api.dagster.responses import ModeInfo
from src.codeloc.manager import get_code_location_manager
from src.codeloc.process import get_process_manager
from src.config import settings

router = APIRouter(prefix="/api/v1/dagster", tags=["health"])


@router.get("/health")
async def health():
    """Health check exposing mode, per-subprocess status and registration lag.

    ``pending_registrations`` is the remote-mode signal that matters most: a code
    location running here that the control plane has not confirmed serves stale
    (or no) assets to users, and nothing else in the system would report it.
    """
    return {
        "status": "healthy",
        "service": "workspace-runtime",
        "dagster_mode": settings.dagster_mode,
        "processes": get_process_manager().health_check()["processes"],
        "pending_registrations": get_code_location_manager().pending_registrations(),
    }


@router.get("/mode", response_model=ModeInfo)
async def mode_info() -> ModeInfo:
    """Return the active Dagster mode and endpoint configuration."""
    return ModeInfo(
        mode=settings.dagster_mode,
        graphql_url=settings.active_graphql_url,
        code_location_host=settings.code_location_host,
        code_location_port=settings.code_location_port,
    )
