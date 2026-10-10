"""Git binder — the HTTP client for branch and worktree lifecycle.

Same contract as the legacy in-process binder (the registry calls it without
knowing where the work happens), but every filesystem/Git operation is
dispatched to workspace-runtime's ``/internal/env-control/git/*`` API. The
control plane derives PATHS (pure computation over the shared layout helpers)
and delegates EXECUTION.

Three deliberate contract changes versus the in-process era:

* ``first_existing_repo`` / ``first_existing_worktree`` / ``list_member_user_ids``
  are now ``async`` — they round-trip to the runtime, which is the only party
  that can see the disk.
* Path-bearing parameters (``repo_path`` / ``worktree_path``) are parsed back
  into logical identifiers before they hit the wire, so the runtime re-derives
  every path itself and the wire contract never trusts a caller's path.
* Every call declares a :class:`RuntimeTarget`. With one replica that changes
  nothing; with several it is what decides WHICH volume answers, and the
  identifiers are already in hand at every call site (see
  :mod:`src.runtime.routing` for the addressing contract).

Unions are merged here, not in the client
-----------------------------------------
``list_member_user_ids`` and ``list_environment_worktrees`` are answered from
each replica's own disk, so their complete answer is the union over replicas.
The client fans the request out and this module merges, because only the binder
knows the SHAPE of each payload. A replica that failed is logged and skipped:
these feed release paths, where an under-report leaves a worktree behind, so
the incompleteness must be visible in the log rather than silently absorbed.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from datacode_common import workspace_paths as ws_paths

from src.runtime.client import RuntimeClient, RuntimeReply, RuntimeTarget, get_runtime_client
from src.utils.errors import RuntimeUnavailableError, WorkspacePathInvalidError
from src.utils.logging import get_logger

logger = get_logger(__name__)

_ENV_CONTROL_PREFIX = "/internal/env-control/"


def _first_non_empty(bodies: t.Sequence[dict], field: str) -> str:
    """The first non-empty ``field`` across replica answers, or ``""``."""
    for body in bodies:
        value = str(body.get(field) or "")
        if value:
            return value
    return ""


@dataclass(frozen=True)
class GitCommandResult:
    """Outcome of a remote Git operation, mirroring the runtime's shape."""

    ok: bool = True
    reason: str = ""
    detail: str = ""


# Mirrors the runtime's branch-source vocabulary; the runtime decides, this
# side only carries the verdict to the registry and the audit trail.
BRANCH_SOURCE_REUSED_REMOTE = "reused_remote"
BRANCH_SOURCE_ADOPTED_LOCAL = "adopted_local"
BRANCH_SOURCE_CREATED_FROM_BASE = "created_from_base"


@dataclass(frozen=True)
class PendingWork:
    """Work a member's working copy would lose on release (runtime's shape).

    ``unpushed`` is reported, never enforced: the remote branch is always
    preserved, so committed-but-unpushed commits are the only code a destroy
    can still lose — worth telling the user about, not worth refusing over.
    """

    uncommitted: int
    unpushed: int


@dataclass(frozen=True)
class WorktreeMaterialization:
    """Outcome of materializing one environment worktree for one user."""

    workspace_path: str
    branch: str
    commit: str
    created: bool
    # One of the BRANCH_SOURCE_* constants (see the runtime binder).
    branch_source: str


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


def _merged_bodies(replies: t.Sequence[RuntimeReply], *, operation: str) -> list[dict]:
    """Successful payloads of a fan-out, logging the replicas that failed.

    An incomplete union is a real risk on the release paths these feed (a
    member missed here keeps a worktree of an environment the registry
    believes it released), so the gap is recorded rather than absorbed. It is
    not raised: one unreachable replica must not strand the cleanup of the
    others, and the reconciler re-detects what was missed.
    """
    bodies: list[dict] = []
    failed = [reply.addr for reply in replies if not reply.ok]
    for reply in replies:
        if reply.body is not None:
            bodies.append(reply.body)
    if failed:
        logger.warning(
            "env_control_union_incomplete",
            operation=operation,
            unreachable_replicas=failed,
            answered=len(bodies),
        )
    return bodies


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
        """User ids that hold a local root of this workspace, sorted.

        The union over every replica: a member materialized on replica B is
        invisible to replica A, and the release paths that consume this list
        must see all of them.
        """
        replies = await self._client.get_all(
            "/internal/env-control/git/members",
            params={"tenant_id": tenant_id, "workspace_id": workspace_id},
            target=RuntimeTarget(tenant_id=tenant_id, workspace_id=workspace_id),
        )
        members: set[str] = set()
        for body in _merged_bodies(replies, operation="git/members"):
            members.update(str(member) for member in body.get("members", []))
        return sorted(members)

    async def first_existing_repo(
        self, *, tenant_id: str, workspace_id: str, members: t.Sequence[str] = ()
    ) -> str:
        """Path of the first member repository present on any replica, or ``""``."""
        bodies = await self._workspace_probe(
            "/internal/env-control/git/first-existing",
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        return _first_non_empty(bodies, "repo_path")

    async def first_existing_worktree(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        environment: str,
        members: t.Sequence[str] = (),
    ) -> str:
        """Path of the first member working copy of an environment, or ``""``."""
        bodies = await self._workspace_probe(
            "/internal/env-control/git/first-existing",
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            environment=environment,
        )
        return _first_non_empty(bodies, "worktree_path")

    # ---- Branch state ----

    async def branch_presence(
        self, *, repo_path: str, branch: str
    ) -> t.Tuple[bool, bool]:
        """``(local, remote)`` existence of a branch on ANY replica's clone."""
        tenant, workspace = _repo_ids(repo_path)
        bodies = await self._workspace_probe(
            "/internal/env-control/git/branch-presence",
            tenant_id=tenant,
            workspace_id=workspace,
            branch=branch,
        )
        return (
            any(bool(body.get("local")) for body in bodies),
            any(bool(body.get("remote")) for body in bodies),
        )

    async def list_environment_branches(self, *, repo_path: str) -> t.Set[str]:
        """Environment names backed by a branch, unioned across replicas."""
        tenant, workspace = _repo_ids(repo_path)
        bodies = await self._workspace_probe(
            "/internal/env-control/git/branches",
            tenant_id=tenant,
            workspace_id=workspace,
        )
        environments: t.Set[str] = set()
        for body in bodies:
            environments.update(body.get("environments", []))
        return environments

    async def _workspace_probe(
        self, path: str, *, tenant_id: str, workspace_id: str, **params: str
    ) -> list[dict]:
        """Ask every replica a disk-fact question about one workspace.

        A replica without files answers 200 with an empty value, so the
        question is fanned out and the caller merges; "first answer wins"
        would stop at a replica that simply has nothing.
        """
        replies = await self._client.get_all(
            path,
            params={"tenant_id": tenant_id, "workspace_id": workspace_id, **params},
            target=RuntimeTarget(tenant_id=tenant_id, workspace_id=workspace_id),
        )
        return _merged_bodies(replies, operation=path.removeprefix(_ENV_CONTROL_PREFIX))

    # ---- Repository & worktrees ----

    async def ensure_repository(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        environment: str,
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

        ``environment`` does not reach the wire: the bare repository is shared
        by all of a user's environments. It is required to ADDRESS the call —
        this is the step that places the lease, and the lease (and therefore
        the volume the objects land on) is per working copy. Passing the
        environment the caller is about to materialize is what makes the
        objects and the worktree share a replica.
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
            target=RuntimeTarget(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                environment=environment,
            ),
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
            target=RuntimeTarget(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                environment=environment,
            ),
        )
        return WorktreeMaterialization(
            workspace_path=str(body.get("workspace_path", "")),
            branch=str(body.get("branch", branch)),
            commit=str(body.get("commit", "")),
            created=bool(body.get("created", False)),
            branch_source=str(
                body.get("branch_source", BRANCH_SOURCE_CREATED_FROM_BASE)
            ),
        )

    async def pending_work(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        environment: str,
        branch: str,
    ) -> PendingWork:
        """Work one member's copy of an environment would lose on release.

        Asked of every replica and summed: a replica without the copy answers
        zeros, and the replica that HAS it is not necessarily the lease holder
        (the lease may have been released or expired while the files stayed).
        Summing is exact because a copy lives on at most one volume.
        """
        replies = await self._client.get_all(
            "/internal/env-control/git/pending-work",
            params={
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "environment": environment,
                "branch": branch,
            },
            target=RuntimeTarget(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                environment=environment,
            ),
        )
        bodies = _merged_bodies(replies, operation="git/pending-work")
        return PendingWork(
            uncommitted=sum(int(body.get("uncommitted", 0)) for body in bodies),
            unpushed=sum(int(body.get("unpushed", 0)) for body in bodies),
        )

    async def remove_worktree(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        environment: str,
    ) -> bool:
        """Remove one user's worktree of an environment (branch is kept).

        Sent to EVERY replica: a worktree is only released if it is released
        everywhere, and a copy left behind on a replica the request skipped is
        what lets a destroyed environment reappear on that member's next push.
        Idempotent, so replicas holding nothing simply answer ``removed=false``.
        """
        replies = await self._client.post_all(
            "/internal/env-control/git/remove-worktree",
            {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "environment": environment,
            },
            target=RuntimeTarget(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                environment=environment,
            ),
        )
        bodies = _merged_bodies(replies, operation="git/remove-worktree")
        return any(bool(body.get("removed", False)) for body in bodies)

    async def list_environment_worktrees(
        self, *, tenant_id: str, workspace_id: str, user_id: str
    ) -> t.Dict[str, str]:
        """Map ``environment -> branch`` for the worktrees present on disk.

        Merged across replicas for the same reason as the member list: a user's
        environments may be spread over several volumes.
        """
        replies = await self._client.get_all(
            "/internal/env-control/git/worktrees",
            params={
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
            },
            target=RuntimeTarget(
                tenant_id=tenant_id, workspace_id=workspace_id, user_id=user_id
            ),
        )
        worktrees: t.Dict[str, str] = {}
        for body in _merged_bodies(replies, operation="git/worktrees"):
            worktrees.update(dict(body.get("worktrees", {})))
        return worktrees

    # ---- Branch operations ----

    async def delete_local_branch(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        environment: str,
        branch: str,
    ) -> bool:
        """Delete the local branch backing an environment.

        Fanned out like the worktree removal: a surviving local branch on
        another replica can re-publish the environment.
        """
        replies = await self._client.post_all(
            "/internal/env-control/git/delete-branch",
            {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "branch": branch,
            },
            target=RuntimeTarget(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                environment=environment,
            ),
        )
        bodies = _merged_bodies(replies, operation="git/delete-branch")
        return any(bool(body.get("ok", False)) for body in bodies)

    async def push_branch(
        self,
        *,
        worktree_path: str,
        branch: str,
        user_id: str,
        git_repo_url: str,
    ) -> GitCommandResult:
        """Publish an environment branch so the whole team can materialize it."""
        tenant, workspace, path_user, environment = _ids_from_path(worktree_path)
        body = await self._client.post(
            "/internal/env-control/git/push-branch",
            {
                "worktree_path": worktree_path,
                "branch": branch,
                "user_id": user_id,
                "git_repo_url": git_repo_url,
            },
            # Addressed by the copy the push reads FROM (the path's owner),
            # which is not necessarily ``user_id``: the credential may belong
            # to whoever triggered the push while the files belong to whoever
            # materialized them.
            target=RuntimeTarget(
                tenant_id=tenant,
                workspace_id=workspace,
                user_id=path_user,
                environment=environment,
            ),
        )
        return GitCommandResult(
            ok=bool(body.get("ok", False)),
            reason=str(body.get("reason", "")),
            detail=str(body.get("detail", "")),
        )

    # Remote branches are never deleted: the runtime exposes no endpoint for it
    # and destruction releases local state only. See the runtime binder for the
    # full argument.

    # ---- Convenience for reconciler commit-drift detection ----

    async def worktree_head(
        self, *, tenant_id: str, workspace_id: str, environment: str
    ) -> str:
        """HEAD commit of the environment's first materialized copy, or ``""``."""
        try:
            bodies = await self._workspace_probe(
                "/internal/env-control/git/worktree-head",
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                environment=environment,
            )
        except RuntimeUnavailableError:
            # Commit-drift detection is read-only: an unreachable runtime is
            # "no verdict", not "no drift".
            return ""
        return _first_non_empty(bodies, "commit")
