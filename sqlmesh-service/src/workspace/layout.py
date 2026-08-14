"""Workspace directory layout helpers.

A workspace root now contains two sibling projects:

- ``sql-warehouse/`` : the SQLMesh project (models, config.yaml, audits, ...)
- ``asset-dag/``     : the Dagster project (Data+AI pipeline definitions)

Git and generic file operations act on the workspace ROOT (the whole tree),
while all SQLMesh-scoped operations act on the ``sql-warehouse/`` sub-directory.
Centralizing the sub-directory names here keeps that split in one place.
"""

from __future__ import annotations

from pathlib import Path

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Sub-directory names under a workspace root.
SQL_WAREHOUSE_DIR = "sql-warehouse"
ASSET_DAG_DIR = "asset-dag"

# Default ignore rules written at workspace creation.
#
# Everything listed here is REGENERABLE runtime output: SQLMesh's local cache
# and run logs, the platform's error archive, Dagster's local instance state,
# Python byte-code, local virtual environments and engine data files. Committing
# them would bloat the repository and produce constant, meaningless diffs across
# members who each hold their own working copy.
#
# Paths are anchored to the workspace root (leading ``/``) where the entry only
# makes sense there, so a same-named user directory deeper in the tree is not
# silently ignored.
WORKSPACE_GITIGNORE = f"""# AgenticData workspace — generated at creation time.
# Runtime artifacts only: everything below is regenerable and must stay untracked.

# Platform error archive
/errors/
/logs/
# SQLMesh project ({SQL_WAREHOUSE_DIR}/)
/{SQL_WAREHOUSE_DIR}/.cache/
/{SQL_WAREHOUSE_DIR}/logs/
/{SQL_WAREHOUSE_DIR}/*.db
/{SQL_WAREHOUSE_DIR}/*.db.wal
/{SQL_WAREHOUSE_DIR}/*.duckdb
/{SQL_WAREHOUSE_DIR}/*.duckdb.wal

# Dagster project ({ASSET_DAG_DIR}/)
/{ASSET_DAG_DIR}/.dagster/
/{ASSET_DAG_DIR}/*.egg-info/

# Python
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
.ipynb_checkpoints/

# Local virtual environments
/venv/
/.venv/

# Local environment overrides — never commit credentials
.env
.env.*
!.env.example

# Editor / OS noise
.DS_Store
.idea/
.vscode/
"""


def gitignore_path(workspace_path: str) -> str:
    """Return the workspace-root ``.gitignore`` path."""
    return str(Path(workspace_path) / ".gitignore")


def ensure_gitignore(workspace_path: str) -> bool:
    """Write the default ``.gitignore`` when the workspace has none.

    Never overwrites an existing file: a workspace materialized from an existing
    repository owns its own ignore rules.

    Returns True when the file was created.
    """
    target = Path(gitignore_path(workspace_path))
    if target.exists():
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(WORKSPACE_GITIGNORE, encoding="utf-8")
    logger.info("workspace_gitignore_created", path=str(target))
    return True


def sqlmesh_root(workspace_path: str) -> str:
    """Return the SQLMesh project directory for a workspace root."""
    return str(Path(workspace_path) / SQL_WAREHOUSE_DIR)


def asset_dag_root(workspace_path: str) -> str:
    """Return the Dagster (asset-dag) project directory for a workspace root."""
    return str(Path(workspace_path) / ASSET_DAG_DIR)
