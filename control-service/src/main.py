"""Control service entrypoint (design 02/06).

The platform's control plane: environment registry + reconciler + TTL reaper,
workspace metadata, Git credentials and quota bookkeeping. It owns the database
(and the platform's alembic chain) and dispatches every filesystem / Git /
engine side effect to workspace-runtime over the internal env-control channel.
"""

from __future__ import annotations

import asyncio

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.config import settings
from src.utils.errors import ErrorCode, ServiceError
from src.utils.logging import get_logger, setup_logging

setup_logging()

logger = get_logger(__name__)

app = FastAPI(
    title="DataCode Control Service",
    description=(
        "Control plane: environment registry, workspace metadata, "
        "reconciliation and reclamation"
    ),
    version="0.1.0",
)


def _register_exception_handlers(application: FastAPI) -> None:
    """One response shape for every outcome: ``{error_code, message, detail?}``."""

    @application.exception_handler(ServiceError)
    async def _service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_response_dict(),
            headers=exc.headers,
        )

    @application.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        message = (
            str(detail.get("message") or detail.get("detail") or "HTTP error")
            if isinstance(detail, dict)
            else (str(detail) if detail else "HTTP error")
        )
        body = {"error_code": ErrorCode.INTERNAL_ERROR.value, "message": message}
        if isinstance(detail, dict):
            body.update(detail)
            body.pop("detail", None)
        return JSONResponse(status_code=exc.status_code, content=body)

    @application.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error_code": ErrorCode.INVALID_REQUEST.value,
                "message": "Request validation failed",
                "errors": exc.errors(),
            },
        )

    @application.exception_handler(Exception)
    async def _unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", error=str(exc), exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error_code": ErrorCode.INTERNAL_ERROR.value,
                "message": "Internal server error",
            },
        )


_register_exception_handlers(app)


@app.get("/health")
async def health() -> dict:
    """Liveness probe (deep probes are the gateway's job)."""
    return {"status": "ok", "service": "control-service"}


# ── Lifecycle ──────────────────────────────────────────────────────────────


def _assert_database_topology() -> None:
    """Fail fast when a declared Global/Cell split did not actually happen.

    control-service is the right place for this check: it owns the CELL tier and
    can resolve the Global one from the same platform config, so it is the only
    service that sees both. identity-service (Go) resolves only its own.
    """
    from datacode_common.db_topology import (
        GLOBAL_DB_SERVICE,
        assert_database_topology,
    )

    from src.config import settings as cfg

    platform = cfg.platform
    assert_database_topology(
        global_url=platform.database.url(service=GLOBAL_DB_SERVICE),
        cell_url=platform.database.url(service="control"),
        declared_global=getattr(platform.cells, "expect_global_db", False),
        service="control-service",
    )


@app.on_event("startup")
async def startup() -> None:
    from src.workspace.database import close_db, init_db

    # Topology BEFORE the schema: if the Global and Cell tiers resolved to one
    # database, init_db would create the cell's tables next to identity's and
    # the mistake becomes invisible (see datacode_common.db_topology).
    _assert_database_topology()

    # The environment lock is shared with workspace-runtime's apply path; an
    # in-process backend would silently drop that exclusion.
    from src.core.env_locks import locks as env_locks

    logger.info("environment_lock_backend_validated", mode=env_locks.validate())

    await init_db()

    from src.config import settings as cfg
    from src.environments.reaper import get_environment_reaper

    if cfg.environment_reaper_enabled:
        reaper_task = asyncio.create_task(
            get_environment_reaper().start_periodic(cfg.environment_reaper_interval)
        )
        app.state.reaper_task = reaper_task

    # Periodic reconciliation: the lease and sandbox drift kinds are leaks
    # no user action would ever surface, so they need a timer rather than a page
    # view. Detection only until `reconcile.sweep_repair` is enabled.
    if cfg.reconcile_sweep_enabled:
        from src.environments.sweep import get_reconcile_sweep

        sweep_task = asyncio.create_task(
            get_reconcile_sweep().start_periodic(cfg.reconcile_sweep_interval)
        )
        app.state.sweep_task = sweep_task

    # Audit record retention: task_records / lease_history are append-only
    # and landed without an expiry; this is the expiry.
    if cfg.retention_enabled:
        from src.records.retention import get_record_retention

        retention_task = asyncio.create_task(
            get_record_retention().start_periodic(cfg.retention_interval)
        )
        app.state.retention_task = retention_task

    # Capacity orchestration: heartbeat-driven replica control + quota
    # snapshot publishing + lease usage accounting. Disabled by default so a
    # single-instance dev topology (one runtime, no scaling surface) starts
    # exactly as before.
    if cfg.capacity_controller_enabled:
        from src.capacity import LeaseControllerLoop, get_lease_controller

        controller_loop = LeaseControllerLoop(
            get_lease_controller(), interval=cfg.capacity_controller_interval
        )
        await controller_loop.start()
        app.state.controller_loop = controller_loop

    app.state.close_db = close_db
    await _register_this_cell()
    logger.info(
        "control_service_started",
        rest_port=settings.rest_port,
        cell_id=_cell_id_or_unknown(),
    )


def _cell_id_or_unknown() -> str:
    """This cell's id, for the startup line. Never raises."""
    try:
        from datacode_common.cell import cell_id

        return cell_id()
    except Exception:  # noqa: BLE001 - a bad id is reported by the guard below
        return "unknown"


async def _register_this_cell() -> None:
    """Publish this cell's endpoint to the global registry.

    Fail-OPEN by design: a cell whose control plane cannot reach the global
    identity service must still serve the tenants already placed on it, or a
    global outage would become a per-cell one. The cost of failing is that NEW
    tenants are not routed here until the next restart, which is recoverable.
    """
    from datacode_common.identity.cell_registry import (
        register_cell,
        registration_from_env,
    )

    from src.config import settings as cfg

    registration = registration_from_env(
        capacity_tenants=cfg.cell_capacity_tenants
    )
    if registration is None:
        # No endpoint configured: the normal single-cell state, where there is
        # nothing to route between.
        return

    identity_url = cfg.platform.services.url("identity", "rest_url") or ""
    accepted = await register_cell(identity_url, registration)
    if accepted:
        logger.info(
            "cell_registered",
            cell_id=registration.cell_id,
            endpoint=registration.endpoint,
            capacity_tenants=registration.capacity_tenants,
        )
    else:
        logger.warning(
            "cell_registration_failed",
            cell_id=registration.cell_id,
            endpoint=registration.endpoint,
            identity_url=identity_url,
            effect="new tenants will not be placed on this cell until it "
            "registers; already-placed tenants are unaffected",
        )


@app.on_event("shutdown")
async def shutdown() -> None:
    reaper_task = getattr(app.state, "reaper_task", None)
    if reaper_task is not None:
        reaper_task.cancel()
    sweep_task = getattr(app.state, "sweep_task", None)
    if sweep_task is not None:
        sweep_task.cancel()
    retention_task = getattr(app.state, "retention_task", None)
    if retention_task is not None:
        retention_task.cancel()
    controller_loop = getattr(app.state, "controller_loop", None)
    if controller_loop is not None:
        await controller_loop.stop()
    close_db = getattr(app.state, "close_db", None)
    if close_db is not None:
        await close_db()
    logger.info("control_service_stopped")


# ── Route groups ────────────────────────────────────────────────────────────

from src.api.rest import (  # noqa: E402
    cell_migration,
    env_registry,
    environments,
    git_credentials,
    knowledge,
    quotas,
    workspace,
)

app.include_router(environments.router)
app.include_router(workspace.router)
app.include_router(git_credentials.router)
# Tenant quota and usage: the read half of enforcement — a user rejected
# with 429 must be able to see what their budget actually is.
app.include_router(quotas.router)
# Knowledge METADATA only: create/upload/read/delete stay on the runtime,
# which owns the working copy's files.
app.include_router(knowledge.router)
# Cell migration data plane: internal-only, token-guarded; never routed
# through the gateway's public surface.
app.include_router(cell_migration.router)
# Registry writes the runtime asks for: it authors the applied watermark and
# the merge audit row, but must not write the control plane's tables itself.
app.include_router(env_registry.router)

from datacode_common.api import LogContextMiddleware  # noqa: E402

app.add_middleware(LogContextMiddleware)


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.rest_port,
        reload=False,
    )
