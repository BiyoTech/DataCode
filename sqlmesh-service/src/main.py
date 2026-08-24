"""SQLMesh Service entry point.

Thin application shell that:
- Disables SQLMesh anonymized analytics (no outbound telemetry)
- Initializes Context Pool on startup
- Registers REST route modules
- Starts background cleanup task
- Provides health/metrics endpoints
"""

import os

# Disable SQLMesh anonymized analytics BEFORE any sqlmesh import.
# This prevents outbound requests to https://analytics.tobikodata.com
os.environ["SQLMESH__DISABLE_ANONYMIZED_ANALYTICS"] = "true"

import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.config import settings
from src.core.context_pool import get_context_pool, init_context_pool
from src.utils.errors import ErrorCode, ServiceError
from src.workspace.paths import WorkspacePathLayoutError
from src.utils.logging import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)


def _register_exception_handlers(application: FastAPI) -> None:
    """Register global exception handlers so EVERY API response — success or
    failure — shares the same shape ``{error_code, message, detail?}``.

    Without the generic ``Exception`` and ``HTTPException`` handlers below,
    any exception that escapes the service layer (or any route using
    ``HTTPException`` directly) would be rendered with FastAPI's default
    body (``{"detail": "Internal Server Error"}``), hiding the root cause
    from the frontend and forcing it to handle multiple response shapes.
    """

    @application.exception_handler(ServiceError)
    async def _service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_response_dict())

    @application.exception_handler(WorkspacePathLayoutError)
    async def _workspace_layout_error_handler(
        _request: Request, exc: WorkspacePathLayoutError
    ) -> JSONResponse:
        # Identifier / path convention violations are caller mistakes (or
        # tampering attempts), never server faults, so they map to 400.
        return JSONResponse(
            status_code=400,
            content={
                "error_code": ErrorCode.WORKSPACE_PATH_INVALID.value,
                "message": str(exc),
            },
        )

    @application.exception_handler(HTTPException)
    async def _http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        # Reshape FastAPI's default ``{"detail": ...}`` into the unified
        # ``{error_code, message, detail}`` so the frontend can parse every
        # error response with a single code path. Preserve the original
        # status code (404, 409, ...) so semantics are not lost.
        detail = exc.detail
        message: str
        detail_out: object | None = None
        if isinstance(detail, dict):
            # Some routes pass a structured dict as detail (e.g. {"code": ...,
            # "message": ...}). Promote ``message`` to top-level and keep the
            # rest under ``detail``.
            message = str(detail.get("message") or detail.get("detail") or "HTTP error")
            detail_out = {k: v for k, v in detail.items() if k not in {"message", "detail"}}
            if not detail_out:
                detail_out = None
        elif isinstance(detail, str) and detail:
            message = detail
        else:
            message = "HTTP error"
        body: dict = {"error_code": ErrorCode.INTERNAL_ERROR.value, "message": message}
        if detail_out is not None:
            body["detail"] = detail_out
        return JSONResponse(status_code=exc.status_code, content=body)

    @application.exception_handler(RequestValidationError)
    async def _validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # 422 with a structured list of field-level errors so the frontend
        # can map each violation back to its input field.
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
        # Last-resort net. Anything reaching here is a genuine bug or an
        # exception the service layer forgot to translate. We log the full
        # traceback server-side and return a structured 500 — never the raw
        # FastAPI "Internal Server Error" string — so the frontend always
        # has an actionable, parseable body. The exception text is included
        # under ``detail`` for diagnostics (DB errors do not carry
        # credentials; SQLMesh errors are user-fixable, so transparency is
        # more valuable than opacity here).
        logger.error("unhandled_exception", error=str(exc), exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error_code": ErrorCode.INTERNAL_ERROR.value,
                "message": "An unexpected error occurred",
                "detail": str(exc).strip() or exc.__class__.__name__,
            },
        )

# Explicitly disable analytics as a second safeguard (handles cases where
# the env var is read after module init or the process was forked).
try:
    from sqlmesh.core.analytics import disable_analytics
    disable_analytics()
    logger.info("sqlmesh_analytics_disabled")
except Exception:
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: init pool + DB on start, shutdown on stop."""
    # Initialize Context Pool
    pool = init_context_pool(
        max_size=settings.context_pool_max_size,
        idle_ttl_seconds=settings.context_pool_idle_ttl,
        max_age_seconds=settings.context_pool_max_age,
    )

    # Initialize database (create tables if needed)
    try:
        from src.workspace.database import init_db
        await init_db()
    except Exception as e:
        logger.warning("database_init_skipped", error=str(e))

    # Start background context cleanup
    cleanup_task = asyncio.create_task(
        pool.start_periodic_cleanup(settings.context_pool_cleanup_interval)
    )

    # Start sandbox reclamation. Expired sandboxes hold a branch, warehouse
    # schemas and a quota slot, so without this loop resource usage only ever
    # grows and the per-workspace limit eventually blocks active members.
    background_tasks = [cleanup_task]
    if settings.environment_reaper_enabled:
        from src.environments.reaper import get_environment_reaper

        background_tasks.append(
            asyncio.create_task(
                get_environment_reaper().start_periodic(
                    settings.environment_reaper_interval
                )
            )
        )

    logger.info(
        "sqlmesh_service_started",
        rest_port=settings.rest_port,
        context_pool_max_size=settings.context_pool_max_size,
        environment_reaper_enabled=settings.environment_reaper_enabled,
    )

    yield

    # Shutdown
    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    await pool.shutdown()
    try:
        from src.workspace.database import close_db
        await close_db()
    except Exception:
        pass
    logger.info("sqlmesh_service_stopped")


app = FastAPI(
    title="SQLMesh Service",
    description="SQLMesh operations via Context Pool",
    version="0.2.0",
    lifespan=lifespan,
)
_register_exception_handlers(app)

from src.api.middleware import LogContextMiddleware  # noqa: E402

app.add_middleware(LogContextMiddleware)

# Register REST route modules
from src.api.rest import (  # noqa: E402
    environments,
    events,
    files,
    git,
    knowledge,
    logs,
    meta,
    sqlmesh,
    workspace,
)

app.include_router(sqlmesh.router)
app.include_router(environments.router)
app.include_router(workspace.router)
app.include_router(git.router)
app.include_router(files.router)
app.include_router(knowledge.router)
app.include_router(events.router)
app.include_router(logs.router)
app.include_router(meta.router)


# --- Health & Metrics ---

@app.get("/health")
async def health_check():
    """Health check with pool metrics."""
    pool = get_context_pool()
    return {
        "status": "healthy",
        "service": "sqlmesh-service",
        "context_pool_size": pool.get_metrics()["size"],
    }


@app.get("/api/v1/pool/metrics")
async def pool_metrics():
    """Detailed Context Pool metrics for observability."""
    return get_context_pool().get_metrics()


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.rest_port,
        reload=True,
    )
