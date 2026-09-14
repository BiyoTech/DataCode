"""workspace-runtime entry point (design 02 §2.2, design 06 P3).

The sole owner of working copies: SQLMesh engine + Ontology + Dagster code
locations + Git + file IO + the gated tool/executor plane, in ONE process.
Merging them here removed the two service-to-service HTTP cycles that the
old sqlmesh-service / dagster-service / ontology-service triangle needed.

Application shell:
- disables SQLMesh telemetry before any sqlmesh import
- initializes the Context Pool, metadata DB and background workers
- registers every REST route group (sqlmesh, git, files, ontology, dagster,
  tools, internal registration API, metrics)
"""

from __future__ import annotations

import os

# Disable SQLMesh anonymized analytics BEFORE any sqlmesh import.
# This prevents outbound requests to https://analytics.tobikodata.com.
os.environ["SQLMESH__DISABLE_ANONYMIZED_ANALYTICS"] = "true"

import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.config import settings
from src.sqlmesh.task_tracker import get_task_tracker
from src.utils.errors import ErrorCode, ServiceError, translate_oserror
from src.utils.logging import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)


def _register_exception_handlers(application: FastAPI) -> None:
    """One response shape for every outcome: ``{error_code, message, detail?}``."""

    @application.exception_handler(ServiceError)
    async def _service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_response_dict(),
            headers=exc.headers,
        )

    # NOTE: the codeloc package no longer registers its own handler. Its error
    # hierarchy used to be a separate ServiceError class with an identical
    # response shape; since both now share one base class
    # (datacode_common.errors.ServiceError), the single handler above covers
    # it — and being registered second, a codeloc-specific handler would
    # SHADOW this one and silently drop the Retry-After headers it carries.

    @application.exception_handler(HTTPException)
    async def _http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        message = (
            str(detail.get("message") or detail.get("detail") or "HTTP error")
            if isinstance(detail, dict)
            else (str(detail) if detail else "HTTP error")
        )
        body: dict = {"error_code": ErrorCode.INTERNAL_ERROR.value, "message": message}
        if isinstance(detail, dict):
            extra = {k: v for k, v in detail.items() if k not in {"message", "detail"}}
            if extra:
                body["detail"] = extra
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
                "detail": exc.errors(),
            },
        )

    @application.exception_handler(OSError)
    async def _oserror_handler(_request: Request, exc: OSError) -> JSONResponse:
        """One translation point for OSErrors (see translate_oserror).

        ENOSPC is the contract's INSUFFICIENT_STORAGE (507); every other
        OSError keeps the generic 500 shape. The verdict itself lives in
        ``src.utils.errors`` so it is unit-testable without the app shell.
        """
        translated = translate_oserror(exc)
        if translated.code == ErrorCode.INSUFFICIENT_STORAGE:
            logger.error(
                "insufficient_storage",
                error=str(exc),
                hint="the working-copy volume is full; the disk reclaimer "
                "will free unreferenced copies",
            )
        else:
            logger.error("unhandled_oserror", error=str(exc), exc_info=True)
        return JSONResponse(
            status_code=translated.status_code, content=translated.to_response_dict()
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
    # Working-copy storage contract FIRST (design 07 ADR-003 / Q7).
    # Deliberately before anything touches the filesystem: the combinations
    # this rejects lose user data with no error at write time, so refusing to
    # boot is the only place the operator can still be told.
    storage = settings.storage_contract()
    logger.info("storage_contract_validated", **storage.describe())
    for warning in storage.warnings:
        # Allowed but weaker than the target shape; never silently swallowed.
        logger.warning("storage_contract_degraded", reason=warning)

    # Read-side metadata: the MetadataContextStore (in-memory cache over the
    # executor sandbox's serialized payloads) initializes lazily on first use;
    # the EXECUTABLE context lives in executor children per design 04 §3.7.
    # The former ContextPool is gone — no Context is constructed in this
    # process any more (P6 close-out).

    # Metadata DB: same database as the legacy sqlmesh-service; runtime holds
    # read mappings only (ownership moves to control-service in P5).
    try:
        from src.workspace.database import init_db

        await init_db()
    except Exception as e:  # noqa: BLE001 — metadata is a recovery layer
        logger.warning("database_init_skipped", error=str(e))

    background_tasks: list[asyncio.Task] = []

    await get_task_tracker().start_registry()

    # Workspace leases (design 06 P4a). No-op while `lease.enabled` is false;
    # started after the storage contract has been validated, because a lease
    # only makes sense on a medium the contract accepted.
    from src.lease.wiring import init_lease_manager

    lease_manager = await init_lease_manager()

    # Resolve the isolation policy at startup (design 06 P6a). Loading it here
    # rather than on the first task means a configuration mistake — a profile
    # naming an unavailable level, or a sandbox plane that is switched off —
    # appears in the startup log instead of inside a user's first plan.
    from src.isolation.policy import runtime_isolation_policy

    isolation = runtime_isolation_policy()
    isolation_default = isolation.default_profile

    # Code-location vehicle (design 06 P6c): pod mode needs a container
    # isolation profile behind it. Checked here — at startup, not when the
    # first deployment arrives — because a vehicle misconfiguration is an
    # operator error, and the operator is present at rollout, not later.
    if (settings.code_location_mode or "process").strip().lower() == "pod":
        from src.codeloc.pod import assert_vehicle_supported

        assert_vehicle_supported(isolation)

    # Dagster data plane: the instance daemon and webserver (local mode) plus
    # the supervisor that respawns them and every code location.
    #
    # This is the ONLY thing that starts them. Code locations are additionally
    # spawned per deployment by the CodeLocationManager, which is why a missing
    # call here is easy to overlook: locations come up, the asset DAG asks the
    # instance GraphQL endpoint — which nobody ever started — and fails with
    # "Dagster instance is unavailable" while everything else looks healthy.
    dagster_plane = None
    try:
        from src.codeloc.process import get_process_manager

        dagster_plane = get_process_manager()
        await dagster_plane.start()
    except Exception as e:  # noqa: BLE001 — a broken data plane must not block boot
        logger.error(
            "dagster_plane_start_failed",
            error=str(e),
            effect="Dagster schedules and the asset DAG are unavailable; "
            "SQLMesh, Git and files are unaffected",
        )

    logger.info(
        "workspace_runtime_started",
        rest_port=settings.rest_port,
        dagster_mode=settings.dagster_mode,
        executor_enabled=settings.executor_enabled,
        metadata_cache_ttl_seconds=settings.metadata_cache_ttl_seconds,
        mount_kind=storage.mount_kind.value,
        lease_enabled=lease_manager is not None,
        task_backend=settings.task_backend,
        isolation_profiles=[p.name for p in isolation.enabled_profiles],
        isolation_default=isolation_default.name if isolation_default else None,
    )

    yield

    # Leases FIRST: draining takes a final WIP snapshot of every working copy,
    # which is what makes a planned shutdown lose nothing. Tearing down the
    # context pool or the database before that would strip the machinery the
    # snapshot path depends on.
    from src.lease.wiring import shutdown_lease_manager

    await shutdown_lease_manager()

    # Dagster's children (daemon, webserver, code locations) are spawned with
    # start_new_session=True, so they do NOT die with this process's group —
    # they survive a group kill and keep holding their ports. Stopping them
    # explicitly is the only thing that prevents an orphaned code server
    # mounting a working copy nobody manages any more.
    if dagster_plane is not None:
        try:
            await dagster_plane.stop_all()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the rest
            logger.warning("dagster_plane_stop_failed", error=str(e))

    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    await get_task_tracker().stop_registry()
    from src.executor.process import get_executor_manager
    from src.tasks.submitter import shutdown_task_submitter
    from src.tasks.warm_pool import shutdown_warm_pool

    await get_executor_manager().shutdown()
    # After the executor: a task in flight may still be talking to a working
    # copy the teardown path is about to release.
    await shutdown_task_submitter()
    # Resident runners last: they are Pods this replica owns (pinned to its node,
    # mounting its working copies), so leaving them behind strands user code on a
    # mount nobody manages.
    await shutdown_warm_pool()
    try:
        from src.workspace.database import close_db

        await close_db()
    except Exception:  # noqa: BLE001
        pass
    logger.info("workspace_runtime_stopped")


app = FastAPI(
    title="workspace-runtime",
    description="The sole owner of working copies: SQLMesh + Ontology + "
    "Dagster code locations + Git + files + gated tool execution.",
    version="0.1.0",
    lifespan=lifespan,
)
_register_exception_handlers(app)

from datacode_common.api import LogContextMiddleware  # noqa: E402

app.add_middleware(LogContextMiddleware)

# ── Route groups ──────────────────────────────────────────────────────────
# SQLMesh engine surface (formerly sqlmesh-service).
from src.api.rest import (  # noqa: E402
    cell_migration,
    deps,
    env_control,
    events,
    files,
    git,
    knowledge,
    leases,
    logs,
    meta,
    metrics,
    ontology,
    sqlmesh,
    tools,
    wip,
)

app.include_router(sqlmesh.router)
app.include_router(git.router)
app.include_router(files.router)
app.include_router(knowledge.router)
app.include_router(events.router)
app.include_router(logs.router)
app.include_router(meta.router)
app.include_router(ontology.router)
app.include_router(tools.router)
app.include_router(metrics.router)
# Working-copy Python dependencies: the platform baseline and one copy's state.
# Read-only — installing is a minutes-long operation and stays behind the
# explicit reload, never behind a GET.
app.include_router(deps.router)
# WIP durability contract (design 07 Q6): the user-facing status/recover/
# discard surface of the lease subsystem's snapshot machinery.
app.include_router(wip.router)
# Lease placement channel (design 06 P4a): the gateway's acquire path.
# Internal-token guarded, fail closed — see api/dagster/internal_auth.py.
app.include_router(leases.router)
# Environment-control channel (design 06 P5): control-service's command path
# for Git materialization, SQLMesh virtual environments and workspace
# provisioning. Internal-token guarded like every /internal/* surface.
app.include_router(env_control.router)
# Cross-cell migration for the tables THIS service owns (design 06 P7). The
# control plane orchestrates and moves its own tables; each service moves what it
# creates, so no table ever has a second creator or a mirrored definition to
# drift. Internal-token guarded like every /internal/* surface.
app.include_router(cell_migration.router)

# Dagster data-plane surface (formerly dagster-service).
from src.api.dagster import (  # noqa: E402
    assets,
    deployments,
    internal,
    pipelines,
    runs,
    schedules,
)
from src.api.dagster import health as dagster_health  # noqa: E402

app.include_router(dagster_health.router)
app.include_router(internal.router)
app.include_router(deployments.router)
app.include_router(pipelines.router)
app.include_router(assets.router)
app.include_router(runs.router)
app.include_router(schedules.router)


@app.get("/health")
async def health_check():
    """Health with sandbox + metadata cache state (P3: executors report coverage)."""
    from src.executor.process import get_executor_manager
    from src.lease.wiring import get_lease_manager
    from src.sqlmesh.metadata_context import get_metadata_context
    from src.tasks.warm_pool import get_warm_pool

    body = {
        "status": "healthy",
        "service": "workspace-runtime",
        "dagster_mode": settings.dagster_mode,
        "metadata_cache_size": get_metadata_context().cached_entry_count(),
        "executor": {
            "enabled": settings.executor_enabled,
            "active": get_executor_manager().active_workspaces(),
        },
        # Resident task runners. Reported because a pool that is quietly failing
        # to start Pods looks exactly like a pool nobody uses: both show zero.
        "warm_pool": get_warm_pool().stats(),
        # Which medium this replica actually runs on: the difference between
        # ephemeral and instance_local decides whether a rolling update
        # consumes the WIP durability window, so it must be observable.
        "storage": settings.storage_contract().describe(),
    }

    lease_manager = get_lease_manager()
    if lease_manager is not None:
        # `unprotected` is the field that matters operationally: it names the
        # working copies whose uncommitted work is NOT covered by the durability
        # contract right now.
        status = lease_manager.protection_status()
        body["lease"] = {
            "served_keys": lease_manager.served_keys(),
            "served_count": status["served"],
            "unprotected": status["unprotected"],
            "last_protected_at": status["last_protected_at"],
        }
    return body


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.rest_port,
        reload=True,
    )
