"""Workspace provisioning — the filesystem side of workspace lifecycle.

The control plane (control-service) owns the database rows and the environment
state machine; every operation that touches the filesystem or Git runs HERE, in
the runtime, reached through the ``/internal/env-control/workspace/*`` API. This
split is what makes control-service free of filesystem access and freely
horizontally scalable.

Ported from the legacy sqlmesh-service WorkspaceManager: the DB logic
stayed in control-service, the disk/Git logic lives in this module.
"""

from __future__ import annotations

import asyncio
import functools
import os
import shutil
import sys
import typing as t
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from datacode_common import workspace_paths as ws_paths
from datacode_common.git_reasons import GitRemoteReason
from datacode_common.naming import ENVS_DIR

from src.git.ops import (
    GitAuth,
    GitCommandResult,
    GitOperations,
    check_remote_write_access,
    extract_git_host,
    gitlab_namespace_id,
    ls_remote_async,
    ls_remote_branches_async,
    parse_repo_path,
    push_async,
    token_account_login,
    validate_git_repo_url_async,
)
from src.utils.errors import GitRemoteAccessError
from src.utils.logging import get_logger

logger = get_logger(__name__)

# The ``sqlmesh`` console script: how the scaffold asks SQLMesh to generate a
# project, and the name to look for next to the running interpreter.
_SQLMESH_SCRIPT = "sqlmesh"

# Runs the CLI through the entry point its console script points at
# (``sqlmesh.cli.main:cli``). Used only when no script file exists, so the
# generated project never depends on setuptools having written one.
_SQLMESH_MODULE_BOOTSTRAP = "from sqlmesh.cli.main import cli; cli()"

# Seconds allowed for one ``sqlmesh init``. The command only writes files, so a
# runaway here means the interpreter or the import of SQLMesh itself is stuck —
# and a workspace creation request must not hang on it.
_SQLMESH_INIT_TIMEOUT_SECONDS = 30


class RemoteProvisionState(str, Enum):
    """What the bound remote turned out to be, and therefore what to do next."""

    # We created it: empty and private, so the platform scaffold becomes its
    # first commit.
    CREATED_EMPTY = "created_empty"
    # It already carries code: reuse it, and scaffold NOTHING.
    REUSABLE = "reusable"
    # It exists but has no branch: nothing to reuse and nothing to overwrite, so
    # the user gets an empty working copy — still no scaffold.
    REMOTE_EMPTY = "remote_empty"


# States in which the caller must seed an empty root commit before a worktree
# can be attached (git cannot check out an unborn branch), and in which no
# template may be written.
_NO_SCAFFOLD_STATES = frozenset({RemoteProvisionState.REUSABLE, RemoteProvisionState.REMOTE_EMPTY})


@dataclass(frozen=True)
class RemoteProvision:
    """Outcome of resolving a workspace's remote before anything is written."""

    state: RemoteProvisionState
    normalized_url: str
    # Branch the reusable remote publishes ("" when there is none).
    default_branch: str = ""
    # True only when this call created the repository.
    created: bool = False

    @property
    def needs_scaffold(self) -> bool:
        """Whether the platform template must be generated for this workspace.

        The single place this question is answered, so "reuse the user's code"
        cannot be re-derived (and mis-derived) by each caller.
        """
        return self.state not in _NO_SCAFFOLD_STATES

    @property
    def needs_bootstrap_commit(self) -> bool:
        """Whether an empty root commit is required to attach a worktree."""
        return self.state in (
            RemoteProvisionState.CREATED_EMPTY,
            RemoteProvisionState.REMOTE_EMPTY,
        )


class WorkspaceProvisioner:
    """Filesystem and Git operations behind workspace lifecycle decisions."""

    def __init__(self) -> None:
        pass

    @property
    def _base_path(self) -> str:
        """Resolve the workspace base lazily on every use.

        Configuration can change (tests, config reload); capturing the path at
        construction time would pin the provisioner to a stale root.
        """
        from src.config import settings

        return os.path.expanduser(settings.workspace_base_path)

    # ---- Scaffold ----

    async def scaffold_and_commit(
        self, workspace_path: str, name: str, dialect: str, workspace_id: str
    ) -> None:
        """Scaffold the dual-directory layout and commit it.

        A workspace root (i.e. one environment worktree) contains two sibling
        projects: ``sql-warehouse/`` (the SQLMesh project) and ``asset-dag/``
        (the Dagster Data+AI pipeline project). The worktree is already a Git
        working tree, so nothing is initialized here — only filled in and
        committed, which is what makes ``main`` carry the scaffold from the
        very first commit.
        """
        await asyncio.to_thread(self._scaffold_project, workspace_path, name, dialect, workspace_id)
        await asyncio.to_thread(self._commit_scaffold, workspace_path)

    def _scaffold_project(
        self, path: str, name: str, dialect: str = "duckdb", workspace_id: str = ""
    ) -> None:
        """Scaffold the dual-directory layout inside an environment worktree.

        The ``.gitignore`` is written BEFORE the projects so the very first
        commit already excludes runtime output (caches, logs, the error archive)
        instead of tracking it and needing a follow-up removal.
        """
        from src.files.asset_dag_skeleton import scaffold_asset_dag
        from src.files.layout import (
            ASSET_DAG_DIR,
            SQL_WAREHOUSE_DIR,
            ensure_gitignore,
        )

        warehouse_path = os.path.join(path, SQL_WAREHOUSE_DIR)
        asset_dag_path = os.path.join(path, ASSET_DAG_DIR)

        ensure_gitignore(path)
        self._scaffold_sqlmesh(warehouse_path, name, dialect, workspace_id)
        scaffold_asset_dag(asset_dag_path, name)

    def _sqlmesh_init_command(self, dialect: str) -> list[str]:
        """``sqlmesh init <dialect>``, resolved against THIS interpreter.

        Why not the bare name
        ---------------------
        The ``sqlmesh`` script is installed INTO the environment that owns
        ``sys.executable``, but this service is started as
        ``<svc>/.venv/bin/python -m uvicorn`` WITHOUT activating the
        virtualenv — so ``.venv/bin``, the only directory holding that script,
        is missing from PATH. ``subprocess.run(["sqlmesh", ...])`` then dies with
        ``FileNotFoundError`` before SQLMesh runs, and the caller falls back to a
        hand-written skeleton: a workspace whose ``models/`` is empty and whose
        ``config.yaml`` carries none of what ``sqlmesh init`` writes. The failure
        is silent from the user's side because the workspace is still created.

        So the script's own directory is searched FIRST, and only then PATH —
        the same resolution ``src.codeloc.process.dagster_search_path`` performs
        for the ``dagster*`` scripts. Resolved through ``shutil.which`` rather
        than by joining paths, so the script is only accepted when it is
        actually executable.

        Last resort is the entry point itself, invoked by the same interpreter:
        a package can be importable while no console script was generated, and
        importing SQLMesh through ``sys.executable`` is what proves the project
        is generated by the SQLMesh THIS service runs, not by another one.
        """
        interpreter_bin = str(Path(sys.executable).absolute().parent)
        resolved = shutil.which(_SQLMESH_SCRIPT, path=interpreter_bin) or shutil.which(
            _SQLMESH_SCRIPT
        )
        if resolved:
            return [resolved, "init", dialect]
        return [sys.executable, "-c", _SQLMESH_MODULE_BOOTSTRAP, "init", dialect]

    @staticmethod
    def _sqlmesh_init_env() -> dict[str, str]:
        """Environment for the init subprocess.

        Inherits this service's environment unchanged except for analytics: the
        CLI emits anonymized telemetry and flushes it in an ``atexit`` hook,
        which would put an outbound request — and therefore the operator's proxy
        settings and a network round trip — inside a synchronous
        workspace-creation request that only needs to write files.
        """
        return {**os.environ, "SQLMESH__DISABLE_ANONYMIZED_ANALYTICS": "true"}

    def _scaffold_sqlmesh(
        self, path: str, name: str, dialect: str = "duckdb", workspace_id: str = ""
    ) -> None:
        """Create the SQLMesh project structure by calling ``sqlmesh init``."""
        import subprocess

        os.makedirs(path, exist_ok=True)
        models_dir = os.path.join(path, "models")
        if os.path.exists(models_dir):
            return  # Already scaffolded

        # An empty dialect would make ``init`` prompt for an engine instead of
        # generating a project, so it is resolved to the documented default here
        # rather than passed on.
        command = self._sqlmesh_init_command(dialect or "duckdb")
        try:
            result = subprocess.run(
                command,
                cwd=path,
                capture_output=True,
                text=True,
                timeout=_SQLMESH_INIT_TIMEOUT_SECONDS,
                # No terminal here: any prompt the CLI still reaches would block
                # a workspace-creation request for the whole timeout, where an
                # EOF makes it fail fast and land in the logged fallback.
                stdin=subprocess.DEVNULL,
                env=self._sqlmesh_init_env(),
            )
            if result.returncode == 0:
                self._inject_project_name(path, name)
                logger.info("workspace_scaffolded_via_cli", path=path, dialect=dialect)
            else:
                logger.error(
                    "sqlmesh_init_failed",
                    path=path,
                    dialect=dialect,
                    command=" ".join(command),
                    stderr=result.stderr,
                )
                self._scaffold_fallback(path, name, dialect)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning(
                "sqlmesh_cli_unavailable",
                command=" ".join(command),
                error=str(e),
            )
            self._scaffold_fallback(path, name, dialect)

        # Write the workspace's managed SQLMesh state location into the generated
        # config.yaml. Doing it HERE — once, before the first commit — is what
        # makes the file the single truth: it is committed, flows to every member
        # and every environment through Git, and is only ever verified (never
        # rewritten) at runtime.
        from src.sqlmesh.config_ops import reconcile_state_schema

        reconcile_state_schema(path, workspace_id)

    def _scaffold_fallback(self, path: str, name: str, dialect: str) -> None:
        """Fallback scaffold when sqlmesh CLI is not available."""
        for subdir in ("models", "audits", "tests", "seeds", "macros"):
            os.makedirs(os.path.join(path, subdir), exist_ok=True)

        config_path = os.path.join(path, "config.yaml")
        if not os.path.exists(config_path):
            config_content = f"""# SQLMesh project: {name}
project: {name}

gateways:
  default:
    connection:
      type: {dialect}

default_gateway: default

model_defaults:
  dialect: {dialect}
  start: "2024-01-01"
  cron: '@daily'
"""
            with open(config_path, "w") as f:
                f.write(config_content)

        logger.info("workspace_scaffolded_fallback", path=path, dialect=dialect)

    def _inject_project_name(self, path: str, name: str) -> None:
        """Inject project name into an existing config.yaml.

        After `sqlmesh init` creates the scaffold, the generated config.yaml may
        not contain a ``project`` key. This method reads the file, inserts the
        ``project: <name>`` entry right after the first comment line (or at the
        top of the file), and writes it back.
        """
        config_path = os.path.join(path, "config.yaml")
        if not os.path.exists(config_path):
            logger.warning("config_yaml_not_found_for_inject", path=path)
            return

        with open(config_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Check if project key already exists
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("project:") and not stripped.startswith("#"):
                logger.info("project_name_already_in_config", path=path)
                return

        # Insert project line after the first comment block / at the top
        insert_idx = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                insert_idx = i
                break

        lines.insert(insert_idx, f"\nproject: {name}\n")

        with open(config_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        logger.info("project_name_injected_into_config", path=path, name=name)

    @staticmethod
    def _commit_scaffold(workspace_path: str) -> None:
        """Commit the freshly generated scaffold onto the production branch.

        The worktree starts from an empty root commit (or from the remote's
        ``main``), so the scaffold has to be committed before it can be pushed
        or branched from.
        """
        GitOperations.commit_all(workspace_path, "Initialize workspace scaffold")

    # ---- Remote provisioning (create-or-reuse) ----

    async def provision_remote(
        self, *, git_repo_url: str, user_id: str, private: bool = True
    ) -> RemoteProvision:
        """Decide, and enact, what the bound remote means for workspace creation.

        The URL a user types is an INTENT, not a precondition. Three situations
        follow from it and each implies a different creation path:

        * the repository does not exist -> create it (private, empty) and let the
          caller scaffold, so the workspace starts from the platform template;
        * it exists and has branches    -> REUSE its code; the caller must not
          scaffold, or the platform would overwrite a real project;
        * it exists and has no branches -> also reuse (nothing to overwrite, and
          nothing to reuse either): the caller produces an empty working copy.

        Deciding this HERE, before anything is written, is what makes the outcome
        predictable. Previously the URL was only touched after the scaffold had
        been committed, so a non-existent repository surfaced as a failed fetch
        and then as an unexplained worktree error.
        """
        from src.workspace.git_credentials import resolve_auth_for_url

        normalized_url = await validate_git_repo_url_async(git_repo_url)
        host = extract_git_host(normalized_url)

        resolved = await resolve_auth_for_url(user_id, normalized_url)
        if resolved.auth is None:
            # Creation and private-repository access both need a credential, and
            # an anonymous probe of a private repository is indistinguishable
            # from a missing one — so this is refused rather than guessed at.
            raise GitRemoteAccessError(
                "provision",
                GitRemoteReason.CREDENTIAL_MISSING,
                host,
                f"no credential configured for {resolved.host or host}",
            )

        exists = await self._remote_exists(normalized_url, resolved.auth, resolved.server_url)
        if not exists:
            return await self._create_remote(
                normalized_url=normalized_url,
                host=host,
                resolved=resolved,
                private=private,
            )

        branches = await ls_remote_branches_async(normalized_url, resolved.auth)
        if not branches:
            logger.info(
                "workspace_remote_provisioned",
                state=RemoteProvisionState.REMOTE_EMPTY.value,
                host=host,
                user_id=user_id,
            )
            return RemoteProvision(
                state=RemoteProvisionState.REMOTE_EMPTY,
                normalized_url=normalized_url,
            )

        default_branch = "main" if "main" in branches else branches[0]
        logger.info(
            "workspace_remote_provisioned",
            state=RemoteProvisionState.REUSABLE.value,
            host=host,
            user_id=user_id,
            branch_count=len(branches),
            default_branch=default_branch,
        )
        return RemoteProvision(
            state=RemoteProvisionState.REUSABLE,
            normalized_url=normalized_url,
            default_branch=default_branch,
        )

    async def _remote_exists(self, normalized_url: str, auth: GitAuth, server_url: str) -> bool:
        """Whether the repository is there, refusing to guess when unsure.

        The provider API answers this precisely (404 = absent, 401/403 = the
        credential is the problem, not the repository), so it is asked first. A
        provider we cannot probe falls back to ``ls-remote``, whose success is
        itself proof of existence — and whose failure is classified, so "not
        found" is separated from "your token was rejected". Only a genuine
        "not found" leads to creation: creating on top of an unreadable
        repository would be a second repository at an address the user believes
        is already theirs.
        """
        check = await check_remote_write_access(normalized_url, auth, server_url)
        if check.performed:
            if check.ok:
                return True
            if check.reason == GitRemoteReason.REPO_NOT_FOUND:
                return False
            raise GitRemoteAccessError(
                "provision",
                check.reason,
                extract_git_host(normalized_url),
                check.detail,
            )

        probe = await ls_remote_async(normalized_url, auth)
        if probe.ok:
            return True
        if probe.reason == GitRemoteReason.REPO_NOT_FOUND:
            return False
        raise GitRemoteAccessError(
            "provision",
            probe.reason,
            extract_git_host(normalized_url),
            probe.detail,
        )

    async def _create_remote(
        self,
        *,
        normalized_url: str,
        host: str,
        resolved: t.Any,
        private: bool,
    ) -> RemoteProvision:
        """Create the repository at exactly the address the user asked for.

        The owner is taken from the URL and never rewritten. If it names an
        organization the token may not create in, that is reported as such: a
        fallback to the token's personal account would hand back a workspace
        bound to a repository nobody asked for, and the URL the user typed would
        silently mean something else.
        """
        owner, repo_name = parse_repo_path(normalized_url)
        if not owner or not repo_name:
            raise GitRemoteAccessError(
                "provision",
                GitRemoteReason.REPO_CREATION_FAILED,
                host,
                "the URL does not name an owner and a repository",
            )

        provider = (resolved.auth.provider or "").lower()
        login = await token_account_login(provider, resolved.auth.token, resolved.server_url, host)
        # Empty login => treat the owner as an organization (see
        # token_account_login): the provider then adjudicates, instead of us
        # defaulting to the personal account.
        owner_arg = "" if login and owner.lower() == login.lower() else owner

        if owner_arg and provider == "gitlab":
            namespace_id = await gitlab_namespace_id(
                resolved.auth.token, owner_arg, resolved.server_url, host
            )
            if not namespace_id:
                raise GitRemoteAccessError(
                    "provision",
                    GitRemoteReason.REPO_CREATION_DENIED,
                    host,
                    f"no accessible GitLab namespace matches {owner_arg!r}",
                )
            owner_arg = namespace_id

        creation = await GitOperations.create_remote_repo(
            name=repo_name,
            token=resolved.auth.token,
            server_url=resolved.server_url,
            provider=provider,
            org=owner_arg,
            private=private,
        )
        if not creation.ok:
            raise GitRemoteAccessError("provision", creation.reason, host, creation.detail)

        logger.info(
            "workspace_remote_repository_created",
            host=host,
            owner=owner,
            repo=repo_name,
            under_organization=bool(owner_arg),
            private=private,
        )
        logger.info(
            "workspace_remote_provisioned",
            state=RemoteProvisionState.CREATED_EMPTY.value,
            host=host,
        )
        return RemoteProvision(
            state=RemoteProvisionState.CREATED_EMPTY,
            # The URL the user typed stays the workspace's remote: the provider's
            # own clone_url may differ in shape (trailing .git, host casing) and
            # swapping it in would make the stored value disagree with the input.
            normalized_url=normalized_url,
            created=True,
        )

    # ---- Remote binding / sync ----

    async def push_initial_code(
        self, local_path: str, git_repo_url: str, branch: str, user_id: str
    ) -> GitCommandResult:
        """Point the local copy at the remote and push the current branch.

        Verifies write access through the provider API BEFORE pushing so an
        under-scoped token yields "the token has no write access" instead of an
        opaque HTTP 403 from git.
        """
        from src.workspace.git_credentials import resolve_auth_for_url

        resolved = await resolve_auth_for_url(user_id, git_repo_url)
        if resolved.auth is None:
            from src.git.ops import GitRemoteReason

            return GitCommandResult(
                ok=False,
                reason=GitRemoteReason.CREDENTIAL_MISSING,
                detail=f"no credential configured for {resolved.host}",
            )

        check = await check_remote_write_access(git_repo_url, resolved.auth, resolved.server_url)
        if not check.ok:
            logger.error(
                "git_write_access_denied",
                host=resolved.host,
                reason=check.reason,
                user_id=user_id,
            )
            return GitCommandResult(ok=False, reason=check.reason, detail=check.detail)

        await asyncio.to_thread(GitOperations.add_remote, local_path, git_repo_url)
        return await push_async(local_path, branch, "origin", resolved.auth)

    async def bind_repository(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        worktree_path: str,
        git_repo_url: str,
        production_branch: str,
    ) -> str:
        """Bind a remote to the workspace: validate, push, fetch into the bare repo.

        Mirrors the legacy ``WorkspaceManager.bind_git_repo`` minus the database
        update (which stays in control-service). Returns the normalized URL; a
        push failure raises :class:`GitRemoteAccessError`.
        """
        from src.git.ops import extract_git_host

        normalized_url = await validate_git_repo_url_async(git_repo_url)

        push_result = await self.push_initial_code(
            local_path=worktree_path,
            git_repo_url=normalized_url,
            branch=production_branch,
            user_id=user_id,
        )
        if not push_result.ok:
            raise GitRemoteAccessError(
                "bind",
                push_result.reason,
                extract_git_host(normalized_url),
                push_result.detail,
            )

        # The bare repository must learn the remote too: every future worktree
        # is created from its refs.
        from src.environments.git_binder import GitBinder

        try:
            await GitBinder(self._base_path).ensure_repository(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                git_repo_url=normalized_url,
            )
        except Exception as e:  # noqa: BLE001 - the push already succeeded
            logger.warning(
                "workspace_repo_remote_bind_failed",
                workspace_id=workspace_id,
                error=str(e),
            )

        return normalized_url

    async def sync_to_remote(
        self,
        *,
        workspace_path: str,
        branch: str,
        message: str,
        user_id: str,
        git_repo_url: str,
    ) -> bool:
        """Commit and push one environment working copy to the remote.

        Rejected while an apply holds the environment: the commit would move
        HEAD underneath a running apply, and the applied data would then be
        attributed to code that was not the code it ran on.
        """
        from src.sqlmesh.env_locks import check_environment_lock
        from src.workspace.git_credentials import (
            resolve_auth_for_url,
            resolve_commit_identity,
        )

        await check_environment_lock(workspace_path, operation="commit")

        # The sync commit is USER content: author it with the user's identity
        # (resolved from their git credential), not the platform's.
        author = await resolve_commit_identity(user_id, git_repo_url)
        commit_hash = await asyncio.to_thread(
            GitOperations.commit_all, workspace_path, message, author
        )
        if not commit_hash:
            logger.info(
                "workspace_sync_no_changes",
                workspace_path=workspace_path,
                user_id=user_id,
            )
            return True

        resolved = await resolve_auth_for_url(user_id, git_repo_url)
        push_result = await push_async(workspace_path, branch, "origin", resolved.auth)
        if not push_result.ok:
            raise GitRemoteAccessError(
                "push", push_result.reason, resolved.host, push_result.detail
            )

        logger.info(
            "workspace_synced",
            workspace_path=workspace_path,
            user_id=user_id,
            branch=branch,
        )
        return True

    async def remote_branches(self, git_repo_url: str, user_id: str) -> t.List[str]:
        """List remote branches available for the workspace repository."""
        from src.workspace.git_credentials import resolve_auth_for_url

        resolved = await resolve_auth_for_url(user_id, git_repo_url)
        return await ls_remote_branches_async(git_repo_url, resolved.auth)

    # ---- Teardown ----

    async def release_cached_contexts(
        self, tenant_id: str, workspace_id: str, user_id: str = ""
    ) -> None:
        """Retire the sandbox executors and cached metadata of a workspace.

        Both are keyed by working-copy path, so the whole subtree is matched
        by prefix. Best-effort: failing to release must never block a teardown,
        and idle expiry is the fallback.
        """
        prefix = (
            ws_paths.user_root_path(self._base_path, tenant_id, workspace_id, user_id)
            if user_id
            else ws_paths.workspace_shared_root(self._base_path, tenant_id, workspace_id)
        )
        try:
            from src.executor.process import get_executor_manager
            from src.sqlmesh.metadata_context import get_metadata_context

            evicted = get_executor_manager().invalidate_under_path(prefix)
            evicted += get_metadata_context().invalidate_under_path(prefix)
        except Exception as exc:  # noqa: BLE001 - teardown must not fail on this
            logger.warning("workspace_context_release_failed", path=prefix, error=str(exc))
            return

        if evicted:
            logger.info(
                "workspace_contexts_released",
                workspace_id=workspace_id,
                path=prefix,
                count=evicted,
            )

    async def hard_purge_workspace(self, *, tenant_id: str, workspace_id: str) -> dict:
        """Erase every trace of a workspace this service holds. Never the remote.

        Ordering is dependency-driven, and each step is best-effort so one
        failure cannot strand the rest:

        1. cached contexts and sandbox executors — they hold engine connections
           and a project loaded from the directory about to vanish;
        2. the Dagster code locations of this workspace, before the working copy
           they mount disappears underneath them;
        3. the SQLMesh virtual environments and the workspace's state schema,
           which needs a working copy to reach the engine — so it must happen
           BEFORE the directories are removed;
        4. the rows of the three tables this service owns, keyed by
           ``workspace_path``;
        5. the directories themselves.

        The REMOTE GIT REPOSITORY IS NEVER TOUCHED. It may be a repository the
        user already owned and merely pointed the workspace at, and deleting a
        Git repository is not an action a platform may take on a user's behalf as
        a side effect of removing a workspace.

        Idempotent throughout (absent directory = no-op, absent rows = 0), so the
        caller may broadcast it to several replicas and retry freely.
        """
        shared_root = ws_paths.workspace_shared_root(self._base_path, tenant_id, workspace_id)

        deployments_stopped = await self._stop_workspace_deployments(shared_root)
        state_released = await self._release_sqlmesh_state(
            tenant_id=tenant_id, workspace_id=workspace_id
        )
        await self.release_cached_contexts(tenant_id, workspace_id)
        rows_deleted = await self._delete_owned_rows(shared_root)
        directory_removed = await self._remove_directory(shared_root, workspace_id)

        logger.info(
            "workspace_hard_purged",
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            deployments_stopped=deployments_stopped,
            state_released=state_released,
            rows_deleted=rows_deleted,
            directory_removed=directory_removed,
        )
        return {
            "purged": True,
            "deployments_stopped": deployments_stopped,
            "state_released": state_released,
            "rows_deleted": rows_deleted,
            "directory_removed": directory_removed,
        }

    async def _stop_workspace_deployments(self, shared_root: str) -> int:
        """Stop the code locations serving any working copy under ``shared_root``.

        Stopping them first matters: a running code location has the working copy
        mounted, and pulling the directory out from under it turns a clean
        teardown into a crash loop against a path that no longer exists.
        """
        try:
            from src.codeloc.deployments import get_deployment_manager

            manager = get_deployment_manager()
            targets = [
                info.spec.deployment_name
                for info in manager.list()
                if _is_under(info.spec.workspace_path, shared_root)
            ]
            stopped = 0
            for deployment_name in targets:
                if await manager.destroy(deployment_name):
                    stopped += 1
            return stopped
        except Exception as exc:  # noqa: BLE001 - teardown must not fail on this
            logger.warning("workspace_purge_deployments_failed", error=str(exc))
            return 0

    async def _release_sqlmesh_state(self, *, tenant_id: str, workspace_id: str) -> bool:
        """Drop the workspace's SQLMesh state schema and its virtual environments.

        Reached through a working copy, because that copy's ``config.yaml`` is the
        only place the gateway connection is described — which is also why this
        runs before the directories are removed. Dropping the whole schema (one
        per workspace by construction) releases the environments in one statement
        instead of walking them.

        Best-effort: an unreachable warehouse must not block the deletion. The
        schema then outlives the workspace, and recreating the same name later
        finds a stale-but-consistent state that its first plan rebuilds.
        """
        from src.sqlmesh.config_ops import workspace_state_schema

        worktree = self._first_worktree_of(tenant_id, workspace_id)
        if not worktree:
            return False

        schema = workspace_state_schema(workspace_id)
        try:
            from src.sqlmesh.config_access import engine_adapter_for

            adapter = await asyncio.to_thread(engine_adapter_for, worktree)
            try:
                await asyncio.to_thread(
                    functools.partial(
                        adapter.drop_schema,
                        schema,
                        ignore_if_not_exists=True,
                        cascade=True,
                    )
                )
            finally:
                await asyncio.to_thread(adapter.close)
        except Exception as exc:  # noqa: BLE001 - resource release is best-effort
            logger.warning(
                "workspace_state_schema_drop_failed",
                workspace_id=workspace_id,
                state_schema=schema,
                error=str(exc),
            )
            return False

        logger.info(
            "workspace_state_schema_dropped",
            workspace_id=workspace_id,
            state_schema=schema,
        )
        return True

    def _first_worktree_of(self, tenant_id: str, workspace_id: str) -> str:
        """Any member's working copy of any environment, or ``""``.

        Every copy of a workspace declares the same workspace-level SQLMesh
        state, so any one of them can reach it. Read straight from disk rather
        than through the Git binder: this runs during teardown, where the only
        question is which directories still exist.
        """
        shared_root = ws_paths.workspace_shared_root(self._base_path, tenant_id, workspace_id)
        if not os.path.isdir(shared_root):
            return ""
        for user_id in sorted(os.listdir(shared_root)):
            envs_dir = os.path.join(shared_root, user_id, ENVS_DIR)
            if not os.path.isdir(envs_dir):
                continue
            for entry in sorted(os.listdir(envs_dir)):
                candidate = os.path.join(envs_dir, entry)
                if os.path.isdir(candidate):
                    return candidate
        return ""

    async def _delete_owned_rows(self, shared_root: str) -> dict:
        """Delete this service's rows for every working copy under ``shared_root``.

        Scoped by a ``workspace_path`` prefix with a TRAILING SLASH: without it
        workspace ``ws-x`` would also match ``ws-x-archive``. Only the three
        tables this service OWNS are touched — the control plane's rows are its
        own to delete, and reaching into them from here would put two writers on
        one table.
        """
        from sqlalchemy import delete as sql_delete
        from sqlalchemy import inspect as sql_inspect

        from src.workspace.database import get_session
        from src.workspace.models import (
            FileDraftModel,
            KnowledgeDocumentModel,
            PlanApplyHistoryModel,
        )

        prefix = f"{shared_root.rstrip('/')}/"
        tables = {
            "knowledge_documents": KnowledgeDocumentModel.__table__,
            "file_drafts": FileDraftModel.__table__,
            "plan_apply_history": PlanApplyHistoryModel.__table__,
        }
        counts: dict = {name: 0 for name in tables}

        try:
            async with get_session() as session:
                # The probe and the deletes share ONE transaction: probing first
                # on its own would autobegin the session and make an explicit
                # begin() fail with "a transaction is already begun".
                conn = await session.connection()
                present = await conn.run_sync(
                    lambda sync_conn: set(sql_inspect(sync_conn).get_table_names())
                )
                for name, table in tables.items():
                    if table.name not in present:
                        continue
                    result = await session.execute(
                        sql_delete(table).where(table.c.workspace_path.like(f"{prefix}%"))
                    )
                    counts[name] = int(result.rowcount or 0)
        except Exception as exc:  # noqa: BLE001 - teardown must not fail on this
            logger.warning("workspace_purge_rows_failed", error=str(exc))
            return counts

        if any(counts.values()):
            logger.info("workspace_purge_rows_deleted", counts=counts)
        return counts

    async def _remove_directory(self, shared_root: str, workspace_id: str) -> bool:
        """Remove the workspace's whole shared root (all members, all copies)."""
        if not os.path.isdir(shared_root):
            return False
        try:
            await asyncio.to_thread(shutil.rmtree, shared_root)
        except OSError as exc:
            logger.warning(
                "workspace_local_copies_remove_failed",
                workspace_id=workspace_id,
                path=shared_root,
                error=str(exc),
            )
            return False
        logger.info(
            "workspace_local_copies_removed",
            workspace_id=workspace_id,
            path=shared_root,
        )
        return True

    async def purge_member_copy(self, *, tenant_id: str, workspace_id: str, user_id: str) -> bool:
        """Drop one member's working copy (access revocation housekeeping)."""
        member_path = ws_paths.user_root_path(self._base_path, tenant_id, workspace_id, user_id)
        if not os.path.isdir(member_path):
            return False

        # Drop the member's cached contexts first: they hold an engine
        # connection and a project loaded from the directory about to vanish.
        await self.release_cached_contexts(tenant_id, workspace_id, user_id=user_id)
        await asyncio.to_thread(shutil.rmtree, member_path, ignore_errors=True)
        logger.info(
            "workspace_member_copy_removed",
            workspace_id=workspace_id,
            member_user_id=user_id,
        )
        return True


def _is_under(path: str, root: str) -> bool:
    """Whether ``path`` lies inside ``root``.

    Compared on normalized paths with a trailing separator, so ``/a/ws-x`` does
    not match ``/a/ws-x-archive`` — the prefix collision that would stop another
    workspace's deployments during this one's deletion.
    """
    if not path or not root:
        return False
    normalized_root = os.path.normpath(root) + os.sep
    return os.path.normpath(path).startswith(normalized_root)


# Global singleton (stateless: paths are resolved lazily from settings).
_provisioner: t.Optional[WorkspaceProvisioner] = None


def get_workspace_provisioner() -> WorkspaceProvisioner:
    """Get or create the global :class:`WorkspaceProvisioner`."""
    global _provisioner
    if _provisioner is None:
        _provisioner = WorkspaceProvisioner()
    return _provisioner
