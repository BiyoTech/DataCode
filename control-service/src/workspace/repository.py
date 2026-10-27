"""Workspace repository - database CRUD layer.

Encapsulates all DB operations for workspace metadata and user Git credentials.

Membership is deliberately absent: role bindings live in the gateway, which is
the platform's single authorization center. This layer only answers "does this
workspace exist inside this tenant", never "may this user use it".
"""

import typing as t
from datetime import datetime, timezone

# Provider facts are shared with the runtime: this service computes a
# credential's host on creation and the runtime recomputes it on every remote
# operation, so the two MUST agree or a freshly created credential is not found.
from datacode_common.git_providers import credential_host
from sqlalchemy import delete, select, update

from src.utils.logging import get_logger
from src.workspace.database import get_session
from src.workspace.models import UserGitCredentialModel, WorkspaceModel

logger = get_logger(__name__)

__all__ = [
    "credential_host",
    "WorkspaceRepository",
    "UserGitCredentialRepository",
]


class WorkspaceRepository:
    """Database CRUD operations for workspace metadata."""

    async def create(self, workspace: WorkspaceModel) -> WorkspaceModel:
        """Insert a workspace row.

        The owner role binding is recorded by the gateway right after this call
        succeeds; it compensates by deleting the workspace if that fails.
        """
        async with get_session() as session:
            session.add(workspace)
            await session.flush()
            logger.info(
                "workspace_db_created",
                workspace_id=workspace.id,
                tenant_id=workspace.tenant_id,
                owner_user_id=workspace.owner_user_id,
            )
            return workspace

    async def get_by_id(self, workspace_id: str) -> t.Optional[WorkspaceModel]:
        """Get workspace by ID regardless of tenant (internal use only).

        No "include deleted" variant exists: deletion is PHYSICAL (see
        :meth:`purge`), so a row that is absent here is absent everywhere. That
        is what makes recreating a workspace under a previously used name an
        ordinary creation rather than the recycling of a tombstone.
        """
        async with get_session() as session:
            result = await session.execute(
                select(WorkspaceModel).where(WorkspaceModel.id == workspace_id)
            )
            return result.scalar_one_or_none()

    async def list_active(self, limit: int = 0) -> t.List[WorkspaceModel]:
        """Every active workspace of this cell, oldest activity first.

        For the periodic reconciliation sweep, which is cell-wide by nature: the
        lease and sandbox parties are enumerated per cell, and only a pass over
        all workspaces can decide which resources no row accounts for.

        Ordered by ``last_active_at`` ascending so that, when ``limit`` truncates
        the sweep, the workspaces reconciled first are the ones least likely to
        be repaired by ordinary user traffic — an actively used workspace has its
        drift surfaced by the requests hitting it.
        """
        async with get_session() as session:
            query = (
                select(WorkspaceModel)
                .where(WorkspaceModel.status == "active")
                .order_by(WorkspaceModel.last_active_at.asc())
            )
            if limit > 0:
                query = query.limit(limit)
            result = await session.execute(query)
            return list(result.scalars().all())

    async def list_by_ids(
        self, tenant_id: str, workspace_ids: t.Sequence[str], status: str = "active"
    ) -> t.List[WorkspaceModel]:
        """List workspaces of a tenant restricted to an explicit id set.

        The caller (gateway) resolved which workspaces it may see from the role
        bindings, so this is a plain metadata lookup. The tenant filter is kept
        as defence in depth against an id belonging to another tenant.
        """
        if not workspace_ids:
            return []
        async with get_session() as session:
            result = await session.execute(
                select(WorkspaceModel)
                .where(
                    WorkspaceModel.tenant_id == tenant_id,
                    WorkspaceModel.status == status,
                    WorkspaceModel.id.in_(list(workspace_ids)),
                )
                .order_by(WorkspaceModel.last_active_at.desc())
            )
            return list(result.scalars().all())

    async def get_in_tenant(
        self, workspace_id: str, tenant_id: str
    ) -> t.Optional[WorkspaceModel]:
        """Get a workspace inside a tenant, or None.

        Returning None for both "does not exist" and "belongs to another tenant"
        lets callers answer 404 without leaking cross-tenant existence.
        """
        async with get_session() as session:
            result = await session.execute(
                select(WorkspaceModel).where(
                    WorkspaceModel.id == workspace_id,
                    WorkspaceModel.tenant_id == tenant_id,
                )
            )
            return result.scalar_one_or_none()

    async def update_status(self, workspace_id: str, status: str) -> bool:
        """Update workspace status (active / archived / deleted)."""
        async with get_session() as session:
            result = await session.execute(
                update(WorkspaceModel)
                .where(WorkspaceModel.id == workspace_id)
                .values(status=status, updated_at=datetime.now(timezone.utc))
            )
            return result.rowcount > 0

    async def update_last_active(self, workspace_id: str) -> None:
        """Touch last_active_at timestamp."""
        async with get_session() as session:
            await session.execute(
                update(WorkspaceModel)
                .where(WorkspaceModel.id == workspace_id)
                .values(last_active_at=datetime.now(timezone.utc))
            )

    async def update_git_repo(self, workspace_id: str, git_repo_url: str) -> bool:
        """Bind a remote Git repository URL to a workspace."""
        async with get_session() as session:
            result = await session.execute(
                update(WorkspaceModel)
                .where(WorkspaceModel.id == workspace_id)
                .values(
                    git_repo_url=git_repo_url,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            return result.rowcount > 0

    async def update_default_env(self, workspace_id: str, default_env: str) -> bool:
        """Set the environment members land on when opening the workspace."""
        async with get_session() as session:
            result = await session.execute(
                update(WorkspaceModel)
                .where(WorkspaceModel.id == workspace_id)
                .values(
                    default_env=default_env,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            return result.rowcount > 0

    async def purge(self, workspace_id: str) -> bool:
        """Physically delete a workspace row.

        Deletion is physical, not a status flag. A tombstone was worse than
        nothing: it kept occupying the primary key (ids derive from
        ``{tenant}/{name}``) while being invisible to every lookup, so every
        recreation under the same name had to go through a special "reclaim"
        path — the source of two distinct production bugs (an archived
        production environment that could not be opened, and un-stamped
        timestamps that crashed the response).

        Deleting instead means recreating a name is an ordinary creation, and it
        is only safe because the caller releases the resources first (leases,
        working copies, derived data). Audit rows are deliberately NOT cascaded:
        environment events, lease history and task records outlive the object
        they describe, which is the point of an audit trail.
        """
        async with get_session() as session:
            result = await session.execute(
                delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id)
            )
            purged = result.rowcount > 0

        if purged:
            logger.info("workspace_db_purged", workspace_id=workspace_id)
        return purged


class UserGitCredentialRepository:
    """Database CRUD operations for user Git credentials (multi-credential support)."""

    async def create(
        self,
        credential_id: str,
        user_id: str,
        provider: str,
        nickname: str,
        auth_type: str,
        token: str,
        email: str = "",
        username: str = "",
        server_url: str = "",
        is_default: bool = False,
    ) -> UserGitCredentialModel:
        """Create a new Git credential."""
        async with get_session() as session:
            # If this is the first credential for user, auto-set as default
            existing = await session.execute(
                select(UserGitCredentialModel).where(
                    UserGitCredentialModel.user_id == user_id
                )
            )
            if not existing.scalars().first():
                is_default = True

            cred = UserGitCredentialModel(
                id=credential_id,
                user_id=user_id,
                provider=provider,
                nickname=nickname,
                auth_type=auth_type,
                token=token,
                email=email or None,
                username=username or None,
                server_url=server_url or None,
                is_default=1 if is_default else 0,
            )
            session.add(cred)
            await session.flush()
            logger.info(
                "git_credential_created",
                credential_id=credential_id,
                user_id=user_id,
                provider=provider,
                auth_type=auth_type,
            )
            return cred

    async def list_by_user(self, user_id: str) -> t.List[UserGitCredentialModel]:
        """List all Git credentials for a user."""
        async with get_session() as session:
            result = await session.execute(
                select(UserGitCredentialModel)
                .where(UserGitCredentialModel.user_id == user_id)
                .order_by(
                    UserGitCredentialModel.is_default.desc(),
                    UserGitCredentialModel.created_at.desc(),
                )
            )
            return list(result.scalars().all())

    async def get_by_id(self, credential_id: str) -> t.Optional[UserGitCredentialModel]:
        """Get a single credential by ID."""
        async with get_session() as session:
            result = await session.execute(
                select(UserGitCredentialModel).where(
                    UserGitCredentialModel.id == credential_id
                )
            )
            return result.scalar_one_or_none()

    async def get_default(self, user_id: str) -> t.Optional[UserGitCredentialModel]:
        """Get the default credential for a user."""
        async with get_session() as session:
            result = await session.execute(
                select(UserGitCredentialModel).where(
                    UserGitCredentialModel.user_id == user_id,
                    UserGitCredentialModel.is_default == 1,
                )
            )
            return result.scalar_one_or_none()

    async def update(
        self,
        credential_id: str,
        **kwargs: t.Any,
    ) -> bool:
        """Update a credential's fields."""
        async with get_session() as session:
            result = await session.execute(
                select(UserGitCredentialModel).where(
                    UserGitCredentialModel.id == credential_id
                )
            )
            cred = result.scalar_one_or_none()
            if not cred:
                return False

            for key, value in kwargs.items():
                if hasattr(cred, key):
                    setattr(cred, key, value)
            cred.updated_at = datetime.now(timezone.utc)
            session.add(cred)
            logger.info("git_credential_updated", credential_id=credential_id)
            return True

    async def set_default(self, user_id: str, credential_id: str) -> bool:
        """Set a credential as default, unsetting all others for that user."""
        async with get_session() as session:
            # Unset all defaults for this user
            await session.execute(
                update(UserGitCredentialModel)
                .where(UserGitCredentialModel.user_id == user_id)
                .values(is_default=0)
            )
            # Set the target credential as default
            result = await session.execute(
                update(UserGitCredentialModel)
                .where(
                    UserGitCredentialModel.id == credential_id,
                    UserGitCredentialModel.user_id == user_id,
                )
                .values(is_default=1, updated_at=datetime.now(timezone.utc))
            )
            if result.rowcount > 0:
                logger.info(
                    "git_credential_set_default",
                    user_id=user_id,
                    credential_id=credential_id,
                )
                return True
            return False

    async def delete(self, credential_id: str, user_id: str) -> bool:
        """Delete a credential by ID. Reassigns default if needed."""
        async with get_session() as session:
            result = await session.execute(
                select(UserGitCredentialModel).where(
                    UserGitCredentialModel.id == credential_id,
                    UserGitCredentialModel.user_id == user_id,
                )
            )
            cred = result.scalar_one_or_none()
            if not cred:
                return False

            was_default = cred.is_default == 1
            await session.delete(cred)
            await session.flush()

            # If deleted credential was default, assign default to next one
            if was_default:
                remaining = await session.execute(
                    select(UserGitCredentialModel)
                    .where(UserGitCredentialModel.user_id == user_id)
                    .order_by(UserGitCredentialModel.created_at.asc())
                    .limit(1)
                )
                next_cred = remaining.scalar_one_or_none()
                if next_cred:
                    next_cred.is_default = 1
                    session.add(next_cred)

            logger.info(
                "git_credential_deleted",
                credential_id=credential_id,
                user_id=user_id,
            )
            return True

    async def has_oauth(self, user_id: str, provider: str) -> bool:
        """Check if user already has an OAuth credential for a provider."""
        async with get_session() as session:
            result = await session.execute(
                select(UserGitCredentialModel).where(
                    UserGitCredentialModel.user_id == user_id,
                    UserGitCredentialModel.provider == provider,
                    UserGitCredentialModel.auth_type == "oauth",
                )
            )
            return result.scalar_one_or_none() is not None

    async def list_for_host(
        self, user_id: str, host: str
    ) -> t.List[UserGitCredentialModel]:
        """Credentials of ``user_id`` that authenticate against ``host``.

        Host matching is what makes a multi-provider setup correct: a GitHub
        remote must never be pushed with a Gitea token. The host is derived
        from ``server_url`` when present, otherwise from the provider's public
        host. Results keep ``list_by_user`` ordering, so the default credential
        comes first.
        """
        target = (host or "").strip().lower()
        if not target:
            return []
        candidates = await self.list_by_user(user_id)
        return [
            cred
            for cred in candidates
            if credential_host(cred.provider, cred.server_url or "") == target
        ]

    async def get_for_host(
        self, user_id: str, host: str
    ) -> t.Optional[UserGitCredentialModel]:
        """Best credential for ``host`` (default first), or None when unmatched."""
        matches = await self.list_for_host(user_id, host)
        return matches[0] if matches else None

    async def get_for_provider(
        self, user_id: str, provider: str
    ) -> t.Optional[UserGitCredentialModel]:
        """Best credential for ``provider`` (default first), or None."""
        target = (provider or "").strip().lower()
        if not target:
            return None
        for cred in await self.list_by_user(user_id):
            if (cred.provider or "").lower() == target:
                return cred
        return None

    async def get_by_user(self, user_id: str) -> t.Optional[UserGitCredentialModel]:
        """Get the default (or first) credential for a user."""
        cred = await self.get_default(user_id)
        if cred:
            return cred
        # Fallback: return first credential if no default set
        creds = await self.list_by_user(user_id)
        return creds[0] if creds else None
