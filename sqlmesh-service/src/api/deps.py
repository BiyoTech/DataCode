"""Shared FastAPI dependencies for API route modules.

Provides service instances, the caller's authorization context and common
validation utilities.
"""

from pathlib import Path
from typing import Annotated, Callable, FrozenSet

from fastapi import Depends, Header
from pydantic import BaseModel, Field

from src.config import settings
from src.core.command_service import get_command_service
from src.core.context_pool import get_context_pool
from src.core.environment_service import get_environment_service
from src.core.external_models_service import get_external_models_service
from src.core.lineage_service import get_lineage_service
from src.core.model_service import get_model_service
from src.core.plan_service import get_plan_service
from src.core.query_service import get_query_service
from src.core.schema_service import get_schema_service
from agenticdata_common.logging.context import bind_workspace_context

from src.utils.errors import PermissionDeniedError, WorkspacePathInvalidError
from src.utils.logging import get_logger
from src.workspace.paths import WorkspacePathLayoutError, parse_workspace_path

logger = get_logger(__name__)


class CallerContext(BaseModel):
    """Identity and authorization decided upstream by the gateway.

    The gateway is the platform's single authorization center: it verifies the
    access token, resolves the caller's roles from the RBAC tables and publishes
    the outcome on headers it fully controls (clients cannot forge them, they are
    stripped and rewritten on every request).

    This service therefore never re-derives permissions and never reads identity
    from a request body or query string for authorization purposes.
    """

    user_id: str
    tenant_id: str
    workspace_id: str = ""
    workspace_role: str = ""
    permissions: FrozenSet[str] = Field(default_factory=frozenset)

    def can(self, permission: str) -> bool:
        """Whether the gateway granted a permission for this request."""
        return permission in self.permissions


def caller_context(
    x_user_id: str = Header(default="", alias="X-User-ID"),
    x_tenant_id: str = Header(default="", alias="X-Tenant-ID"),
    x_workspace_id: str = Header(default="", alias="X-Workspace-ID"),
    x_workspace_role: str = Header(default="", alias="X-Workspace-Role"),
    x_granted_permissions: str = Header(default="", alias="X-Granted-Permissions"),
) -> CallerContext:
    """Build the caller context from the gateway-controlled headers."""
    if not x_user_id or not x_tenant_id:
        # Reaching this service without a gateway-issued identity means the
        # request bypassed authorization entirely.
        logger.warning("caller_context_missing_identity")
        raise PermissionDeniedError(
            "This request carries no verified identity: it did not pass through "
            "the API gateway"
        )

    permissions = frozenset(
        code.strip() for code in x_granted_permissions.split(",") if code.strip()
    )
    return CallerContext(
        user_id=x_user_id,
        tenant_id=x_tenant_id,
        workspace_id=x_workspace_id,
        workspace_role=x_workspace_role,
        permissions=permissions,
    )


# FastAPI dependency exposing the authorization context of the current request.
Caller = Annotated[CallerContext, Depends(caller_context)]


def require_permission(permission: str) -> Callable[..., CallerContext]:
    """Build a dependency asserting that the gateway granted a permission.

    Defence in depth: the gateway already denied the request if the permission
    was missing, so a failure here means the two layers disagree and the request
    must not proceed.
    """

    def dependency(caller: Caller) -> CallerContext:
        if not caller.can(permission):
            logger.warning(
                "permission_assertion_failed",
                user_id=caller.user_id,
                tenant_id=caller.tenant_id,
                workspace_id=caller.workspace_id,
                permission=permission,
            )
            raise PermissionDeniedError(
                "Permission denied: your role does not allow this operation",
                permission=permission,
            )
        return caller

    return dependency


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


# FastAPI dependency for query-parameter workspace_path validation.
WorkspacePath = Annotated[str, Depends(validate_workspace_path)]


__all__ = [
    "get_context_pool",
    "get_plan_service",
    "get_model_service",
    "get_lineage_service",
    "get_query_service",
    "get_schema_service",
    "get_command_service",
    "get_environment_service",
    "validate_workspace_path",
    "WorkspacePath",
    "CallerContext",
    "Caller",
    "caller_context",
    "require_permission",
]
