"""Dagster Service entry point.

Thin FastAPI application shell that:
- Registers global exception handlers (unified ``{error_code, message, detail}``)
- On startup, boots the Dagster subprocesses appropriate for the active mode:
    * local  -> daemon + webserver + Code Location gRPC server
    * remote  -> Code Location gRPC server only (registered to remote instance)
- On shutdown, gracefully terminates all managed subprocesses
- Provides a health endpoint exposing mode + per-process status
"""

# Disable Dagster telemetry BEFORE any dagster import to avoid outbound calls.
import os

os.environ.setdefault("DAGSTER_TELEMETRY_ENABLED", "false")

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.config import settings
from src.utils.errors import ErrorCode, ServiceError
from src.utils.logging import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)


def _register_exception_handlers(application: FastAPI) -> None:
    """Register global handlers so every API response shares the same shape
    ``{error_code, message, detail?}`` (see sqlmesh-service for rationale)."""

    @application.exception_handler(ServiceError)
    async def _service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_response_dict())

    @application.exception_handler(HTTPException)
    async def _http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        message = detail if isinstance(detail, str) and detail else "HTTP error"
        return JSONResponse(
            status_code=exc.status_code,
            content={"error_code": ErrorCode.INTERNAL_ERROR.value, "message": message},
        )

    @application.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error_code": ErrorCode.INVALID_REQUEST.value,
                "message": "Request validation failed",
                "detail": exc.errors(),
            },
        )

    @application.exception_handler(Exception)
    async def _unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", error=str(exc), exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error_code": ErrorCode.INTERNAL_ERROR.value,
                "message": "An unexpected error occurred",
                "detail": str(exc).strip() or exc.__class__.__name__,
            },
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: start Dagster subprocesses on boot, stop on exit.

    The process manager decides which subprocesses to run based on the active
    mode. Startup failures are logged but do not crash the API server, so the
    health endpoint can still report the degraded state.
    """
    # Lazy import keeps the module importable before core lands and avoids a
    # heavy dagster import at definition time.
    from src.core.process_manager import get_process_manager

    manager = get_process_manager()
    try:
        await manager.start()
    except Exception as e:  # noqa: BLE001 - surface via health, do not crash boot
        logger.error("dagster_service_startup_failed", error=str(e), exc_info=True)

    logger.info(
        "dagster_service_started",
        rest_port=settings.rest_port,
        dagster_mode=settings.dagster_mode,
        graphql_url=settings.active_graphql_url,
    )

    yield

    await manager.stop_all()
    logger.info("dagster_service_stopped")


app = FastAPI(
    title="Dagster Service",
    description="Data+AI pipeline orchestration (local/remote dual-mode)",
    version="0.1.0",
    lifespan=lifespan,
)
_register_exception_handlers(app)

from src.api.middleware import LogContextMiddleware  # noqa: E402

app.add_middleware(LogContextMiddleware)

# Register REST route modules.
from src.api.rest import assets, deployments, health, pipelines, runs, schedules  # noqa: E402

app.include_router(health.router)
app.include_router(deployments.router)
app.include_router(pipelines.router)
app.include_router(assets.router)
app.include_router(runs.router)
app.include_router(schedules.router)


@app.get("/health")
async def health_check():
    """Root health check (mirrors /api/v1/dagster/health for infra probes)."""
    from src.core.process_manager import get_process_manager

    return {
        "status": "healthy",
        "service": "dagster-service",
        "dagster_mode": settings.dagster_mode,
        "processes": get_process_manager().health_check()["processes"],
    }


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.rest_port,
        reload=True,
    )
