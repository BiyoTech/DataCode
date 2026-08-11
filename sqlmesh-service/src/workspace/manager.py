"""Workspace lifecycle management.

Orchestrates workspace operations across three layers:
- Database (metadata persistence)
- Git (remote code persistence)
- Disk (local working copy)

Handles creation, activation, sync, and recovery of user workspaces.
Each Workspace is a SQLMesh project directory on disk, optionally backed by Git.
"""

import os
import typing as t
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class WorkspaceMetadata:
    """Workspace state and metadata."""
    workspace_id: str
    user_id: str
    name: str
    local_path: str
    dialect: str = "duckdb"
    git_repo_url: str = ""
    git_branch: str = "main"
    status: str = "active"  # active / archived / deleted
    last_active_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class WorkspaceManager:
    """Manages workspace lifecycle across DB, Git, and local filesystem.

    Storage layout: {base_path}/{user_id}/{workspace_name}/

    Source of truth:
    - Disk is authoritative for *existence*: any directory with a config.yaml
      is a workspace. Listings reconcile disk with the DB so a directory is
      never hidden just because the DB lacks a record.
    - DB is a metadata/recovery layer: Git binding, status, timestamps, and
      (for ephemeral-disk deployments) enough info to re-clone a workspace
      whose local copy is absent. It degrades gracefully when unavailable.
    """

    def __init__(self):
        # Expand ~ to actual home directory path
        self._base_path = os.path.expanduser(settings.workspace_base_path)
        self._workspaces: t.Dict[str, WorkspaceMetadata] = {}
        os.makedirs(self._base_path, exist_ok=True)
        # Disk scan as fallback (works without DB for local dev)
        self._discover_existing_workspaces()
        logger.info(
            "workspace_manager_initialized",
            base_path=self._base_path,
            discovered=len(self._workspaces),
        )

    # ---- Public API ----

    async def create(
        self,
        user_id: str,
        name: str,
        dialect: str = "duckdb",
        git_repo_url: str = "",
    ) -> WorkspaceMetadata:
        """Create a new workspace.

        Steps:
        1. Create local directory + SQLMesh scaffold
        2. Init local Git repository
        3. Persist metadata to DB
        4. If git_repo_url provided, set up remote

        Args:
            user_id: Owner user ID.
            name: Workspace display name.
            dialect: SQL engine type.
            git_repo_url: Optional remote Git repo URL to bind.

        Returns:
            WorkspaceMetadata for the created workspace.
        """
        dir_name = name.replace(" ", "_").lower()
        local_path = os.path.join(self._base_path, user_id, dir_name)
        workspace_id = f"ws-{user_id}-{dir_name}"

        # Return existing if already created
        if workspace_id in self._workspaces:
            ws = self._workspaces[workspace_id]
            ws.last_active_at = datetime.now(timezone.utc)
            ws.status = "active"
            return ws

        # Step 1: Create directory with SQLMesh project scaffold
        os.makedirs(local_path, exist_ok=True)
        self._scaffold_project(local_path, name, dialect)

        # Step 2: Init local Git
        from src.workspace.git_ops import GitOperations
        GitOperations.init(local_path)

        # Step 3: Build metadata
        ws = WorkspaceMetadata(
            workspace_id=workspace_id,
            user_id=user_id,
            name=name,
            local_path=local_path,
            dialect=dialect,
            git_repo_url=git_repo_url,
        )
        self._workspaces[workspace_id] = ws

        # Step 4: Persist to DB (best-effort, non-blocking for local dev)
        await self._persist_to_db(ws)

        # Step 5: If remote Git URL provided, set up remote
        if git_repo_url:
            await self._setup_remote_git(ws)

        logger.info(
            "workspace_created",
            workspace_id=workspace_id,
            local_path=local_path,
            dialect=dialect,
            has_git_remote=bool(git_repo_url),
        )
        return ws

    def create_sync(
        self,
        user_id: str,
        name: str,
        dialect: str = "duckdb",
    ) -> WorkspaceMetadata:
        """Synchronous create (no DB/Git remote).

        Used by sync endpoints. For new code, prefer async create().
        """
        dir_name = name.replace(" ", "_").lower()
        local_path = os.path.join(self._base_path, user_id, dir_name)
        workspace_id = f"ws-{user_id}-{dir_name}"

        if workspace_id in self._workspaces:
            ws = self._workspaces[workspace_id]
            ws.last_active_at = datetime.now(timezone.utc)
            ws.status = "active"
            return ws

        os.makedirs(local_path, exist_ok=True)
        self._scaffold_project(local_path, name, dialect)

        ws = WorkspaceMetadata(
            workspace_id=workspace_id,
            user_id=user_id,
            name=name,
            local_path=local_path,
            dialect=dialect,
        )
        self._workspaces[workspace_id] = ws

        logger.info("workspace_created", workspace_id=workspace_id, local_path=local_path, dialect=dialect)
        return ws

    def get(self, workspace_id: str) -> t.Optional[WorkspaceMetadata]:
        """Get workspace metadata by ID."""
        return self._workspaces.get(workspace_id)

    def get_path(self, workspace_id: str) -> t.Optional[str]:
        """Get the filesystem path for a workspace."""
        ws = self._workspaces.get(workspace_id)
        return ws.local_path if ws else None

    async def list_workspaces(self, user_id: str) -> t.List[WorkspaceMetadata]:
        """List workspaces for a user, reconciling disk with the DB.

        Disk is the source of truth for existence: every workspace directory on
        disk is included, so a directory is never hidden just because it is
        missing from the DB. The DB supplements this with metadata (Git binding,
        status, timestamps) and enables recovery of workspaces whose local copy
        is absent — e.g. a fresh Pod with ephemeral disk — which are re-cloned
        lazily on access via ``ensure_local``.

        Reconciliation per call:
        - disk ∩ db  → DB metadata, but trust disk for local_path/dialect
        - disk only  → include and back-fill into the DB (make it recoverable)
        - db only    → include (recoverable; local copy re-cloned on access)
        Soft-deleted DB records are excluded (``list_by_user`` filters status).
        """
        disk = self._scan_disk_workspaces(user_id)

        # Refresh the in-memory cache from disk (source of truth for presence).
        for workspace_id, ws in disk.items():
            cached = self._workspaces.get(workspace_id)
            if cached is None:
                self._workspaces[workspace_id] = ws
            else:
                cached.local_path = ws.local_path
                if cached.dialect in ("", "duckdb") and ws.dialect:
                    cached.dialect = ws.dialect

        db_workspaces = await self._list_from_db(user_id)
        if db_workspaces is None:
            # DB unavailable → disk is the only truth.
            return list(disk.values())

        merged: t.Dict[str, WorkspaceMetadata] = {
            ws.workspace_id: ws for ws in db_workspaces
        }
        for workspace_id, ws in disk.items():
            existing = merged.get(workspace_id)
            if existing is not None:
                # Trust disk for the local working copy details.
                existing.local_path = ws.local_path
                if existing.dialect in ("", "duckdb") and ws.dialect:
                    existing.dialect = ws.dialect
            else:
                # On disk but not in DB → surface it and back-fill for recovery.
                merged[workspace_id] = ws
                await self._persist_to_db(ws)
                logger.info("workspace_backfilled_to_db", workspace_id=workspace_id)

        return list(merged.values())

    def list_workspaces_sync(self, user_id: str) -> t.List[WorkspaceMetadata]:
        """Synchronous list (backward-compatible, in-memory only)."""
        return [ws for ws in self._workspaces.values() if ws.user_id == user_id]

    async def ensure_local(self, workspace_id: str) -> t.Optional[str]:
        """Ensure workspace exists on local disk. Clone from Git if needed.

        Used for Pod recovery: DB has metadata → clone from remote Git.
        Returns local_path or None if not recoverable.
        """
        ws = self._workspaces.get(workspace_id)
        if ws and os.path.exists(ws.local_path):
            return ws.local_path

        # Try to recover from DB
        ws_db = await self._get_from_db(workspace_id)
        if not ws_db:
            return None

        if ws_db.git_repo_url and not os.path.exists(ws_db.local_path):
            # Clone from remote Git
            token = await self._get_user_token(ws_db.user_id)
            from src.workspace.git_ops import GitOperations
            success = GitOperations.clone(
                repo_url=ws_db.git_repo_url,
                local_path=ws_db.local_path,
                branch=ws_db.git_branch,
                token=token,
            )
            if not success:
                logger.error("workspace_recovery_failed", workspace_id=workspace_id)
                return None

        self._workspaces[workspace_id] = ws_db
        logger.info("workspace_recovered", workspace_id=workspace_id)
        return ws_db.local_path

    async def sync_to_remote(self, workspace_id: str, message: str = "Auto-save") -> bool:
        """Commit and push local changes to remote Git.

        Args:
            workspace_id: Workspace ID.
            message: Commit message.

        Returns:
            True if sync successful.
        """
        ws = self._workspaces.get(workspace_id)
        if not ws or not ws.git_repo_url:
            return False

        from src.workspace.git_ops import GitOperations

        # Commit all local changes
        commit_hash = GitOperations.commit_all(ws.local_path, message)
        if not commit_hash:
            return True  # No changes to sync

        # Push to remote
        return GitOperations.push(ws.local_path, branch=ws.git_branch)

    async def bind_git_repo(self, workspace_id: str, git_repo_url: str, user_id: str) -> bool:
        """Bind an existing Git repository to a workspace.

        Sets up remote and pushes existing code to the repo.
        """
        ws = self._workspaces.get(workspace_id)
        if not ws:
            return False

        token = await self._get_user_token(user_id)
        from src.workspace.git_ops import GitOperations

        # Add remote
        GitOperations.add_remote(ws.local_path, git_repo_url, token=token)

        # Push existing code
        GitOperations.push(ws.local_path, branch=ws.git_branch)

        # Update metadata
        ws.git_repo_url = git_repo_url
        await self._update_git_repo_in_db(workspace_id, git_repo_url)

        logger.info("workspace_git_bound", workspace_id=workspace_id, repo_url=git_repo_url)
        return True

    async def delete(self, workspace_id: str, delete_remote: bool = False) -> bool:
        """Delete a workspace.

        Removes the local directory (so a disk scan won't resurrect it), marks
        the record deleted in DB, and optionally removes the remote Git repo.
        Because disk is the source of truth for existence, an explicit delete
        MUST clear the on-disk directory — otherwise the next ``list`` would
        rediscover and re-create it.
        """
        ws = self._workspaces.pop(workspace_id, None)
        if ws is None:
            # Not cached (e.g. cross-Pod): resolve metadata from the DB so we
            # can still clean up the local directory and soft-delete the record.
            ws = await self._get_from_db(workspace_id)
        if ws is None:
            return False

        # Remove the local working copy so it is not rediscovered on next scan.
        if ws.local_path and os.path.isdir(ws.local_path):
            import shutil

            try:
                shutil.rmtree(ws.local_path)
                logger.info("workspace_dir_removed", workspace_id=workspace_id, path=ws.local_path)
            except OSError as e:
                logger.warning(
                    "workspace_dir_remove_failed",
                    workspace_id=workspace_id,
                    path=ws.local_path,
                    error=str(e),
                )

        # Mark deleted in DB
        await self._delete_in_db(workspace_id)

        # Optionally delete remote Git repo
        if delete_remote and ws.git_repo_url:
            token = await self._get_user_token(ws.user_id)
            from src.workspace.git_ops import GitOperations
            await GitOperations.delete_remote_repo(
                name=ws.name,
                token=token,
                owner=ws.user_id,
            )

        logger.info("workspace_deleted", workspace_id=workspace_id)
        return True

    def activate(self, workspace_id: str) -> bool:
        """Mark workspace as active (restore from hibernation)."""
        ws = self._workspaces.get(workspace_id)
        if not ws:
            return False
        ws.status = "active"
        ws.last_active_at = datetime.now(timezone.utc)
        return True

    # ---- Private: DB operations (best-effort) ----

    async def _persist_to_db(self, ws: WorkspaceMetadata) -> None:
        """Persist workspace metadata to DB. Gracefully skip if DB unavailable."""
        try:
            from src.workspace.repository import WorkspaceRepository
            from src.workspace.models import WorkspaceModel

            repo = WorkspaceRepository()
            model = WorkspaceModel(
                id=ws.workspace_id,
                user_id=ws.user_id,
                name=ws.name,
                dialect=ws.dialect,
                git_repo_url=ws.git_repo_url or None,
                git_branch=ws.git_branch,
                status=ws.status,
                local_path=ws.local_path,
            )
            await repo.create(model)
        except Exception as e:
            logger.warning("db_persist_skipped", error=str(e))

    async def _list_from_db(self, user_id: str) -> t.Optional[t.List[WorkspaceMetadata]]:
        """List workspaces from DB with timeout. Returns None if DB unavailable."""
        import asyncio

        try:
            async with asyncio.timeout(8):  # 8s timeout for DB query
                from src.workspace.repository import WorkspaceRepository

                repo = WorkspaceRepository()
                models = await repo.list_by_user(user_id)
                return [
                    WorkspaceMetadata(
                        workspace_id=m.id,
                        user_id=m.user_id,
                        name=m.name,
                        local_path=m.local_path,
                        dialect=m.dialect,
                        git_repo_url=m.git_repo_url or "",
                        git_branch=m.git_branch,
                        status=m.status,
                        last_active_at=m.last_active_at,
                        created_at=m.created_at,
                    )
                    for m in models
                ]
        except asyncio.TimeoutError:
            logger.warning("db_list_timeout", user_id=user_id)
            return None
        except Exception as e:
            logger.warning("db_list_fallback", error=str(e))
            return None

    async def _get_from_db(self, workspace_id: str) -> t.Optional[WorkspaceMetadata]:
        """Get workspace from DB by ID."""
        try:
            from src.workspace.repository import WorkspaceRepository

            repo = WorkspaceRepository()
            m = await repo.get_by_id(workspace_id)
            if not m:
                return None
            return WorkspaceMetadata(
                workspace_id=m.id,
                user_id=m.user_id,
                name=m.name,
                local_path=m.local_path,
                dialect=m.dialect,
                git_repo_url=m.git_repo_url or "",
                git_branch=m.git_branch,
                status=m.status,
                last_active_at=m.last_active_at,
                created_at=m.created_at,
            )
        except Exception as e:
            logger.warning("db_get_failed", error=str(e))
            return None

    async def _update_git_repo_in_db(self, workspace_id: str, git_repo_url: str) -> None:
        """Update Git repo URL in DB."""
        try:
            from src.workspace.repository import WorkspaceRepository

            repo = WorkspaceRepository()
            await repo.update_git_repo(workspace_id, git_repo_url)
        except Exception as e:
            logger.warning("db_update_git_failed", error=str(e))

    async def _delete_in_db(self, workspace_id: str) -> None:
        """Soft-delete in DB."""
        try:
            from src.workspace.repository import WorkspaceRepository

            repo = WorkspaceRepository()
            await repo.delete(workspace_id)
        except Exception as e:
            logger.warning("db_delete_failed", error=str(e))

    async def _get_user_token(self, user_id: str) -> str:
        """Get user's Git token from DB."""
        try:
            from src.workspace.repository import UserGitCredentialRepository

            repo = UserGitCredentialRepository()
            cred = await repo.get_by_user(user_id)
            return cred.token if cred else ""
        except Exception as e:
            logger.warning("db_get_token_failed", error=str(e))
            return ""

    async def _setup_remote_git(self, ws: WorkspaceMetadata) -> None:
        """Set up remote Git for a workspace."""
        token = await self._get_user_token(ws.user_id)
        if not token:
            logger.warning("no_git_token_for_remote", user_id=ws.user_id)
            return

        from src.workspace.git_ops import GitOperations
        GitOperations.add_remote(ws.local_path, ws.git_repo_url, token=token)
        GitOperations.push(ws.local_path, branch=ws.git_branch)

    # ---- Private: Disk scan (fallback for local dev) ----

    def _read_dialect_from_config(self, config_path: Path) -> str:
        """Best-effort read of a workspace's dialect from its config.yaml.

        Falls back to the default_gateway connection type, then "duckdb".
        """
        try:
            import yaml

            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            model_defaults = data.get("model_defaults") or {}
            if isinstance(model_defaults, dict) and model_defaults.get("dialect"):
                return str(model_defaults["dialect"])

            default_gw = data.get("default_gateway")
            gateways = data.get("gateways") or {}
            gw = gateways.get(default_gw) if isinstance(gateways, dict) else None
            if isinstance(gw, dict):
                conn = gw.get("connection") or {}
                if isinstance(conn, dict) and conn.get("type"):
                    return str(conn["type"])
        except Exception as e:
            logger.warning("read_dialect_failed", path=str(config_path), error=str(e))
        return "duckdb"

    def _scan_disk_workspaces(
        self, user_id: t.Optional[str] = None
    ) -> t.Dict[str, WorkspaceMetadata]:
        """Scan base_path for workspaces on disk (dirs containing config.yaml).

        Disk is the source of truth for *locally present* workspaces. Returns a
        fresh map keyed by workspace_id and does NOT mutate the in-memory cache,
        so callers can reconcile it against the DB on demand.

        Directory convention: {base_path}/{user_id}/{workspace_name}/
        A directory is a workspace when it contains ``sql-warehouse/config.yaml``.
        """
        from src.workspace.layout import SQL_WAREHOUSE_DIR

        found: t.Dict[str, WorkspaceMetadata] = {}
        base = Path(self._base_path)
        if not base.exists():
            return found

        for user_dir in base.iterdir():
            if not user_dir.is_dir():
                continue
            uid = user_dir.name
            if user_id is not None and uid != user_id:
                continue

            for ws_dir in user_dir.iterdir():
                if not ws_dir.is_dir():
                    continue
                config_path = ws_dir / SQL_WAREHOUSE_DIR / "config.yaml"
                # Only treat directories with sql-warehouse/config.yaml as valid
                # workspaces (the new dual-directory layout).
                if not config_path.exists():
                    continue

                dir_name = ws_dir.name
                workspace_id = f"ws-{uid}-{dir_name}"
                found[workspace_id] = WorkspaceMetadata(
                    workspace_id=workspace_id,
                    user_id=uid,
                    name=dir_name,
                    local_path=str(ws_dir),
                    dialect=self._read_dialect_from_config(config_path),
                    status="active",
                )
        return found

    def _discover_existing_workspaces(self) -> None:
        """Warm the in-memory cache from disk at startup (non-destructive)."""
        for workspace_id, ws in self._scan_disk_workspaces().items():
            self._workspaces.setdefault(workspace_id, ws)
        logger.info("workspaces_discovered", count=len(self._workspaces))

    # ---- Private: Project scaffolding ----

    def _scaffold_project(self, path: str, name: str, dialect: str = "duckdb") -> None:
        """Scaffold the dual-directory workspace layout.

        A workspace root now contains two sibling projects:
        - ``sql-warehouse/`` : the SQLMesh project (created via ``sqlmesh init``)
        - ``asset-dag/``     : the Dagster Data+AI pipeline project

        Git is initialized at the workspace ROOT so both projects are versioned
        together.
        """
        from src.workspace.layout import ASSET_DAG_DIR, SQL_WAREHOUSE_DIR

        warehouse_path = os.path.join(path, SQL_WAREHOUSE_DIR)
        asset_dag_path = os.path.join(path, ASSET_DAG_DIR)

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
