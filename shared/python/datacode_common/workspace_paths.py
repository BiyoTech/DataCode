"""Workspace identifier and filesystem path conventions (design 07, P1).

Promoted from ``sqlmesh-service/src/workspace/paths.py`` to the shared
library so workspace-runtime and control-service derive paths from ONE
module. The naming algebra itself lives in :mod:`datacode_common.naming`;
this module owns only the path layout.

Conventions
-----------
Workspace ID   : ``ws-{tenant_id}-{slug}``
Shared root    : ``{base}/{tenant_id}/{workspace_id}``
User root      : ``{base}/{tenant_id}/{workspace_id}/{user_id}``
Object database: ``{user_root}/.repo``            (bare clone, shared objects)
Working copy   : ``{user_root}/envs/{environment}`` (one Git worktree per env)

Rationale: a workspace is a TENANT-level shared resource whose only durable
code carrier is Git. Local directories are disposable, so they are never
persisted in the database - they are always derived from
(base_path, tenant_id, workspace_id, user_id, environment) at runtime.

Why one worktree per environment rather than one checkout per user:

* the shared ``.repo`` object database makes disk usage O(1) in the number of
  environments instead of O(envs);
* Git itself enforces that a branch is checked out by at most one worktree,
  which is exactly the Environment <-> Branch 1:1 invariant we want;
* switching environment costs nothing and is never blocked by uncommitted
  changes, so several environments can be planned in parallel.

All functions here are pure so they can be unit-tested without a filesystem
or database.
"""

from __future__ import annotations

import re
from pathlib import Path

from datacode_common.naming import ENVS_DIR, is_valid_env_name

# A path segment must be a conservative, filesystem-safe token. Rejecting
# anything else is a defence-in-depth measure against path traversal, because
# these values originate from authentication claims and user input.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Number of segments between base_path and an environment working copy:
# {tenant_id}/{workspace_id}/{user_id}/envs/{environment}
_LAYOUT_DEPTH = 5

WORKSPACE_ID_PREFIX = "ws-"

# Bare Git repository shared by every worktree of one user.
REPO_DIR = ".repo"


class WorkspacePathLayoutError(ValueError):
    """Raised when a path/identifier does not match the workspace layout."""


def slugify(name: str) -> str:
    """Normalize a workspace display name into a filesystem-safe slug.

    Collapses any run of non-alphanumeric characters into a single ``-``,
    lowercases the result and trims separators. Raises when the name contains
    no usable character at all, so callers surface a clear validation error
    instead of silently creating an unnamed directory.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    slug = slug[:48].strip("-")
    if not slug:
        raise WorkspacePathLayoutError(
            "workspace name must contain at least one alphanumeric character"
        )
    return slug


def validate_segment(value: str, *, field: str) -> str:
    """Validate that ``value`` is usable as a single path segment."""
    if not _SEGMENT_RE.match(value or ""):
        raise WorkspacePathLayoutError(f"invalid {field}: {value!r}")
    return value


def validate_environment(value: str) -> str:
    """Validate an environment name used as a directory segment."""
    if not is_valid_env_name(value):
        raise WorkspacePathLayoutError(f"invalid environment: {value!r}")
    return value


def build_workspace_id(tenant_id: str, slug: str) -> str:
    """Build the canonical workspace ID ``ws-{tenant_id}-{slug}``."""
    validate_segment(tenant_id, field="tenant_id")
    return f"{WORKSPACE_ID_PREFIX}{tenant_id}-{slug}"


def workspace_shared_root(base_path: str, tenant_id: str, workspace_id: str) -> str:
    """Return the directory holding every user's copies of a workspace."""
    validate_segment(tenant_id, field="tenant_id")
    validate_segment(workspace_id, field="workspace_id")
    return str(Path(base_path) / tenant_id / workspace_id)


def user_root_path(
    base_path: str, tenant_id: str, workspace_id: str, user_id: str
) -> str:
    """Return a user's private root: it holds ``.repo`` and ``envs/``.

    This is NOT a working copy: no SQLMesh/Dagster operation may target it.
    """
    validate_segment(user_id, field="user_id")
    return str(Path(workspace_shared_root(base_path, tenant_id, workspace_id)) / user_id)


def repo_path(base_path: str, tenant_id: str, workspace_id: str, user_id: str) -> str:
    """Return the bare Git repository shared by all of a user's worktrees."""
    return str(Path(user_root_path(base_path, tenant_id, workspace_id, user_id)) / REPO_DIR)


def envs_root(base_path: str, tenant_id: str, workspace_id: str, user_id: str) -> str:
    """Return the parent directory of a user's environment worktrees."""
    return str(Path(user_root_path(base_path, tenant_id, workspace_id, user_id)) / ENVS_DIR)


def environment_workspace_path(
    base_path: str,
    tenant_id: str,
    workspace_id: str,
    user_id: str,
    environment: str,
) -> str:
    """Return the working-copy path of one environment (a Git worktree root).

    This is the value carried as ``workspace_path`` across every service.
    """
    validate_environment(environment)
    return str(
        Path(envs_root(base_path, tenant_id, workspace_id, user_id)) / environment
    )


def parse_workspace_path(base_path: str, path: str) -> tuple[str, str, str, str]:
    """Decompose a working copy into ``(tenant, workspace, user, environment)``.

    Enforces the exact ``{base}/{tenant}/{workspace}/{user}/envs/{env}`` layout:
    the path must be inside ``base_path`` and expose exactly five segments below
    it. Any extra or missing level is rejected, which prevents a caller from
    reaching a sibling tenant/user directory, the shared object database or the
    user root itself.
    """
    base = Path(base_path).expanduser().resolve()
    target = Path(path).expanduser().resolve()

    try:
        relative = target.relative_to(base)
    except ValueError as exc:
        raise WorkspacePathLayoutError(
            f"path is outside the workspace base directory: {path}"
        ) from exc

    parts = relative.parts
    if len(parts) != _LAYOUT_DEPTH:
        raise WorkspacePathLayoutError(
            "path must match {base}/{tenant_id}/{workspace_id}/{user_id}/envs/"
            f"{{environment}}, got {len(parts)} segment(s) below base: {path}"
        )

    tenant_id, workspace_id, user_id, envs_dir, environment = parts
    validate_segment(tenant_id, field="tenant_id")
    validate_segment(workspace_id, field="workspace_id")
    validate_segment(user_id, field="user_id")
    if envs_dir != ENVS_DIR:
        raise WorkspacePathLayoutError(
            f"environment working copies must live under {ENVS_DIR!r}: {path}"
        )
    validate_environment(environment)

    if not workspace_id.startswith(f"{WORKSPACE_ID_PREFIX}{tenant_id}-"):
        raise WorkspacePathLayoutError(
            f"workspace_id {workspace_id!r} does not belong to tenant {tenant_id!r}"
        )

    return tenant_id, workspace_id, user_id, environment
