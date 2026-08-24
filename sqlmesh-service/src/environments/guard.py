"""Runtime guard: only REGISTERED environments may be planned or applied.

Two invariants are enforced here, both of them consequences of the registry
being the platform's source of truth:

1. **Registered only.** A Git branch or a SQLMesh state entry nobody registered
   is drift, not an environment. Planning against it would create a virtual
   environment with no owner, no lifetime and no Dagster deployment — exactly the
   "environment as a side effect of apply" hack the registry replaces. Such a
   request is rejected so the drift is adopted through the registry instead.

2. **Code and data share one source.** A working copy is a Git worktree of ONE
   environment's branch, so the only environment its code may materialize is that
   same environment. Accepting a different target would let ``prod`` be applied
   from an unmerged branch, which is the failure mode the whole design removes.

Not enforced here: forbidding direct writes to production. That protection
arrives with the promote orchestration (which is the alternative it points users
to); until then production remains appliable, because a fresh workspace has no
other environment.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass
from datetime import datetime, timezone

from src.environments import naming
from src.environments.repository import EnvironmentRepository
from src.utils.errors import (
    EnvironmentNotFoundError,
    EnvironmentStateError,
    InvalidRequestError,
)
from src.utils.logging import get_logger
from src.workspace.identity import WorkspaceIdentity, resolve_workspace_identity

logger = get_logger(__name__)

# Lifecycle states in which SQLMesh operations are allowed. ``applying`` and
# ``promoting`` are included because the registry marks them for the duration of
# an operation and the environment-level lock (not the status) is what serializes
# concurrent work; ``drifted`` is included so a broken environment can be
# repaired by re-planning it.
OPERABLE_STATUSES = (
    naming.STATUS_ACTIVE,
    naming.STATUS_APPLYING,
    naming.STATUS_PROMOTING,
    naming.STATUS_DRIFTED,
)


@dataclass(frozen=True)
class ResolvedEnvironment:
    """A validated environment target for a SQLMesh operation."""

    identity: WorkspaceIdentity
    environment_id: str
    name: str
    env_type: str
    status: str
    base_env: str

    @property
    def is_production(self) -> bool:
        return self.env_type == naming.ENV_TYPE_PRODUCTION


async def require_registered_environment(
    workspace_path: str, environment: str = ""
) -> ResolvedEnvironment:
    """Validate the environment a request targets, or raise.

    An empty ``environment`` resolves to the environment the working copy is
    checked out to, which is always the correct target for that path.

    Raises:
        WorkspacePathInvalidError: the path is not a platform working copy.
        InvalidRequestError: the requested environment is not the one this
            working copy carries.
        EnvironmentNotFoundError: the environment is absent from the registry.
        EnvironmentStateError: its lifecycle state forbids the operation.
    """
    identity = resolve_workspace_identity(workspace_path)
    requested = (environment or "").strip() or identity.environment

    if requested != identity.environment:
        logger.warning(
            "environment_target_mismatch",
            workspace_id=identity.workspace_id,
            worktree_environment=identity.environment,
            requested_environment=requested,
        )
        raise InvalidRequestError(
            f"This working copy is checked out to environment "
            f"'{identity.environment}', so it cannot operate on '{requested}'. "
            f"Open '{requested}' to get its own working copy.",
        )

    model = await EnvironmentRepository().get(
        identity.environment_id, identity.tenant_id
    )
    if model is None:
        logger.warning(
            "environment_not_registered",
            workspace_id=identity.workspace_id,
            environment=identity.environment,
        )
        raise EnvironmentNotFoundError(identity.environment)

    if model.status not in OPERABLE_STATUSES:
        raise EnvironmentStateError(
            f"Environment '{model.name}' is {model.status} and cannot be "
            f"planned or applied"
        )

    return ResolvedEnvironment(
        identity=identity,
        environment_id=model.id,
        name=model.name,
        env_type=model.env_type,
        status=model.status,
        base_env=model.base_env,
    )


async def mark_applied(
    environment_id: str,
    *,
    commit_hash: str,
    dirty: bool = False,
    applied_at: t.Optional[datetime] = None,
) -> None:
    """Record a successful apply on the registry row (best-effort).

    ``last_applied_commit`` is what makes "code and data are in sync" observable:
    comparing it with the branch HEAD yields the ``commit_drift`` signal the UI
    surfaces on the environment selector. Never fails the apply it describes.
    Timestamps are stored in UTC.

    ``dirty`` records that the apply ran from a working copy with uncommitted
    changes. It must be stored rather than hidden: the data is then NOT
    reproducible from ``commit_hash``, so reporting the environment as in sync
    would forge the "code and data share one source" invariant. Applying from a
    dirty tree stays allowed (a sandbox exists to iterate before committing);
    the UI turns this flag into drift that a commit + re-apply clears.
    """
    try:
        await EnvironmentRepository().update(
            environment_id,
            last_applied_commit=commit_hash or None,
            last_applied_at=applied_at or datetime.now(timezone.utc),
            last_applied_dirty=1 if dirty else 0,
        )
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not break apply
        logger.warning(
            "environment_apply_marker_failed",
            environment_id=environment_id,
            error=str(exc),
        )
