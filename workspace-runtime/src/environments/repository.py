"""Environment registry repository - database CRUD layer.

Keeps every SQL statement about environments in one place. Authorization is not
performed here: the gateway is the platform's single authorization center and
the caller's permissions have already been checked upstream. Tenant scoping IS
enforced, so an environment of another tenant is reported as missing.
"""

from __future__ import annotations

import json
import typing as t
import uuid
from datetime import datetime, timezone

from datacode_common import naming as naming
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from src.environments.models import EnvironmentEventModel, EnvironmentModel
from src.utils.errors import ConflictError
from src.utils.logging import get_logger
from src.workspace.database import get_session

logger = get_logger(__name__)

# Columns a caller may update through :meth:`EnvironmentRepository.update`.
# Whitelisting keeps identity/derived columns (id, workspace_id, name, branch)
# immutable, which is what guarantees the 1:1 environment/branch binding.
_UPDATABLE_COLUMNS = frozenset(
    {
        "env_type",
        "base_env",
        "status",
        "deployment_name",
        "schedules_enabled",
        "last_applied_commit",
        "last_applied_at",
        "last_applied_dirty",
        "last_promoted_to",
        "ttl_hours",
        "expires_at",
    }
)


class EnvironmentRepository:
    """Database CRUD operations for the environment registry."""

    async def create(self, environment: EnvironmentModel) -> EnvironmentModel:
        """Insert an environment row.

        Written BEFORE any downstream side effect (Git branch, SQLMesh env,
        Dagster deployment) so a crashed orchestration can always be replayed
        from the registry.

        The registry's same-name pre-check is advisory only; the unique key is
        the real guard, so a concurrent duplicate maps to ``ConflictError``
        (409) instead of an unmapped ``IntegrityError`` (500).
        """
        async with get_session() as session:
            session.add(environment)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise ConflictError(
                    f"Environment already exists: {environment.name}"
                ) from exc
            logger.info(
                "environment_db_created",
                environment_id=environment.id,
                workspace_id=environment.workspace_id,
                env_type=environment.env_type,
                branch=environment.branch,
            )
            return environment

    async def reactivate(
        self,
        environment_id: str,
        *,
        expected_statuses: t.Sequence[str],
        **values: t.Any,
    ) -> None:
        """Recycle a dead row's lifecycle so its name can be claimed again.

        Names are user-facing: the sandbox suggestion flow recommends
        ``dev_{user}``, which users expect to recreate after destroying a
        sandbox, and a half-failed creation must not strand its name either. The
        row is recycled rather than replaced: identity columns stay untouched,
        so the previous life's audit trail remains attached.

        ``expected_statuses`` makes the reset a compare-and-set: if the row left
        that set concurrently, nothing is written and the caller retries.
        """
        allowed = {
            "env_type",
            "base_env",
            "owner_user_id",
            "deployment_name",
            "schedules_enabled",
            "ttl_hours",
            "expires_at",
        }
        rejected = set(values) - allowed
        if rejected:
            raise ValueError(
                f"environment columns are not reactivatable: {sorted(rejected)}"
            )

        lifecycle_reset = {
            "status": naming.STATUS_CREATING,
            "last_applied_commit": None,
            "last_applied_at": None,
            "last_applied_dirty": 0,
            "last_promoted_to": None,
            # A recycled row starts a new life: report it as created now, so its
            # age reflects the environment users actually see.
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        async with get_session() as session:
            result = await session.execute(
                update(EnvironmentModel)
                .where(
                    EnvironmentModel.id == environment_id,
                    EnvironmentModel.status.in_(list(expected_statuses)),
                )
                .values(**values, **lifecycle_reset)
            )
            if result.rowcount == 0:
                raise ConflictError(
                    f"Environment {environment_id} is no longer reusable; "
                    "retry the request"
                )
        logger.info("environment_db_reactivated", environment_id=environment_id)

    async def get(
        self, environment_id: str, tenant_id: str = ""
    ) -> t.Optional[EnvironmentModel]:
        """Get an environment by registry key, optionally scoped to a tenant."""
        conditions = [EnvironmentModel.id == environment_id]
        if tenant_id:
            conditions.append(EnvironmentModel.tenant_id == tenant_id)

        async with get_session() as session:
            result = await session.execute(select(EnvironmentModel).where(*conditions))
            return result.scalar_one_or_none()

    async def get_by_name(
        self, workspace_id: str, name: str
    ) -> t.Optional[EnvironmentModel]:
        """Get an environment by its workspace-scoped name."""
        return await self.get(naming.environment_id(workspace_id, name))

    async def list_by_workspace(
        self, workspace_id: str, include_archived: bool = False
    ) -> t.List[EnvironmentModel]:
        """List a workspace's environments, production first then newest.

        Archived rows are retained for audit but hidden by default.
        """
        conditions = [EnvironmentModel.workspace_id == workspace_id]
        if not include_archived:
            conditions.append(EnvironmentModel.status != naming.STATUS_ARCHIVED)

        async with get_session() as session:
            result = await session.execute(
                select(EnvironmentModel)
                .where(*conditions)
                .order_by(
                    EnvironmentModel.env_type.asc(),
                    EnvironmentModel.created_at.desc(),
                )
            )
            return list(result.scalars().all())

    async def list_expired(
        self, now: t.Optional[datetime] = None
    ) -> t.List[EnvironmentModel]:
        """List sandbox environments whose TTL has elapsed (UTC).

        Covers every status that still holds resources, not only ``active``: a
        ``drifted`` or ``failed`` sandbox occupies a branch, a schema set and a
        slot in the workspace quota just the same, so excluding it would make
        expiry silently optional for exactly the environments most likely to be
        abandoned. Environments with an operation in flight are excluded.
        """
        deadline = now or datetime.now(timezone.utc)
        async with get_session() as session:
            result = await session.execute(
                select(EnvironmentModel)
                .where(
                    EnvironmentModel.status.in_(list(naming.RECLAIMABLE_STATUSES)),
                    EnvironmentModel.env_type == naming.ENV_TYPE_SANDBOX,
                    EnvironmentModel.expires_at.is_not(None),
                    EnvironmentModel.expires_at < deadline,
                )
                .order_by(EnvironmentModel.expires_at.asc())
            )
            return list(result.scalars().all())

    async def count_events(
        self,
        environment_id: str,
        action: str,
        since: t.Optional[datetime] = None,
    ) -> int:
        """Count an environment's events of one action, optionally since a time.

        Lets a periodic job stay idempotent without extra columns: "has this
        expiry already been announced?" is answered by the audit trail that had
        to be written anyway.
        """
        conditions = [
            EnvironmentEventModel.environment_id == environment_id,
            EnvironmentEventModel.action == action,
        ]
        if since is not None:
            conditions.append(EnvironmentEventModel.created_at >= since)

        async with get_session() as session:
            result = await session.execute(
                select(func.count())
                .select_from(EnvironmentEventModel)
                .where(*conditions)
            )
            return int(result.scalar_one() or 0)

    async def update(self, environment_id: str, **values: t.Any) -> bool:
        """Update whitelisted columns of one environment."""
        rejected = set(values) - _UPDATABLE_COLUMNS
        if rejected:
            # A programming error, not user input: fail loudly instead of
            # silently ignoring the write.
            raise ValueError(f"environment columns are not updatable: {sorted(rejected)}")
        if not values:
            return False

        values["updated_at"] = datetime.now(timezone.utc)
        async with get_session() as session:
            result = await session.execute(
                update(EnvironmentModel)
                .where(EnvironmentModel.id == environment_id)
                .values(**values)
            )
            return result.rowcount > 0

    async def update_status(
        self, environment_id: str, status: str, *, expected: str = ""
    ) -> bool:
        """Move an environment to a new lifecycle state.

        ``expected`` makes the transition conditional (compare-and-set), which
        is what keeps a long orchestration from resurrecting a row another
        operation has meanwhile moved on: a create that finishes after the
        environment was destroyed must NOT write ``active`` back onto an
        archived row whose Git and SQLMesh resources are already gone.
        """
        conditions = [EnvironmentModel.id == environment_id]
        if expected:
            conditions.append(EnvironmentModel.status == expected)

        async with get_session() as session:
            result = await session.execute(
                update(EnvironmentModel)
                .where(*conditions)
                .values(status=status, updated_at=datetime.now(timezone.utc))
            )
            changed = result.rowcount > 0

        logger.info(
            "environment_status_updated",
            environment_id=environment_id,
            status=status,
            expected=expected,
            changed=changed,
        )
        return changed

    async def record_event(
        self,
        environment_id: str,
        action: str,
        actor_user_id: str = "",
        trigger_type: str = "human",
        succeeded: bool = True,
        commit_hash: str = "",
        detail: t.Optional[t.Dict[str, t.Any]] = None,
        duration_ms: int = 0,
    ) -> str:
        """Append one lifecycle event and return its id.

        Best-effort by contract of the callers: an audit write must never fail
        the business operation, so callers wrap this in their own try/except.
        """
        event_id = str(uuid.uuid4())
        event = EnvironmentEventModel(
            id=event_id,
            environment_id=environment_id,
            action=action,
            actor_user_id=actor_user_id,
            trigger_type=trigger_type,
            commit_hash=commit_hash or None,
            succeeded=1 if succeeded else 0,
            detail_json=json.dumps(detail, ensure_ascii=False) if detail else None,
            duration_ms=duration_ms,
        )
        async with get_session() as session:
            session.add(event)
            await session.flush()
        logger.info(
            "environment_event_recorded",
            environment_id=environment_id,
            action=action,
            succeeded=succeeded,
            trigger_type=trigger_type,
        )
        return event_id

    async def list_events(
        self, environment_id: str, limit: int = 50
    ) -> t.List[EnvironmentEventModel]:
        """List the most recent lifecycle events of an environment."""
        async with get_session() as session:
            result = await session.execute(
                select(EnvironmentEventModel)
                .where(EnvironmentEventModel.environment_id == environment_id)
                .order_by(EnvironmentEventModel.created_at.desc())
                .limit(max(1, min(limit, 500)))
            )
            return list(result.scalars().all())
