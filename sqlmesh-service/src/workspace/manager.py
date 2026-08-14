"""Workspace lifecycle management.

A workspace is a TENANT-level shared development unit:

- **Database** is the single source of truth for its existence and metadata.
- **Git** is the only durable carrier of workspace code.
- **Disk** holds a disposable per-user working copy, derived at runtime from
  ``{base}/{tenant_id}/{workspace_id}/{user_id}`` (see :mod:`src.workspace.paths`).

Authorization is NOT performed here. The gateway owns the role bindings and is
the platform's single authorization center; by the time a request reaches this
module the caller's permissions have already been checked and are carried in
:class:`src.api.deps.CallerContext`. This module therefore only enforces tenant
scoping (a workspace of another tenant is reported as missing) and the domain
invariants that do not depend on roles.
"""

import asyncio
import os
import re
import shutil
import typing as t
from dataclasses import dataclass
from datetime import datetime

from src.config import settings
from src.utils.errors import (
    ConflictError,
    GitRemoteAccessError,
    GitRepoNotBoundError,
    InvalidRequestError,
    WorkspaceNotFoundError,
)
from src.utils.logging import get_logger
from src.workspace import paths as ws_paths
from src.workspace.models import WorkspaceModel

if t.TYPE_CHECKING:  # pragma: no cover - typing only
    from src.workspace.git_credentials import ResolvedGitAuth
    from src.workspace.git_ops import GitCommandResult

logger = get_logger(__name__)

# Conservative Git branch name: rejects option-looking values (leading "-")
# and anything outside a safe ref character set.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")

# Reason codes returned to the frontend when a workspace cannot be opened.
BLOCK_REASON_NO_GIT_REPO = "no_git_repo"


@dataclass(frozen=True)
class WorkspaceView:
    """Read model handed to the API layer.

    ``openable`` / ``open_blocked_reason`` are computed server-side on purpose:
    the frontend must not re-derive access rules from raw fields.

    ``role`` is echoed from the caller context for convenience; the gateway
    overwrites it (together with the effective permissions) in the response it
    returns to the client, so it stays the only authority on roles.
    """

    workspace_id: str
    tenant_id: str
    owner_user_id: str
    name: str
    dialect: str
    git_repo_url: str
    git_branch: str
    status: str
    role: str
    is_owner: bool
    openable: bool
    open_blocked_reason: str
    created_at: datetime
    last_active_at: datetime


@dataclass(frozen=True)
class OpenedWorkspace:
    """Result of materializing a workspace working copy for one user."""

    workspace_id: str
    workspace_path: str
    branch: str


class WorkspaceManager:
    """Manages workspace lifecycle across DB, Git and the local filesystem."""

    def __init__(self) -> None:
        # Expand ~ to the actual home directory path.
        self._base_path = os.path.expanduser(settings.workspace_base_path)
        os.makedirs(self._base_path, exist_ok=True)
        logger.info("workspace_manager_initialized", base_path=self._base_path)

    @property
    def base_path(self) -> str:
        """Root directory holding every tenant's working copies."""
        return self._base_path

    # ---- Public API: lifecycle ----

    async def create(
        self,
        tenant_id: str,
        owner_user_id: str,
        name: str,
        dialect: str = "duckdb",
        git_repo_url: str = "",
    ) -> WorkspaceView:
        """Create a workspace owned by ``owner_user_id`` inside ``tenant_id``.

        Order of operations is chosen so a failure never leaves a half-created
        workspace: the local scaffold is built first and rolled back if the
        (authoritative) database insert fails.
        """
        from src.workspace.git_ops import GitOperations, validate_git_repo_url_async

        ws_paths.validate_segment(tenant_id, field="tenant_id")
        ws_paths.validate_segment(owner_user_id, field="user_id")
        slug = ws_paths.slugify(name)
        workspace_id = ws_paths.build_workspace_id(tenant_id, slug)

        repo = self._repository()
        if await repo.get_by_id(workspace_id) is not None:
            raise ConflictError(f"Workspace already exists: {workspace_id}")

        if git_repo_url:
            git_repo_url = await validate_git_repo_url_async(git_repo_url)

        local_path = self.user_path(tenant_id, workspace_id, owner_user_id)
        os.makedirs(local_path, exist_ok=True)
        await asyncio.to_thread(self._scaffold_project, local_path, name, dialect)
        await asyncio.to_thread(GitOperations.init, local_path)

        model = WorkspaceModel(
            id=workspace_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            name=name,
            dialect=dialect,
            git_repo_url=git_repo_url or None,
            git_branch="main",
            status="active",
        )
        try:
            await repo.create(model)
        except Exception:
            # The DB is authoritative; drop the orphan scaffold so a retry is clean.
            shutil.rmtree(local_path, ignore_errors=True)
            logger.error(
                "workspace_create_rolled_back",
                workspace_id=workspace_id,
                tenant_id=tenant_id,
            )
            raise

        if git_repo_url:
            push_result = await self._push_initial_code(
                local_path=local_path,
                git_repo_url=git_repo_url,
                branch="main",
                user_id=owner_user_id,
            )
            if not push_result.ok:
                # Non-fatal: the workspace exists and the owner keeps a local
                # copy, but members cannot clone until the push succeeds.
                logger.error(
                    "workspace_initial_push_failed",
                    workspace_id=workspace_id,
                    tenant_id=tenant_id,
                    reason=push_result.reason,
                )

        logger.info(
            "workspace_created",
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            dialect=dialect,
            has_git_remote=bool(git_repo_url),
        )
        return self._build_view(model, role="", requester_user_id=owner_user_id,
                                local_path=local_path)

    async def list_for_ids(
        self, tenant_id: str, user_id: str, workspace_ids: t.Sequence[str]
    ) -> t.List[WorkspaceView]:
        """List the active workspaces named by ``workspace_ids``.

        The gateway resolved the identifiers from the caller's role bindings, so
        this is a metadata lookup restricted to the caller's tenant.
        """
        ws_paths.validate_segment(tenant_id, field="tenant_id")
        ws_paths.validate_segment(user_id, field="user_id")

        repo = self._repository()
        models = await repo.list_by_ids(tenant_id, workspace_ids)

        views: t.List[WorkspaceView] = []
        for model in models:
            local_path = self.user_path(tenant_id, model.id, user_id)
            views.append(
                self._build_view(
                    model, role="", requester_user_id=user_id, local_path=local_path
                )
            )

        logger.info(
            "workspaces_listed",
            tenant_id=tenant_id,
            user_id=user_id,
            count=len(views),
        )
        return views

    async def open(
        self,
        workspace_id: str,
        tenant_id: str,
        user_id: str,
        branch: str = "",
    ) -> OpenedWorkspace:
        """Materialize the caller's working copy and return its path.

        Clones on first open, otherwise fetches and checks out the requested
        branch. Each member gets an isolated directory so concurrent work on the
        same workspace never collides.
        """
        from src.workspace.git_ops import clone_async, fetch_and_checkout_async

        model = await self._require_workspace(workspace_id, tenant_id)
        local_path = self.user_path(tenant_id, workspace_id, user_id)
        git_repo_url = model.git_repo_url or ""

        if not git_repo_url:
            # Bootstrap window: the owner still holds the only copy of the code
            # and must be able to enter the workspace to bind Git and push.
            if model.owner_user_id == user_id and os.path.isdir(local_path):
                await self._touch_last_active(workspace_id)
                logger.info(
                    "workspace_opened_without_git",
                    workspace_id=workspace_id,
                    user_id=user_id,
                )
                return OpenedWorkspace(workspace_id, local_path, model.git_branch)
            logger.warning(
                "workspace_open_blocked_no_git_repo",
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            raise GitRepoNotBoundError(workspace_id)

        target_branch = self._validate_branch(branch or model.git_branch)
        resolved = await self._resolve_git_auth(user_id, git_repo_url)

        if os.path.isdir(os.path.join(local_path, ".git")):
            result = await fetch_and_checkout_async(
                local_path, target_branch, resolved.auth
            )
            if not result.ok:
                raise GitRemoteAccessError(
                    "fetch", result.reason, resolved.host, result.detail
                )
        else:
            # A stale non-git directory would make clone fail; clear it first.
            if os.path.isdir(local_path):
                shutil.rmtree(local_path, ignore_errors=True)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            result = await clone_async(
                git_repo_url, local_path, target_branch, resolved.auth
            )
            if not result.ok:
                raise GitRemoteAccessError(
                    "clone", result.reason, resolved.host, result.detail
                )

        await self._touch_last_active(workspace_id)
        logger.info(
            "workspace_opened",
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            user_id=user_id,
            branch=target_branch,
        )
        return OpenedWorkspace(workspace_id, local_path, target_branch)

    async def remote_branches(
        self, workspace_id: str, tenant_id: str, user_id: str
    ) -> t.List[str]:
        """List remote branches available for the workspace repository."""
        from src.workspace.git_ops import ls_remote_branches_async

        model = await self._require_workspace(workspace_id, tenant_id)
        if not model.git_repo_url:
            return []

        resolved = await self._resolve_git_auth(user_id, model.git_repo_url)
        branches = await ls_remote_branches_async(model.git_repo_url, resolved.auth)
        logger.info(
            "workspace_remote_branches_listed",
            workspace_id=workspace_id,
            user_id=user_id,
            count=len(branches),
        )
        return branches

    async def bind_git_repo(
        self,
        workspace_id: str,
        tenant_id: str,
        user_id: str,
        git_repo_url: str,
    ) -> str:
        """Bind a remote repository to the workspace and push the current code.

        Binding decides where every member pulls the code from, so it is gated
        by the ``workspace:git:bind`` permission at the gateway.
        """
        from src.workspace.git_ops import extract_git_host, validate_git_repo_url_async

        model = await self._require_workspace(workspace_id, tenant_id)
        normalized_url = await validate_git_repo_url_async(git_repo_url)

        local_path = self.user_path(tenant_id, workspace_id, user_id)
        if not os.path.isdir(local_path):
            raise ConflictError(
                "Local working copy is missing; open the workspace before binding Git"
            )

        push_result = await self._push_initial_code(
            local_path=local_path,
            git_repo_url=normalized_url,
            branch=model.git_branch,
            user_id=user_id,
        )
        if not push_result.ok:
            raise GitRemoteAccessError(
                "bind",
                push_result.reason,
                extract_git_host(normalized_url),
                push_result.detail,
            )

        repo = self._repository()
        await repo.update_git_repo(workspace_id, normalized_url)
        logger.info(
            "workspace_git_bound",
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return normalized_url

    async def sync_to_remote(
        self,
        workspace_id: str,
        tenant_id: str,
        user_id: str,
        message: str = "Auto-save",
    ) -> bool:
        """Commit and push the caller's working copy to the remote repository.

        Gated by the ``workspace:git:push`` permission at the gateway.
        """
        from src.workspace.git_ops import GitOperations, push_async

        model = await self._require_workspace(workspace_id, tenant_id)
        if not model.git_repo_url:
            raise GitRepoNotBoundError(workspace_id)

        local_path = self.user_path(tenant_id, workspace_id, user_id)
        if not os.path.isdir(local_path):
            raise ConflictError("Local working copy is missing; open the workspace first")

        commit_hash = await asyncio.to_thread(
            GitOperations.commit_all, local_path, message
        )
        if not commit_hash:
            logger.info(
                "workspace_sync_no_changes", workspace_id=workspace_id, user_id=user_id
            )
            return True

        resolved = await self._resolve_git_auth(user_id, model.git_repo_url)
        push_result = await push_async(
            local_path, model.git_branch, "origin", resolved.auth
        )
        if not push_result.ok:
            raise GitRemoteAccessError(
                "push", push_result.reason, resolved.host, push_result.detail
            )

        logger.info(
            "workspace_synced",
            workspace_id=workspace_id,
            user_id=user_id,
            branch=model.git_branch,
        )
        return True

    async def delete(
        self,
        workspace_id: str,
        tenant_id: str,
        user_id: str,
        delete_remote: bool = False,
    ) -> bool:
        """Soft-delete a workspace and drop every local working copy.

        Gated by the ``workspace:delete`` permission at the gateway, which also
        purges the role bindings once this call succeeds. All members' copies
        under the shared root are removed so no cached SQLMesh context keeps
        pointing at a deleted workspace.
        """
        model = await self._require_workspace(workspace_id, tenant_id)

        shared_root = ws_paths.workspace_shared_root(
            self._base_path, tenant_id, workspace_id
        )
        if os.path.isdir(shared_root):
            try:
                await asyncio.to_thread(shutil.rmtree, shared_root)
                logger.info(
                    "workspace_local_copies_removed",
                    workspace_id=workspace_id,
                    path=shared_root,
                )
            except OSError as e:
                logger.warning(
                    "workspace_local_copies_remove_failed",
                    workspace_id=workspace_id,
                    path=shared_root,
                    error=str(e),
                )

        repo = self._repository()
        await repo.delete(workspace_id)

        if delete_remote and model.git_repo_url:
            from src.workspace.git_ops import GitOperations

            token = await self._get_user_token(user_id)
            await GitOperations.delete_remote_repo(
                name=model.name, token=token, owner=model.owner_user_id
            )

        logger.info(
            "workspace_deleted",
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return True

    # ---- Public API: working-copy housekeeping ----

    async def cleanup_member_copy(
        self, workspace_id: str, tenant_id: str, target_user_id: str
    ) -> bool:
        """Drop a revoked member's working copy.

        Access revocation itself happens in the gateway; this only removes the
        disposable checkout so the code stops being readable on disk. The owner's
        copy is never touched.
        """
        model = await self._require_workspace(workspace_id, tenant_id)
        ws_paths.validate_segment(target_user_id, field="target_user_id")

        if target_user_id == model.owner_user_id:
            raise ConflictError("The workspace owner's working copy cannot be removed")

        member_path = self.user_path(tenant_id, workspace_id, target_user_id)
        if not os.path.isdir(member_path):
            return False

        await asyncio.to_thread(shutil.rmtree, member_path, ignore_errors=True)
        logger.info(
            "workspace_member_copy_removed",
            workspace_id=workspace_id,
            member_user_id=target_user_id,
        )
        return True

    # ---- Public API: path derivation ----

    def user_path(self, tenant_id: str, workspace_id: str, user_id: str) -> str:
        """Derive the caller's working-copy path (never read from the DB)."""
        return ws_paths.user_workspace_path(
            self._base_path, tenant_id, workspace_id, user_id
        )

    # ---- Private: lookup helpers ----

    @staticmethod
    def _repository() -> t.Any:
        """Instantiate the workspace repository (imported lazily)."""
        from src.workspace.repository import WorkspaceRepository

        return WorkspaceRepository()

    async def _require_workspace(
        self, workspace_id: str, tenant_id: str
    ) -> WorkspaceModel:
        """Resolve a workspace inside a tenant, or raise 404.

        A workspace of another tenant yields the same 404 as a non-existent one,
        so identifiers cannot be enumerated across tenants. Whether the caller
        may use it was already decided by the gateway.
        """
        ws_paths.validate_segment(tenant_id, field="tenant_id")
        ws_paths.validate_segment(workspace_id, field="workspace_id")

        repo = self._repository()
        model = await repo.get_in_tenant(workspace_id, tenant_id)
        if model is None:
            logger.warning(
                "workspace_not_found",
                workspace_id=workspace_id,
                tenant_id=tenant_id,
            )
            raise WorkspaceNotFoundError(workspace_id)
        return model

    @staticmethod
    def _validate_branch(branch: str) -> str:
        """Validate a branch name before it is passed to the Git CLI."""
        if not _BRANCH_RE.match(branch or ""):
            raise InvalidRequestError(f"invalid Git branch name: {branch}")
        return branch

    # ---- Private: view assembly ----

    def _build_view(
        self,
        model: WorkspaceModel,
        role: str,
        requester_user_id: str,
        local_path: str,
    ) -> WorkspaceView:
        """Assemble the API read model, computing openability server-side."""
        is_owner = model.owner_user_id == requester_user_id
        git_repo_url = model.git_repo_url or ""
        openable = bool(git_repo_url)
        blocked_reason = ""

        if not openable:
            # Owner-only bootstrap: the freshly scaffolded copy is still local.
            if is_owner and os.path.isdir(local_path):
                openable = True
            else:
                blocked_reason = BLOCK_REASON_NO_GIT_REPO

        return WorkspaceView(
            workspace_id=model.id,
            tenant_id=model.tenant_id,
            owner_user_id=model.owner_user_id,
            name=model.name,
            dialect=model.dialect,
            git_repo_url=git_repo_url,
            git_branch=model.git_branch,
            status=model.status,
            role=role,
            is_owner=is_owner,
            openable=openable,
            open_blocked_reason=blocked_reason,
            created_at=model.created_at,
            last_active_at=model.last_active_at,
        )

    # ---- Private: DB / Git helpers ----

    async def _touch_last_active(self, workspace_id: str) -> None:
        """Best-effort last-active bookkeeping (never fails the request)."""
        try:
            repo = self._repository()
            await repo.update_last_active(workspace_id)
        except Exception as e:
            logger.warning(
                "workspace_touch_last_active_failed",
                workspace_id=workspace_id,
                error=str(e),
            )

    async def _resolve_git_auth(self, user_id: str, repo_url: str) -> "ResolvedGitAuth":
        """Resolve the credential matching ``repo_url``'s host.

        Returns a resolution whose ``auth`` may be None (no credential for that
        host); read-only operations can still proceed anonymously, while write
        operations must turn that into a ``credential_missing`` error.
        """
        from src.workspace.git_credentials import resolve_auth_for_url

        return await resolve_auth_for_url(user_id, repo_url)

    async def _push_initial_code(
        self, local_path: str, git_repo_url: str, branch: str, user_id: str
    ) -> "GitCommandResult":
        """Point the local copy at the remote and push the current branch.

        Verifies write access through the provider API BEFORE pushing so an
        under-scoped token yields "the token has no write access" instead of an
        opaque HTTP 403 from git.
        """
        from src.workspace.git_ops import (
            GitCommandResult,
            GitOperations,
            GitRemoteReason,
            check_remote_write_access,
            push_async,
        )

        resolved = await self._resolve_git_auth(user_id, git_repo_url)
        if resolved.auth is None:
            return GitCommandResult(
                ok=False,
                reason=GitRemoteReason.CREDENTIAL_MISSING,
                detail=f"no credential configured for {resolved.host}",
            )

        check = await check_remote_write_access(
            git_repo_url, resolved.auth, resolved.server_url
        )
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

    # ---- Private: Project scaffolding ----

    def _scaffold_project(self, path: str, name: str, dialect: str = "duckdb") -> None:
        """Scaffold the dual-directory workspace layout.

        A workspace root now contains two sibling projects:
        - ``sql-warehouse/`` : the SQLMesh project (created via ``sqlmesh init``)
        - ``asset-dag/``     : the Dagster Data+AI pipeline project

        Git is initialized at the workspace ROOT so both projects are versioned
        together. The ``.gitignore`` is written HERE rather than after
        ``git init`` so the very first commit already excludes runtime output
        (caches, logs, the error archive) instead of tracking it and needing a
        follow-up removal.
        """
        from src.workspace.layout import (
            ASSET_DAG_DIR,
            SQL_WAREHOUSE_DIR,
            ensure_gitignore,
        )

        warehouse_path = os.path.join(path, SQL_WAREHOUSE_DIR)
        asset_dag_path = os.path.join(path, ASSET_DAG_DIR)

        ensure_gitignore(path)
        self._scaffold_sqlmesh(warehouse_path, name, dialect)
        self._scaffold_asset_dag(asset_dag_path, name)

    def _scaffold_sqlmesh(self, path: str, name: str, dialect: str = "duckdb") -> None:
        """Create the SQLMesh project structure by calling ``sqlmesh init``."""
        import subprocess

        os.makedirs(path, exist_ok=True)
        models_dir = os.path.join(path, "models")
        if os.path.exists(models_dir):
            return  # Already scaffolded

        try:
            result = subprocess.run(
                ["sqlmesh", "init", dialect],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                self._inject_project_name(path, name)
                logger.info("workspace_scaffolded_via_cli", path=path, dialect=dialect)
            else:
                logger.error(
                    "sqlmesh_init_failed",
                    path=path,
                    dialect=dialect,
                    stderr=result.stderr,
                )
                self._scaffold_fallback(path, name, dialect)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("sqlmesh_cli_unavailable", error=str(e))
            self._scaffold_fallback(path, name, dialect)

        # Give the new workspace its own SQLMesh state schema so it never
        # collides with sibling workspaces on a shared engine/database.
        try:
            from src.workspace.config_ops import ensure_state_isolation

            ensure_state_isolation(path)
        except Exception as e:  # scaffolding must not fail on this
            logger.warning("state_isolation_scaffold_skipped", path=path, error=str(e))

    def _scaffold_asset_dag(self, path: str, name: str) -> None:
        """Create the canonical Dagster (asset-dag) project skeleton.

        Writes definitions.py + pyproject.toml + the assets/ package so the
        workspace is immediately loadable as a classic (non-dg) Code Location.
        The dagster-service re-renders any missing file from its own templates
        when the code location is first ensured.
        """
        from src.workspace.asset_dag_skeleton import scaffold_asset_dag

        scaffold_asset_dag(path, name)

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

        After `sqlmesh init` creates the scaffold, the generated
        config.yaml may not contain a ``project`` key.  This method
        reads the file, inserts the ``project: <name>`` entry right
        after the first comment line (or at the top of the file), and
        writes it back.
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


# Global singleton
_manager: t.Optional[WorkspaceManager] = None


def get_workspace_manager() -> WorkspaceManager:
    """Get or create the global WorkspaceManager instance."""
    global _manager
    if _manager is None:
        _manager = WorkspaceManager()
    return _manager
