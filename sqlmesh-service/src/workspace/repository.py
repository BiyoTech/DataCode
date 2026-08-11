"""Workspace repository - database CRUD layer.

Encapsulates all DB operations for workspaces and user Git credentials.
"""

import typing as t
from datetime import datetime, timezone

from sqlalchemy import select, update

from src.utils.logging import get_logger
from src.workspace.database import get_session
from src.workspace.models import UserGitCredentialModel, WorkspaceModel

logger = get_logger(__name__)


class WorkspaceRepository:
    """Database CRUD operations for workspace metadata."""

    async def create(self, workspace: WorkspaceModel) -> WorkspaceModel:
        """Insert a new workspace record."""
        async with get_session() as session:
            session.add(workspace)
            await session.flush()
            logger.info("workspace_db_created", workspace_id=workspace.id)
            return workspace

    async def get_by_id(self, workspace_id: str) -> t.Optional[WorkspaceModel]:
        """Get workspace by ID."""
        async with get_session() as session:
            result = await session.execute(
                select(WorkspaceModel).where(WorkspaceModel.id == workspace_id)
            )
            return result.scalar_one_or_none()

    async def list_by_user(
        self, user_id: str, status: str = "active"
    ) -> t.List[WorkspaceModel]:
        """List all workspaces for a user filtered by status."""
        async with get_session() as session:
            result = await session.execute(
                select(WorkspaceModel)
                .where(
                    WorkspaceModel.user_id == user_id,
                    WorkspaceModel.status == status,
                )
                .order_by(WorkspaceModel.last_active_at.desc())
            )
            return list(result.scalars().all())

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
        """Bind a Git repository URL to a workspace."""
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

    async def delete(self, workspace_id: str) -> bool:
        """Soft-delete a workspace (mark as deleted)."""
        return await self.update_status(workspace_id, "deleted")


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

    async def get_by_user(self, user_id: str) -> t.Optional[UserGitCredentialModel]:
        """Get the default (or first) credential for a user.

        Used by WorkspaceManager to obtain Git token for operations.
        """
        cred = await self.get_default(user_id)
        if cred:
            return cred
        # Fallback: return first credential if no default set
        creds = await self.list_by_user(user_id)
        return creds[0] if creds else None
