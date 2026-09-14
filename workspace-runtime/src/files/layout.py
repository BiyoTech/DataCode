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

from datacode_common.workspace_layout import (
    AGENT_DIR,
    AGENT_STATE_PREFIXES,
    LEGACY_SQLMESH_HOME_DIR,
    PLATFORM_STATE_DIR,
)

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Sub-directory names under a workspace root.
SQL_WAREHOUSE_DIR = "sql-warehouse"
ASSET_DAG_DIR = "asset-dag"

# Everything the platform records about a workspace at runtime lives under
# ``logs/``: the service timeline, task-scoped streams and the rich error
# archive. Keeping one root means the agent has a single place to look and the
# retention story is a single directory.
LOGS_DIR = "logs"
LOGS_SERVICE_DIR = "service"
LOGS_RUNS_DIR = "runs"
LOGS_ERRORS_DIR = "errors"
# Raw stdout/stderr of the code location server. Plain text, not NDJSON: it is
# whatever the user's own asset code printed, plus the traceback Dagster emits
# when importing it fails. That traceback is the ONLY place the real cause of a
# failed reload appears, so it has to land somewhere both the UI and the agent
# can read without a log store.
LOGS_CODELOC_DIR = "codeloc"

# The error archive used to sit at the workspace root. Readers still check the
# old location so workspaces created before the move keep their history.
LEGACY_ERRORS_DIR = "errors"

# Default ignore rules written at workspace creation.
#
# Everything listed here is REGENERABLE runtime output: platform state (the
# rendered project, the sandbox HOME, the log root), SQLMesh's cache and engine
# files, Dagster's local instance state, Python byte-code, virtual environments
# and tool caches, plus the agent's per-user state. Committing them would bloat
# the repository and produce constant, meaningless diffs across members who each
# hold their own working copy — or, for ``.agent/``, leak personal state.
#
# Paths are anchored to the workspace root (leading ``/``) where the entry only
# makes sense there, so a same-named user directory deeper in the tree is not
# silently ignored. Entries that must match at ANY depth (byte-code, caches,
# virtual environments) deliberately carry no leading slash: gitignore matches a
# pattern that holds no slash — other than a trailing one — in every directory.
#
# The directory NAMES come from ``datacode_common.workspace_layout``, the same
# module the explorer and the agent tools hide them by: a path that is written
# by the platform but tracked by Git is a drift nobody notices until it is in a
# commit.

# One rule per per-user agent directory, generated so the ignore file and the
# hidden-path policy cannot disagree.
_AGENT_STATE_RULES = "\n".join(f"/{prefix}/" for prefix in AGENT_STATE_PREFIXES)

WORKSPACE_GITIGNORE = f"""# DataCode workspace — generated at creation time.
# Runtime artifacts only: everything below is regenerable and must stay untracked.

# Platform state: rendered projects, the sandbox HOME (SQLMesh's personal
# directory and any other tool dotfile), and the log + error archive.
/{PLATFORM_STATE_DIR}/
/logs/
# Legacy locations (workspaces created before the current layout)
/errors/
/{LEGACY_SQLMESH_HOME_DIR}/

# Agent workspace state ({AGENT_DIR}/)
# Each member holds their own working copy, so per-user runtime state must never
# be committed — it would leak personal tasks, plans, memory and context to
# other members on pull. Team-shared conventions are listed but commented out:
# uncomment them once the team adopts them, so the choice to share is explicit
# rather than accidental.
# Team-shared agent conventions (intentionally committed)
# /{AGENT_DIR}/config.yaml
# /{AGENT_DIR}/rules/
# /{AGENT_DIR}/skills/custom/
# Per-user runtime state (must stay untracked)
{_AGENT_STATE_RULES}
# Memory notes are AUTHORED (the agent lists and reads them) but personal, so
# they stay visible in the copy and out of the repository.
/{AGENT_DIR}/memory/

# SQLMesh project ({SQL_WAREHOUSE_DIR}/)
/{SQL_WAREHOUSE_DIR}/logs/
/{SQL_WAREHOUSE_DIR}/*.db
/{SQL_WAREHOUSE_DIR}/*.db.wal

# Dagster project ({ASSET_DAG_DIR}/)
/{ASSET_DAG_DIR}/.dagster/

# Caches, byte-code and dependency trees — no leading slash: these appear under
# every package directory, at any depth, and must never be tracked wherever the
# tool that owns them decides to write.
__pycache__/
*.py[cod]
*.egg
*.egg-info/
.venv/
venv/
.cache/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.ipynb_checkpoints/
.tox/
node_modules/

# Local engine files. A DuckDB database is machine state wherever it sits, so
# unlike the ``*.db`` rules above it is not anchored to the SQLMesh project.
*.duckdb
*.duckdb.wal

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


def logs_root(workspace_path: str) -> str:
    """Return the platform log directory for a workspace root."""
    return str(Path(workspace_path) / LOGS_DIR)


def service_logs_dir(workspace_path: str) -> str:
    """Return the directory holding per-service NDJSON timelines."""
    return str(Path(logs_root(workspace_path)) / LOGS_SERVICE_DIR)


def run_logs_dir(workspace_path: str) -> str:
    """Return the directory holding task-scoped NDJSON streams."""
    return str(Path(logs_root(workspace_path)) / LOGS_RUNS_DIR)


def errors_dir(workspace_path: str) -> str:
    """Return the error archive directory."""
    return str(Path(logs_root(workspace_path)) / LOGS_ERRORS_DIR)


def codeloc_logs_dir(workspace_path: str) -> str:
    """Return the directory holding code-location stdout/stderr transcripts."""
    return str(Path(logs_root(workspace_path)) / LOGS_CODELOC_DIR)


def legacy_errors_dir(workspace_path: str) -> str:
    """Return the pre-``logs/`` error archive directory.

    Read-only: nothing writes here any more, but existing working copies may
    still hold history worth surfacing.
    """
    return str(Path(workspace_path) / LEGACY_ERRORS_DIR)


def sqlmesh_root(workspace_path: str) -> str:
    """Return the SQLMesh project directory for a workspace root."""
    return str(Path(workspace_path) / SQL_WAREHOUSE_DIR)


def asset_dag_root(workspace_path: str) -> str:
    """Return the Dagster (asset-dag) project directory for a workspace root."""
    return str(Path(workspace_path) / ASSET_DAG_DIR)
