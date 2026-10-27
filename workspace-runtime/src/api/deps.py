"""Shared FastAPI dependencies for API route modules.

Provides service instances, the caller's authorization context and common
validation utilities.

The authorization context comes from ``datacode_common.api``: every service
behind the gateway must read the same headers to the same conclusion, and a
second, slightly different reading would be a second authorization decision
nobody reviews. What is local to this service is the working-copy path
dependency below — including the lease gate, which only a service that holds
working copies can answer.
"""

from pathlib import Path
from typing import Annotated

from datacode_common.api import CallerAuth, CallerContext
from datacode_common.logging.context import bind_workspace_context
from datacode_common.naming import lease_key
from datacode_common.workspace_paths import WorkspacePathLayoutError, parse_workspace_path
from fastapi import Depends

from src.config import settings
from src.sqlmesh.command_service import get_command_service
from src.sqlmesh.environment_service import get_environment_service
from src.sqlmesh.external_models_service import get_external_models_service
from src.sqlmesh.lineage_service import get_lineage_service
from src.sqlmesh.model_service import get_model_service
from src.sqlmesh.plan_service import get_plan_service
from src.sqlmesh.query_service import get_query_service
from src.sqlmesh.schema_service import get_schema_service
from src.utils.errors import (
    ErrorCode,
    PermissionDeniedError,
    ServiceError,
    WorkspacePathInvalidError,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)


_auth = CallerAuth(permission_denied=PermissionDeniedError)

caller_context = _auth.context
#: FastAPI dependency exposing the authorization context of the current request.
#:
#: This service consumes the granted permission set directly (``caller.can(...)``)
#: rather than through ``_auth.require_permission``: its only assertion is
#: conditional on request CONTENT (promoting to production, decided by the target
#: branch in the body), which a static route dependency cannot express.
Caller = _auth.Caller


def validate_workspace_path(workspace_path: str) -> str:
    """Validate an environment working-copy path before any operation uses it.

    Two independent layers of defence:
    1. containment - the resolved path must live under the configured base
       (``relative_to``, not string prefix matching, so a sibling directory such
       as ``/workspaces-evil`` is rejected);
    2. layout - it must be exactly
       ``{base}/{tenant_id}/{workspace_id}/{user_id}/envs/{environment}``, which
       stops a caller from reaching the shared root, another tenant's directory,
       the bare object database (``.repo``) or an arbitrary nested folder.

    A valid path therefore identifies the workspace AND the environment, which is
    what lets every downstream service stay environment-aware without a new
    parameter.

    This is also where the request's logging context learns which working copy
    it acts on. Both the query-parameter dependency and the request-body
    ``WorkspacePathMixin`` funnel through here, so binding once covers every
    entry point and makes the workspace log sink target the right directory.
    """
    base = Path(settings.workspace_base_path).expanduser().resolve()
    target = Path(workspace_path).expanduser().resolve()

    try:
        target.relative_to(base)
    except ValueError:
        raise WorkspacePathInvalidError(
            workspace_path,
            reason=f"Path is outside allowed base: {base}",
        ) from None

    try:
        tenant_id, workspace_id, user_id, environment = parse_workspace_path(
            str(base), str(target)
        )
    except WorkspacePathLayoutError as e:
        raise WorkspacePathInvalidError(workspace_path, reason=str(e)) from None

    _check_lease_state(
        workspace_path, target, tenant_id, workspace_id, user_id, environment
    )

    if not target.exists():
        raise WorkspacePathInvalidError(
            workspace_path,
            reason="Path does not exist",
        )

    resolved = str(target)
    bind_workspace_context(
        resolved,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        user_id=user_id,
        environment=environment,
    )
    return resolved


def _check_lease_state(
    workspace_path: str,
    target: Path,
    tenant_id: str,
    workspace_id: str,
    user_id: str,
    environment: str,
) -> None:
    """The lease-aware gate on every working-copy request.

    With the lease subsystem enabled, exactly ONE runtime holds each working
    copy, and the gateway routes to that holder. Two situations need a verdict
    beyond "the path is valid":

    * **Not the holder** — the gateway's route cache has a TTL, and a lease
      can move (holder drained, crashed, released) inside that window. A
      request then lands on a replica whose volume may not even contain the
      copy. The contract answer is ``409 LEASE_NOT_HELD`` (design 08 §7): the
      runtime states it is not the owner, and the gateway invalidates its
      cached route, re-resolves, and retries once. Serving it anyway would
      read stale shared state or fabricate a path that does not exist.
    * **Holder, but the copy is not on disk yet** — ``adopt()`` commits the
      lease first and materializes in the background, so the first requests
      after a placement race the clone. That is not an error of any kind:
      ``202 WORKSPACE_MATERIALIZING`` (design 08 §7) tells the caller the
      request was valid and will succeed shortly. The invariant that makes
      the verdict safe: a materialization failure RELEASES the lease, so
      "held but missing" can only mean "still materializing".

    No verdict at all when the lease subsystem is off: that is a legitimate
    deployment (shared medium, single replica), and the storage contract
    already refuses the unsafe combination (instance-local medium + leases
    off) at startup.
    """
    from src.lease.wiring import get_lease_manager

    manager = get_lease_manager()
    if manager is None:
        return

    key = lease_key(tenant_id, workspace_id, user_id, environment)
    if not manager.holds(key):
        if manager.draining:
            # This instance is going away and just released the copy: it is
            # RELOCATING (design 03 §4.3), not merely elsewhere. The client
            # should wait out the hand-over — the frontend interceptor keys
            # its banner and transparent retry on exactly this verdict.
            logger.info(
                "workspace_relocating",
                lease_key=key,
                workspace_path=workspace_path,
            )
            raise ServiceError(
                ErrorCode.WORKSPACE_RELOCATING,
                (
                    "This working copy is relocating to another runtime; "
                    "retry after the advertised window"
                ),
                status_code=503,
                headers={"Retry-After": "5"},
            )
        logger.info(
            "lease_not_held",
            lease_key=key,
            workspace_path=workspace_path,
            hint="gateway should invalidate its route and re-resolve",
        )
        raise ServiceError(
            ErrorCode.LEASE_NOT_HELD,
            (
                "This runtime does not hold the lease for the requested "
                "working copy; the gateway must re-resolve the lease and retry"
            ),
            status_code=409,
        )

    if not target.exists():
        logger.info(
            "workspace_materializing",
            lease_key=key,
            workspace_path=workspace_path,
        )
        raise ServiceError(
            ErrorCode.WORKSPACE_MATERIALIZING,
            (
                "This working copy is being materialized on this runtime; "
                "retry shortly and the request will succeed"
            ),
            status_code=202,
            headers={"Retry-After": "2"},
        )


# FastAPI dependency for query-parameter workspace_path validation.
WorkspacePath = Annotated[str, Depends(validate_workspace_path)]


__all__ = [
    "get_plan_service",
    "get_model_service",
    "get_lineage_service",
    "get_query_service",
    "get_schema_service",
    "get_command_service",
    "get_environment_service",
    "get_external_models_service",
    "validate_workspace_path",
    "WorkspacePath",
    "CallerContext",
    "Caller",
    "caller_context",
]
