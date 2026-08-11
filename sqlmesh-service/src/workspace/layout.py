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

# Sub-directory names under a workspace root.
SQL_WAREHOUSE_DIR = "sql-warehouse"
ASSET_DAG_DIR = "asset-dag"


def sqlmesh_root(workspace_path: str) -> str:
    """Return the SQLMesh project directory for a workspace root."""
    return str(Path(workspace_path) / SQL_WAREHOUSE_DIR)


def asset_dag_root(workspace_path: str) -> str:
    """Return the Dagster (asset-dag) project directory for a workspace root."""
    return str(Path(workspace_path) / ASSET_DAG_DIR)
