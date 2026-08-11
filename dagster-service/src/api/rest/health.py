"""Health + mode info endpoints."""

from fastapi import APIRouter

from src.api.models.responses import ModeInfo
from src.config import settings
from src.core.process_manager import get_process_manager

router = APIRouter(prefix="/api/v1/dagster", tags=["health"])


@router.get("/health")
async def health():
    """Health check exposing mode + per-subprocess status."""
    return {
        "status": "healthy",
        "service": "dagster-service",
        "dagster_mode": settings.dagster_mode,
        "processes": get_process_manager().health_check()["processes"],
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
