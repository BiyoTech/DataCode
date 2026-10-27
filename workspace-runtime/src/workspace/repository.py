"""Workspace repository - database CRUD layer.

Encapsulates all DB operations for workspace metadata, user Git credentials and
editor drafts.

Two very different kinds of table live behind this module, and the split drives
what may appear here:

* ``file_drafts`` is OWNED by this service — it is created by ``init_db`` and
  freely written (see :class:`FileDraftRepository`).
* ``workspaces`` and ``user_git_credentials`` are control-service's platform
  metadata. Control creates both tables in its migrate step and performs EVERY
  write to them (workspace lifecycle in ``control-service/src/workspace``,
  credential CRUD in ``control-service/src/api/rest/git_credentials.py``).

This module therefore exposes only READ methods for the two control-owned
tables. The write methods that used to sit here were never called — they were
verbatim copies of control's repository, kept alive by nothing but inertia,
which is exactly how they drifted: the copy of control's workspace-reclaim
routine carried a timestamp bug (an UPDATE never populates the caller's model,
so the response serializer hit ``None.isoformat()``) that had already been fixed
on the control side. (That routine has since been removed outright: workspace
deletion is physical, so there is no tombstone left to reclaim.) Deleting these
methods turns "runtime must not write platform metadata" from a documented
intention into something the code cannot express.

Membership is deliberately absent: role bindings live in the gateway, which is
the platform's single authorization center. This layer only answers "does this
workspace exist inside this tenant", never "may this user use it".
"""

import typing as t
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from src.git.ops import credential_host
from src.utils.logging import get_logger
from src.workspace.database import get_session
from src.workspace.models import (
    FileDraftModel,
    UserGitCredentialModel,
    WorkspaceModel,
)

logger = get_logger(__name__)


class WorkspaceRepository:
    """Read-only access to control-service's ``workspaces`` table.

    The only consumer is the lease wiring (``src.lease.wiring``), which
    resolves a workspace's Git remote from a working-copy path. Everything that
    would create, reclaim or update a workspace lives in control-service.
    """

    async def get_by_id(self, workspace_id: str) -> t.Optional[WorkspaceModel]:
        """Get a non-deleted workspace by ID, or None.

        Deleted rows are always hidden: a deleted workspace has no working
        copies and no live environments left, so a caller resolving a path back
        to a workspace must never see one.
        """
        async with get_session() as session:
            result = await session.execute(
                select(WorkspaceModel).where(
                    WorkspaceModel.id == workspace_id,
                    WorkspaceModel.status != "deleted",
                )
            )
            return result.scalar_one_or_none()


class UserGitCredentialRepository:
    """Read-only access to control-service's ``user_git_credentials`` table.

    Credentials are created, updated and deleted through control-service's REST
    API; this service only resolves the token to authenticate a Git operation
    (push, remote creation, host matching) on a working copy.
    """

    async def list_by_user(self, user_id: str) -> t.List[UserGitCredentialModel]:
        """List all Git credentials of a user, default first then newest."""
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

    async def get_default(self, user_id: str) -> t.Optional[UserGitCredentialModel]:
        """Get the user's default credential, or None."""
        async with get_session() as session:
            result = await session.execute(
                select(UserGitCredentialModel).where(
                    UserGitCredentialModel.user_id == user_id,
                    UserGitCredentialModel.is_default == 1,
                )
            )
            return result.scalar_one_or_none()

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


class FileDraftRepository:
    """Database CRUD for editor draft autosaves.

    One draft per (working copy, file): upserts either insert or overwrite, so
    the frontend's 5-second debounce cannot accumulate duplicate rows. Drafts
    are user-private typing state — the API layer enforces that only the
    working copy's owner reaches these methods.
    """

    async def upsert(
        self, workspace_path: str, file_path: str, content: str
    ) -> datetime:
        """Insert or overwrite the draft for one file; return the stamp (UTC)."""
        stamp = datetime.now(timezone.utc)
        async with get_session() as session:
            existing = (
                await session.execute(
                    select(FileDraftModel).where(
                        FileDraftModel.workspace_path == workspace_path,
                        FileDraftModel.file_path == file_path,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    FileDraftModel(
                        id=str(uuid.uuid4()),
                        workspace_path=workspace_path,
                        file_path=file_path,
                        content=content,
                        updated_at=stamp,
                    )
                )
            else:
                existing.content = content
                existing.updated_at = stamp
        logger.debug(
            "file_draft_upserted",
            workspace_path=workspace_path,
            file_path=file_path,
            bytes=len(content),
        )
        return stamp

    async def get(
        self, workspace_path: str, file_path: str
    ) -> t.Optional[FileDraftModel]:
        """The draft for one file, or ``None`` (the normal case after a save)."""
        async with get_session() as session:
            return (
                await session.execute(
                    select(FileDraftModel).where(
                        FileDraftModel.workspace_path == workspace_path,
                        FileDraftModel.file_path == file_path,
                    )
                )
            ).scalar_one_or_none()

    async def delete(self, workspace_path: str, file_path: str) -> bool:
        """Drop the draft once its file was saved; ``False`` when none existed."""
        async with get_session() as session:
            draft = (
                await session.execute(
                    select(FileDraftModel).where(
                        FileDraftModel.workspace_path == workspace_path,
                        FileDraftModel.file_path == file_path,
                    )
                )
            ).scalar_one_or_none()
            if draft is None:
                return False
            await session.delete(draft)
        return True
