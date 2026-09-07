"""Git binder — branch and worktree lifecycle of an environment.

The ONLY component allowed to translate registry intent into Git state. It is
deliberately free of database access: the registry decides *what* must exist,
the binder makes it so, idempotently.

Layout it maintains for one user of one workspace::

    {user_root}/.repo            bare repository, shared object database
    {user_root}/envs/{env}       one linked worktree per environment

Every step probes before acting, so a failed orchestration can simply be
retried.

P5: this module runs inside workspace-runtime. control-service reaches it
through the ``/internal/env-control/git/*`` API (see
:mod:`src.api.rest.env_control`), which is what keeps the control plane free
of filesystem access.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import typing as t
from dataclasses import dataclass

from datacode_common import workspace_paths as ws_paths
from datacode_common.naming import ENVS_DIR, env_of

from src.git.ops import (
    GitCommandResult,
    GitOperations,
    GitRemoteReason,
    GitWorktreeOperations,
    extract_git_host,
    is_valid_branch_name,
)
from src.utils.errors import GitOperationError, GitRemoteAccessError
from src.utils.logging import get_logger

logger = get_logger(__name__)

REMOTE_NAME = "origin"

# Fetch failures no retry and no stale mirror can rescue: the repository is not
# there, or the credential will not authenticate. They are raised to the caller
# with the remote's own wording, because letting them through produces a failure
# further downstream ("add worktree ...") that names none of the actual cause.
#
# Everything else — notably HOST_UNREACHABLE and UNKNOWN — stays non-fatal: the
# existing mirror is genuinely the better answer for local work, and a transient
# network fault must not block opening a workspace that is already on disk.
_TERMINAL_FETCH_REASONS: frozenset[str] = frozenset(
    {
        GitRemoteReason.REPO_NOT_FOUND,
        GitRemoteReason.INVALID_CREDENTIAL,
        GitRemoteReason.CREDENTIAL_MISSING,
        GitRemoteReason.NO_WRITE_ACCESS,
    }
)


@dataclass(frozen=True)
class WorktreeMaterialization:
    """Outcome of materializing one environment worktree for one user."""

    workspace_path: str
    branch: str
    commit: str
    created: bool


class GitBinder:
    """Maintains the bare repository and the per-environment worktrees."""

    def __init__(self, base_path: str) -> None:
        self._base_path = base_path

    # ---- Path derivation (always runtime-derived, never persisted) ----

    def repo_path(self, tenant_id: str, workspace_id: str, user_id: str) -> str:
        """Bare repository path of one user."""
        return ws_paths.repo_path(self._base_path, tenant_id, workspace_id, user_id)

    def worktree_path(
        self, tenant_id: str, workspace_id: str, user_id: str, environment: str
    ) -> str:
        """Working-copy path of one environment for one user."""
        return ws_paths.environment_workspace_path(
            self._base_path, tenant_id, workspace_id, user_id, environment
        )

    # ---- Members ----

    async def list_member_user_ids(
        self, *, tenant_id: str, workspace_id: str
    ) -> t.List[str]:
        """User ids that hold a local root of this workspace, sorted.

        The platform has no membership table of its own — the gateway owns the
        role bindings — but every member who ever opened the workspace left a
        private root on disk. Enumerating those roots is therefore the only way
        a resource-release operation can find EVERY copy it has to clean up:
        releasing just the caller's copy would leave the other members holding a
        worktree (and a local branch) of an environment that no longer exists,
        and a single ``git push`` from any of them would resurrect the branch.

        Entries that do not look like a user root (no ``.repo`` and no ``envs``)
        or whose name is not a valid path segment are ignored, so unrelated
        files under the shared root can never be mistaken for a member.
        """
        root = ws_paths.workspace_shared_root(self._base_path, tenant_id, workspace_id)
        return await asyncio.to_thread(self._scan_member_roots, root)

    @staticmethod
    def _scan_member_roots(shared_root: str) -> t.List[str]:
        """Filesystem scan behind :meth:`list_member_user_ids` (blocking)."""
        if not os.path.isdir(shared_root):
            return []

        members: t.List[str] = []
        for name in sorted(os.listdir(shared_root)):
            candidate = os.path.join(shared_root, name)
            if not os.path.isdir(candidate):
                continue
            try:
                ws_paths.validate_segment(name, field="user_id")
            except ws_paths.WorkspacePathLayoutError:
                continue
            if os.path.isdir(
                os.path.join(candidate, ws_paths.REPO_DIR)
            ) or os.path.isdir(os.path.join(candidate, ENVS_DIR)):
                members.append(name)
        return members

    def first_existing_repo(
        self, *, tenant_id: str, workspace_id: str, members: t.Sequence[str]
    ) -> str:
        """Path of the first member repository present on disk, or ``""``.

        Operations that only need to READ Git state (does this branch exist?) or
        that talk to the remote can use any member's clone, since all of them
        mirror the same repository.
        """
        for member in members:
            repo = self.repo_path(tenant_id, workspace_id, member)
            if os.path.isdir(repo):
                return repo
        return ""

    def first_existing_worktree(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        environment: str,
        members: t.Sequence[str],
    ) -> str:
        """Path of the first member working copy of an environment, or ``""``.

        Engine access needs a working copy because the connection is declared in
        its ``config.yaml``; every member's copy declares the same
        workspace-level state, so any of them will do.
        """
        for member in members:
            worktree = self.worktree_path(tenant_id, workspace_id, member, environment)
            if os.path.isdir(worktree):
                return worktree
        return ""

    async def branch_presence(
        self, *, repo_path: str, branch: str
    ) -> t.Tuple[bool, bool]:
        """Return ``(local, remote)`` existence of a branch in one repository.

        The remote answer is read from the mirror (``refs/remotes/origin/*``), so
        it is only as fresh as the last fetch — callers that need certainty must
        fetch first. Reporting the mirror is deliberate: it costs no network and
        is what every other Git decision in the platform is already based on.
        """
        self._assert_branch(branch)
        if not repo_path or not os.path.isdir(repo_path):
            return False, False

        local = await asyncio.to_thread(
            GitWorktreeOperations.local_branch_exists, repo_path, branch
        )
        remote = await asyncio.to_thread(
            GitWorktreeOperations.remote_branch_exists, repo_path, branch, REMOTE_NAME
        )
        return bool(local), bool(remote)

    async def list_environment_branches(self, *, repo_path: str) -> t.Set[str]:
        """Environment names backed by a branch in the platform's namespace.

        Scans local heads AND the remote mirror: an orphan may exist on either
        side (a failed create that never pushed, or a branch pushed from outside
        the platform). Branches outside the namespace are not environments and
        are ignored (design §11.2).
        """
        if not repo_path or not os.path.isdir(repo_path):
            return set()

        refs = await asyncio.to_thread(
            GitWorktreeOperations.list_branch_refs, repo_path, REMOTE_NAME
        )
        environments = set()
        for ref in refs:
            environment = env_of(ref)
            if environment:
                environments.add(environment)
        return environments

    # ---- Repository ----

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
        """Ensure the user's bare repository exists and is up to date.

        With ``git_repo_url`` the remote is (re)pointed and fetched, which is
        what makes every branch of the workspace visible as
        ``refs/remotes/origin/*``. Without a remote (bootstrap window, before the
        owner binds Git) an empty root commit is created on ``bootstrap_branch``
        so a worktree can be attached at all.

        ``allow_bootstrap_commit`` extends that last step to the case where a
        remote IS configured but carries no branch yet (a freshly created or
        genuinely empty repository). Git cannot attach a worktree to an unborn
        branch, so without this the caller would fail with "branch 'main' exists
        neither locally nor on origin" — a symptom that says nothing about the
        real situation. The commit is EMPTY: nothing is scaffolded here.
        """
        repo = self.repo_path(tenant_id, workspace_id, user_id)
        created = await asyncio.to_thread(
            GitWorktreeOperations.init_bare, repo, bootstrap_branch or "main"
        )
        if not created:
            raise GitOperationError("init bare repository", detail=repo)

        if git_repo_url:
            await self._repoint_remote(
                repo,
                git_repo_url,
                workspace_id=workspace_id,
                bootstrap_branch=bootstrap_branch,
            )
            if fetch:
                await self._fetch_remote(
                    repo,
                    git_repo_url,
                    workspace_id=workspace_id,
                    user_id=user_id,
                )

        branch = bootstrap_branch
        if branch and (allow_bootstrap_commit or not git_repo_url):
            await self._ensure_bootstrap_commit(repo, branch, git_repo_url=git_repo_url)

        return repo

    async def _repoint_remote(
        self,
        repo: str,
        git_repo_url: str,
        *,
        workspace_id: str,
        bootstrap_branch: str,
    ) -> None:
        """Point ``origin`` at ``git_repo_url``, reporting a changed binding.

        A bare repository outlives the URL bound to it, and ``_add_worktree``
        prefers an existing LOCAL branch over the remote — so a clone that was
        built from another repository would keep serving that repository's code
        while the registry names the new one.

        The stale local head is deliberately NOT deleted. The one supported way
        to change a workspace's remote is ``bind_repository``, which PUSHES the
        local code to the new remote first: there the local branch is the
        authoritative copy, and dropping it would destroy the very commits about
        to be published. (Git also refuses to delete a branch checked out in a
        worktree, so the attempt could only ever half-succeed.) The other way a
        clone could outlive its remote — deleting a workspace and recreating it
        with the same name but a different URL — is closed at the source: the
        deletion removes the whole local root.

        What remains is therefore the report: a divergence between disk and
        registry that nobody could see before is now a warning naming both URLs.
        ``fetch --prune`` immediately afterwards drops the old remote-tracking
        refs, so branch resolution stops seeing the previous repository.
        """
        previous = await asyncio.to_thread(GitOperations.remote_url, repo, REMOTE_NAME)
        await asyncio.to_thread(
            GitOperations.add_remote, repo, git_repo_url, REMOTE_NAME
        )
        if not previous or previous == git_repo_url:
            return

        logger.warning(
            "environment_repo_remote_changed",
            workspace_id=workspace_id,
            repo=repo,
            # URLs are normalized and credential-stripped before this layer.
            previous_url=previous,
            new_url=git_repo_url,
            local_branch_retained=(
                bool(bootstrap_branch)
                and GitWorktreeOperations.local_branch_exists(repo, bootstrap_branch)
            ),
        )

    async def _fetch_remote(
        self, repo: str, git_repo_url: str, *, workspace_id: str, user_id: str
    ) -> None:
        """Fetch ``origin``, escalating the failures a retry cannot fix.

        The distinction matters more than the fetch itself. A stale mirror is
        genuinely usable for LOCAL work, which is why this used to be logged and
        swallowed — but that also swallowed "this repository does not exist" and
        "your token was rejected", and the request then failed further
        downstream as "add worktree for environment prod", carrying none of the
        actual reason. Terminal reasons are therefore raised HERE, where the
        remote's own words are still in hand; transient ones keep the old
        behaviour because the mirror really is the better answer then.
        """
        auth = await self._auth_for(user_id, git_repo_url)
        result = await asyncio.to_thread(
            GitOperations.fetch, repo, REMOTE_NAME, True, auth
        )
        if result.ok:
            return

        if result.reason in _TERMINAL_FETCH_REASONS:
            logger.error(
                "environment_repo_fetch_rejected",
                workspace_id=workspace_id,
                user_id=user_id,
                reason=result.reason,
            )
            raise GitRemoteAccessError(
                "fetch",
                result.reason,
                extract_git_host(git_repo_url),
                result.detail,
            )

        logger.warning(
            "environment_repo_fetch_failed",
            workspace_id=workspace_id,
            user_id=user_id,
            reason=result.reason,
        )

    async def _ensure_bootstrap_commit(
        self, repo: str, branch: str, *, git_repo_url: str
    ) -> None:
        """Create an empty root commit on ``branch`` when nothing else can seed it.

        Skipped as soon as the branch exists locally or on the mirror: seeding a
        second root commit would fork the history of a repository that already
        has one.
        """
        if GitWorktreeOperations.local_branch_exists(repo, branch):
            return
        if git_repo_url and GitWorktreeOperations.remote_branch_exists(
            repo, branch, REMOTE_NAME
        ):
            return

        commit = await asyncio.to_thread(
            GitWorktreeOperations.create_empty_root_commit, repo, branch
        )
        if not commit:
            raise GitOperationError("create bootstrap commit", detail=branch)
        logger.info(
            "environment_repo_bootstrap_commit_created",
            repo=repo,
            branch=branch,
            has_remote=bool(git_repo_url),
        )

    # ---- Worktrees ----

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
        """Ensure ``environment``'s worktree exists for ``user_id``.

        ``start_branch`` is only consulted when the environment branch does not
        exist yet (environment creation); re-opening an existing environment must
        never silently re-create its branch from the base.
        """
        self._assert_branch(branch)
        if start_branch:
            self._assert_branch(start_branch)

        repo = self.repo_path(tenant_id, workspace_id, user_id)
        target = self.worktree_path(tenant_id, workspace_id, user_id, environment)

        registered = await self._registered_worktree_paths(repo)
        if os.path.realpath(target) in registered and os.path.isdir(target):
            commit = await asyncio.to_thread(GitWorktreeOperations.head_commit, target)
            return WorktreeMaterialization(target, branch, commit, created=False)

        # A leftover directory that Git does not know about is disposable
        # residue (interrupted creation, manual copy): clear it so `worktree
        # add` has a clean slot.
        if os.path.isdir(target):
            logger.warning(
                "environment_worktree_directory_reclaimed",
                workspace_id=workspace_id,
                environment=environment,
                path=target,
            )
            await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
        await asyncio.to_thread(GitWorktreeOperations.worktree_prune, repo)

        result = await self._add_worktree(repo, target, branch, start_branch)
        if not result.ok:
            # The reason travels with the error: an "add worktree" failure whose
            # only content is the operation name is the report users could do
            # nothing with. Terminal remote failures never reach here anymore
            # (ensure_repository raises them first), so what lands here is a
            # genuine local Git problem, and its own wording is the diagnosis.
            raise GitOperationError(
                f"add worktree for environment {environment}",
                detail=result.detail,
                reason=result.reason or GitRemoteReason.UNKNOWN,
            )

        commit = await asyncio.to_thread(GitWorktreeOperations.head_commit, target)
        logger.info(
            "environment_worktree_materialized",
            workspace_id=workspace_id,
            user_id=user_id,
            environment=environment,
            branch=branch,
            commit=commit[:8],
        )
        return WorktreeMaterialization(target, branch, commit, created=True)

    async def _add_worktree(
        self, repo: str, target: str, branch: str, start_branch: str
    ) -> GitCommandResult:
        """Pick the right start point and add the worktree."""
        remote_branch = f"{REMOTE_NAME}/{branch}"
        if GitWorktreeOperations.local_branch_exists(repo, branch):
            return await asyncio.to_thread(
                GitWorktreeOperations.worktree_add, repo, target, branch
            )
        if GitWorktreeOperations.remote_branch_exists(repo, branch, REMOTE_NAME):
            # The environment already exists for the team: track its remote.
            return await asyncio.to_thread(
                GitWorktreeOperations.worktree_add,
                repo,
                target,
                branch,
                remote_branch,
                True,
            )
        if not start_branch:
            return GitCommandResult(
                ok=False,
                reason=GitRemoteReason.BRANCH_NOT_FOUND,
                detail=f"branch {branch!r} exists neither locally nor on {REMOTE_NAME}",
            )

        start_point = (
            f"{REMOTE_NAME}/{start_branch}"
            if GitWorktreeOperations.remote_branch_exists(
                repo, start_branch, REMOTE_NAME
            )
            else start_branch
        )
        return await asyncio.to_thread(
            GitWorktreeOperations.worktree_add, repo, target, branch, start_point
        )

    async def uncommitted_change_count(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        environment: str,
    ) -> int:
        """Count uncommitted changes in one user's worktree of an environment.

        Worktree removal discards them, so destruction probes this first to
        give the user a chance to keep their work (design §13).
        """
        target = self.worktree_path(tenant_id, workspace_id, user_id, environment)
        if not os.path.isdir(target):
            return 0
        changes = await asyncio.to_thread(GitOperations.list_changes, target)
        return len(changes)

    async def remove_worktree(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        environment: str,
    ) -> bool:
        """Remove one user's worktree of an environment (branch is kept)."""
        repo = self.repo_path(tenant_id, workspace_id, user_id)
        target = self.worktree_path(tenant_id, workspace_id, user_id, environment)
        if not os.path.isdir(target):
            return False

        result = await asyncio.to_thread(
            GitWorktreeOperations.worktree_remove, repo, target, True
        )
        if not result.ok:
            # Fall back to a plain directory removal: the worktree is disposable
            # and its administrative record is pruned right after.
            await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
            await asyncio.to_thread(GitWorktreeOperations.worktree_prune, repo)
        logger.info(
            "environment_worktree_removed",
            workspace_id=workspace_id,
            user_id=user_id,
            environment=environment,
        )
        return True

    async def list_environment_worktrees(
        self, *, tenant_id: str, workspace_id: str, user_id: str
    ) -> t.Dict[str, str]:
        """Map ``environment -> branch`` for the worktrees present on disk.

        Used to reconcile the registry against Git for one user.
        """
        repo = self.repo_path(tenant_id, workspace_id, user_id)
        if not os.path.isdir(repo):
            return {}

        entries = await asyncio.to_thread(GitWorktreeOperations.worktree_list, repo)
        found: t.Dict[str, str] = {}
        for entry in entries:
            if entry.bare or not entry.branch:
                continue
            try:
                _, _, _, environment = ws_paths.parse_workspace_path(
                    self._base_path, entry.path
                )
            except ws_paths.WorkspacePathLayoutError:
                # A worktree outside the platform layout is not ours to manage.
                continue
            found[environment] = entry.branch
        return found

    # ---- Branch operations ----

    async def delete_local_branch(
        self, *, tenant_id: str, workspace_id: str, user_id: str, branch: str
    ) -> bool:
        """Delete the local branch backing an environment."""
        self._assert_branch(branch)
        repo = self.repo_path(tenant_id, workspace_id, user_id)
        result = await asyncio.to_thread(
            GitOperations.delete_branch, repo, branch, True
        )
        return result.ok

    async def push_branch(
        self,
        *,
        worktree_path: str,
        branch: str,
        user_id: str,
        git_repo_url: str,
    ) -> GitCommandResult:
        """Publish an environment branch so the whole team can materialize it."""
        self._assert_branch(branch)
        auth = await self._auth_for(user_id, git_repo_url)
        if auth is None:
            return GitCommandResult(
                ok=False,
                reason=GitRemoteReason.CREDENTIAL_MISSING,
                detail="no Git credential configured for this remote",
            )

        from src.git.ops import push_async

        return await push_async(worktree_path, branch, REMOTE_NAME, auth)

    async def delete_remote_branch(
        self, *, repo_path: str, branch: str, user_id: str, git_repo_url: str
    ) -> GitCommandResult:
        """Delete an environment branch on the remote."""
        self._assert_branch(branch)
        auth = await self._auth_for(user_id, git_repo_url)

        from src.git.ops import run_git_async

        result = await run_git_async(
            repo_path, ["push", REMOTE_NAME, "--delete", branch], auth
        )
        if result.returncode == 0:
            logger.info("environment_remote_branch_deleted", branch=branch)
            return GitCommandResult(ok=True)
        logger.warning(
            "environment_remote_branch_delete_failed",
            branch=branch,
            stderr=result.stderr,
        )
        return GitCommandResult(
            ok=False, reason=GitRemoteReason.UNKNOWN, detail=result.stderr
        )

    # ---- Internals ----

    @staticmethod
    def _assert_branch(branch: str) -> None:
        """Reject any ref that git could mistake for an option."""
        if not is_valid_branch_name(branch):
            raise GitOperationError("validate branch name", detail=branch)

    @staticmethod
    async def _auth_for(user_id: str, git_repo_url: str):
        """Resolve the credential matching the remote's host (may be None)."""
        if not git_repo_url:
            return None
        from src.workspace.git_credentials import resolve_auth_for_url

        resolved = await resolve_auth_for_url(user_id, git_repo_url)
        return resolved.auth

    @staticmethod
    async def _registered_worktree_paths(repo: str) -> t.Set[str]:
        """Real paths Git currently accounts for in ``repo``."""
        if not os.path.isdir(repo):
            return set()
        entries = await asyncio.to_thread(GitWorktreeOperations.worktree_list, repo)
        return {os.path.realpath(entry.path) for entry in entries if not entry.bare}
