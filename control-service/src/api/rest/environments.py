"""Environment endpoints — the platform's first-class deployment unit.

An environment binds one Git branch, one SQLMesh virtual environment and one
Dagster deployment. These endpoints are the ONLY way to create, open or destroy
one: creating an environment as a side effect of ``apply`` is no longer possible.

Identity and authorization come exclusively from the gateway through headers it
fully controls (see ``src.api.deps.CallerContext``). Permissions are re-asserted
here as defence in depth.

All timestamps are returned as UTC ISO-8601 strings; the frontend renders them
in the user's timezone.
"""

import typing as t
from datetime import datetime

from datacode_common.rbac import (
    PERM_WORKSPACE_ENV_CREATE,
    PERM_WORKSPACE_ENV_DELETE,
    PERM_WORKSPACE_ENV_READ,
    PERM_WORKSPACE_READ,
)
from fastapi import APIRouter, Depends, Query

from src.api.deps import CallerContext, require_permission
from src.api.models.requests import CreateEnvironmentRequest
from src.environments.registry import EnvironmentView, get_environment_registry
from src.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["environment"])

# Permission codes come from the shared catalog (shared/rbac-catalog.json).


def _iso(value: t.Optional[datetime]) -> str:
    """Serialize a UTC timestamp, or an empty string when unset."""
    return value.isoformat() if value else ""


def _serialize(env: EnvironmentView) -> t.Dict[str, t.Any]:
    """Serialize an environment read model."""
    return {
        "environment_id": env.environment_id,
        "workspace_id": env.workspace_id,
        "name": env.name,
        "branch": env.branch,
        "env_type": env.env_type,
        "base_env": env.base_env,
        "owner_user_id": env.owner_user_id,
        "status": env.status,
        "deployment_name": env.deployment_name,
        "schedules_enabled": env.schedules_enabled,
        "is_production": env.is_production,
        "last_applied_commit": env.last_applied_commit,
        "last_applied_at": _iso(env.last_applied_at),
        "last_applied_dirty": env.last_applied_dirty,
        "last_promoted_to": env.last_promoted_to,
        "ttl_hours": env.ttl_hours,
        "expires_at": _iso(env.expires_at),
        "created_at": _iso(env.created_at),
        "updated_at": _iso(env.updated_at),
    }


@router.get("/workspaces/{workspace_id}/environments")
async def list_environments(
    workspace_id: str,
    include_archived: bool = Query(
        default=False, description="Include archived environments (audit view)."
    ),
    caller: CallerContext = Depends(require_permission(PERM_WORKSPACE_ENV_READ)),
):
    """List a workspace's environments.

    Environments are workspace-level shared assets, so every member sees the
    same list regardless of which working copies they hold locally.
    """
    environments = await get_environment_registry().list(
        workspace_id=workspace_id,
        tenant_id=caller.tenant_id,
        include_archived=include_archived,
    )
    return {
        "workspace_id": workspace_id,
        "environments": [_serialize(env) for env in environments],
    }


@router.post("/workspaces/{workspace_id}/environments")
async def create_environment(
    workspace_id: str,
    req: CreateEnvironmentRequest,
    caller: CallerContext = Depends(require_permission(PERM_WORKSPACE_ENV_CREATE)),
):
    """Create an environment: registry row, Git branch and worktree.

    Publishing the branch to the remote requires ``workspace:git:push``; when
    the push fails the environment stays usable for its creator and the response
    reports it through ``status``.
    """
    env = await get_environment_registry().create(
        workspace_id=workspace_id,
        tenant_id=caller.tenant_id,
        actor_user_id=caller.user_id,
        name=req.name,
        env_type=req.env_type,
        base_env=req.base_env,
        ttl_hours=req.ttl_hours,
        trigger_type=req.trigger_type,
    )
    return _serialize(env)


@router.get("/environments/{environment_id}")
async def get_environment(
    environment_id: str,
    caller: CallerContext = Depends(require_permission(PERM_WORKSPACE_ENV_READ)),
):
    """Get one environment's registry state."""
    env = await get_environment_registry().get(environment_id, caller.tenant_id)
    return _serialize(env)


@router.post("/environments/{environment_id}/open")
async def open_environment(
    environment_id: str,
    caller: CallerContext = Depends(require_permission(PERM_WORKSPACE_READ)),
):
    """Materialize the caller's worktree of an environment and return its path.

    The returned ``workspace_path`` is the value every other endpoint of every
    service expects: it identifies both the workspace AND the environment.
    """
    opened = await get_environment_registry().open(
        environment_id=environment_id,
        tenant_id=caller.tenant_id,
        user_id=caller.user_id,
    )
    return {
        "environment_id": opened.environment_id,
        "environment": opened.environment,
        "branch": opened.branch,
        "workspace_path": opened.workspace_path,
        "head_commit": opened.head_commit,
        "materialized": opened.materialized,
    }


@router.get("/environments/{environment_id}/events")
async def list_environment_events(
    environment_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    caller: CallerContext = Depends(require_permission(PERM_WORKSPACE_ENV_READ)),
):
    """Lifecycle timeline of an environment (newest first)."""
    events = await get_environment_registry().events(
        environment_id, caller.tenant_id, limit
    )
    return {
        "environment_id": environment_id,
        "events": [
            {**event, "created_at": _iso(event["created_at"])} for event in events
        ],
    }


@router.post("/environments/{environment_id}/reconcile")
async def reconcile_environment(
    environment_id: str,
    repair: bool = Query(
        default=True,
        description="Apply the repairs the reconciler is allowed to perform.",
    ),
    caller: CallerContext = Depends(require_permission(PERM_WORKSPACE_ENV_CREATE)),
):
    """Compare an environment against Git and the SQLMesh state, and repair it.

    Repairs are limited to actions that cost no data and adopt nothing: a branch
    the registry owns is recreated from its base, a virtual environment is
    rebuilt (a pointer switch), and an archived environment's leftover resources
    are released. ``commit_drift`` and unregistered branches are reported only —
    the first needs an apply the user must authorize, the second a human decision
    (design §11).

    Requires ``env:create`` because repairing may create a branch and a virtual
    environment, which is exactly what creating an environment does.
    """
    report = await get_environment_registry().reconcile(
        environment_id=environment_id,
        tenant_id=caller.tenant_id,
        actor_user_id=caller.user_id,
        repair=repair,
    )
    return {"environment_id": environment_id, **report}


@router.delete("/environments/{environment_id}")
async def destroy_environment(
    environment_id: str,
    delete_remote_branch: bool = Query(
        default=True, description="Also delete the environment branch on the remote."
    ),
    force: bool = Query(
        default=False,
        description="Discard uncommitted changes in the caller's working copy.",
    ),
    caller: CallerContext = Depends(require_permission(PERM_WORKSPACE_ENV_DELETE)),
):
    """Destroy an environment: release Git resources, keep the audit trail.

    Production is rejected: it is the workspace's own line of development.
    Uncommitted changes in the caller's working copy require ``force``.
    """
    result = await get_environment_registry().destroy(
        environment_id=environment_id,
        tenant_id=caller.tenant_id,
        actor_user_id=caller.user_id,
        delete_remote_branch=delete_remote_branch,
        force=force,
    )
    # Honest reporting: a skipped release (no local copy anywhere, a missing
    # credential) must not masquerade as a full cleanup. ``worktrees_removed``
    # counts member copies, since an environment is materialized once per member.
    return {
        "success": True,
        "environment_id": environment_id,
        "archived": result.archived,
        "remote_branch_deleted": result.remote_branch_deleted,
        "virtual_environment_released": result.virtual_environment_released,
        "worktrees_removed": result.worktrees_removed,
    }
