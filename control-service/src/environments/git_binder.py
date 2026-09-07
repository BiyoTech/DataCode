"""Git binder — the HTTP client for branch and worktree lifecycle (P5).

Same contract as the legacy in-process binder (the registry calls it without
knowing where the work happens), but every filesystem/Git operation is
dispatched to workspace-runtime's ``/internal/env-control/git/*`` API. The
control plane derives PATHS (pure computation over the shared layout helpers)
and delegates EXECUTION.

Two deliberate contract changes versus the in-process era:

* ``first_existing_repo`` / ``first_existing_worktree`` / ``list_member_user_ids``
  are now ``async`` — they round-trip to the runtime, which is the only party
  that can see the disk.
* Path-bearing parameters (``repo_path`` / ``worktree_path``) are parsed back
  into logical identifiers before they hit the wire, so the runtime re-derives
  every path itself and the wire contract never trusts a caller's path.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from datacode_common import workspace_paths as ws_paths

from src.runtime.client import RuntimeClient, get_runtime_client
from src.utils.errors import RuntimeUnavailableError, WorkspacePathInvalidError
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class GitCommandResult:
    """Outcome of a remote Git operation, mirroring the runtime's shape."""

    ok: bool = True
    reason: str = ""
    detail: str = ""


@dataclass(frozen=True)
class WorktreeMaterialization:
    """Outcome of materializing one environment worktree for one user."""

    workspace_path: str
    branch: str
    commit: str
    created: bool


def _ids_from_path(path: str) -> tuple[str, str, str, str]:
    """Parse a platform-layout path into ``(tenant, workspace, user, env)``.

    Raises :class:`WorkspacePathInvalidError` when the path is not part of the
    platform layout — the control plane only ever holds paths the runtime
    itself derived, so anything else is a programming error.
    """
    import os

    from src.config import settings

    try:
        tenant, workspace, user, env = ws_paths.parse_workspace_path(
            os.path.expanduser(settings.workspace_base_path), path
        )
    except ws_paths.WorkspacePathLayoutError as exc:
        raise WorkspacePathInvalidError(path, str(exc)) from exc
    return tenant, workspace, user, env


def _repo_ids(path: str) -> tuple[str, str]:
    """Parse a bare-repo path into ``(tenant, workspace)`` (any user's clone).

    A bare repository lives at ``{base}/{tenant}/{ws}/{user}/.repo`` — outside
    the ``envs/{env}`` layout ``parse_workspace_path`` understands — so the
    segments are read directly.
    """
    import os

    from src.config import settings

    base = os.path.expanduser(settings.workspace_base_path)
    try:
        rel = os.path.relpath(path, base)
    except ValueError as exc:
        raise WorkspacePathInvalidError(path, "path is outside the workspace root") from exc
    segments = rel.split(os.sep)
    if len(segments) != 4 or segments[3] != ws_paths.REPO_DIR:
        raise WorkspacePathInvalidError(
            path, "not a platform bare-repository path ({tenant}/{ws}/{user}/.repo)"
        )
    return segments[0], segments[1]


class GitBinder:
    """Branch and worktree lifecycle, executed by workspace-runtime."""

    def __init__(self, _base_path: str = "", *, client: RuntimeClient | None = None):
        # ``_base_path`` is kept for signature compatibility with the legacy
        # binder; paths are derived from settings inside the helpers.
        self._client = client or get_runtime_client()

    # ---- Path derivation (pure computation, no I/O) ----

    def repo_path(self, tenant_id: str, workspace_id: str, user_id: str) -> str:
        """Bare repository path of one user."""
        import os

        from src.config import settings

        return ws_paths.repo_path(
            os.path.expanduser(settings.workspace_base_path),
            tenant_id,
            workspace_id,
            user_id,
        )

    def worktree_path(
        self, tenant_id: str, workspace_id: str, user_id: str, environment: str
    ) -> str:
        """Working-copy path of one environment for one user."""
        import os

        from src.config import settings

        return ws_paths.environment_workspace_path(
            os.path.expanduser(settings.workspace_base_path),
            tenant_id,
            workspace_id,
            user_id,
            environment,
        )

    # ---- Members (disk facts -> runtime round trips) ----

    async def list_member_user_ids(
        self, *, tenant_id: str, workspace_id: str
    ) -> t.List[str]:
        """User ids that hold a local root of this workspace, sorted."""
        body = await self._client.get(
            "/internal/env-control/git/members",
            params={"tenant_id": tenant_id, "workspace_id": workspace_id},
        )
        return list(body.get("members", []))

    async def first_existing_repo(
        self, *, tenant_id: str, workspace_id: str, members: t.Sequence[str] = ()
    ) -> str:
        """Path of the first member repository present on disk, or ``""``."""
        body = await self._client.get(
            "/internal/env-control/git/first-existing",
            params={"tenant_id": tenant_id, "workspace_id": workspace_id},
        )
        return str(body.get("repo_path", ""))

    async def first_existing_worktree(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        environment: str,
        members: t.Sequence[str] = (),
    ) -> str:
        """Path of the first member working copy of an environment, or ``""``."""
        body = await self._client.get(
            "/internal/env-control/git/first-existing",
            params={
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "environment": environment,
            },
        )
        return str(body.get("worktree_path", ""))

    # ---- Branch state ----

    async def branch_presence(
        self, *, repo_path: str, branch: str
    ) -> t.Tuple[bool, bool]:
        """``(local, remote)`` existence of a branch, read from any clone."""
        tenant, workspace = _repo_ids(repo_path)
        body = await self._client.get(
            "/internal/env-control/git/branch-presence",
            params={
                "tenant_id": tenant,
                "workspace_id": workspace,
                "branch": branch,
            },
        )
        return bool(body.get("local")), bool(body.get("remote"))

    async def list_environment_branches(self, *, repo_path: str) -> t.Set[str]:
        """Environment names backed by a branch in the platform's namespace."""
        tenant, workspace = _repo_ids(repo_path)
        body = await self._client.get(
            "/internal/env-control/git/branches",
            params={"tenant_id": tenant, "workspace_id": workspace},
        )
        return set(body.get("environments", []))

    # ---- Repository & worktrees ----

    async def ensure_repository(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        git_repo_url: str = "",
        bootstrap_branch: str = "",
        fetch: bool = True,
        allow_bootstrap_commit: bool = False,
    ) -> str:
        """Ensure the user's bare repository exists and mirrors the remote.

        ``allow_bootstrap_commit`` lets the caller state that the remote is
        legitimately branchless (just created, or reused while empty), so an
        empty root commit may seed ``bootstrap_branch`` — Git cannot attach a
        worktree to an unborn branch. It is the CALLER's knowledge: the runtime
        cannot distinguish "no branches" from "refs failed to fetch".
        """
        body = await self._client.post(
            "/internal/env-control/git/ensure-repository",
            {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "git_repo_url": git_repo_url,
                "bootstrap_branch": bootstrap_branch,
                "fetch": fetch,
                "allow_bootstrap_commit": allow_bootstrap_commit,
            },
        )
        return str(body.get("repo_path", ""))

    async def ensure_worktree(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        environment: str,
        branch: str,
        start_branch: str = "",
    ) -> WorktreeMaterialization:
        """Ensure ``environment``'s worktree exists for ``user_id``."""
        body = await self._client.post(
            "/internal/env-control/git/ensure-worktree",
            {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "environment": environment,
                "branch": branch,
                "start_branch": start_branch,
            },
        )
        return WorktreeMaterialization(
            workspace_path=str(body.get("workspace_path", "")),
            branch=str(body.get("branch", branch)),
            commit=str(body.get("commit", "")),
            created=bool(body.get("created", False)),
        )

    async def uncommitted_change_count(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        environment: str,
    ) -> int:
        """Count uncommitted changes in one user's worktree of an environment."""
        body = await self._client.get(
            "/internal/env-control/git/uncommitted-count",
            params={
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "environment": environment,
            },
        )
        return int(body.get("count", 0))

    async def remove_worktree(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        environment: str,
    ) -> bool:
        """Remove one user's worktree of an environment (branch is kept)."""
        body = await self._client.post(
            "/internal/env-control/git/remove-worktree",
            {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "environment": environment,
            },
        )
        return bool(body.get("removed", False))

    async def list_environment_worktrees(
        self, *, tenant_id: str, workspace_id: str, user_id: str
    ) -> t.Dict[str, str]:
        """Map ``environment -> branch`` for the worktrees present on disk."""
        body = await self._client.get(
            "/internal/env-control/git/worktrees",
            params={
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
            },
        )
        return dict(body.get("worktrees", {}))

    # ---- Branch operations ----

    async def delete_local_branch(
        self, *, tenant_id: str, workspace_id: str, user_id: str, branch: str
    ) -> bool:
        """Delete the local branch backing an environment."""
        body = await self._client.post(
            "/internal/env-control/git/delete-branch",
            {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "branch": branch,
            },
        )
        return bool(body.get("ok", False))

    async def push_branch(
        self,
        *,
        worktree_path: str,
        branch: str,
        user_id: str,
        git_repo_url: str,
    ) -> GitCommandResult:
        """Publish an environment branch so the whole team can materialize it."""
        body = await self._client.post(
            "/internal/env-control/git/push-branch",
            {
                "worktree_path": worktree_path,
                "branch": branch,
                "user_id": user_id,
                "git_repo_url": git_repo_url,
            },
        )
        return GitCommandResult(
            ok=bool(body.get("ok", False)),
            reason=str(body.get("reason", "")),
            detail=str(body.get("detail", "")),
        )

    async def delete_remote_branch(
        self, *, repo_path: str, branch: str, user_id: str, git_repo_url: str
    ) -> GitCommandResult:
        """Delete an environment branch on the remote.

        ``repo_path`` is parsed for tenancy; the runtime re-derives the actual
        repository, so a stale or absent clone simply fails this member's
        attempt (the registry tries the next member).
        """
        tenant, workspace = _repo_ids(repo_path)
        body = await self._client.post(
            "/internal/env-control/git/delete-remote-branch",
            {
                "tenant_id": tenant,
                "workspace_id": workspace,
                "user_id": user_id,
                "branch": branch,
                "git_repo_url": git_repo_url,
            },
        )
        return GitCommandResult(
            ok=bool(body.get("ok", False)),
            reason=str(body.get("reason", "")),
            detail=str(body.get("detail", "")),
        )

    # ---- Convenience for reconciler commit-drift detection ----

    async def worktree_head(
        self, *, tenant_id: str, workspace_id: str, environment: str
    ) -> str:
        """HEAD commit of the environment's first materialized copy, or ``""``."""
        try:
            body = await self._client.get(
                "/internal/env-control/git/worktree-head",
                params={
                    "tenant_id": tenant_id,
                    "workspace_id": workspace_id,
                    "environment": environment,
                },
            )
        except RuntimeUnavailableError:
            # Commit-drift detection is read-only: an unreachable runtime is
            # "no verdict", not "no drift".
            return ""
        return str(body.get("commit", ""))
