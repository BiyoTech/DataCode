"""Workspace identifier and filesystem path conventions.

This module is the SINGLE SOURCE OF TRUTH for how a workspace is named and
where its local working copies live on disk.

Conventions
-----------
Workspace ID   : ``ws-{tenant_id}-{slug}``
Shared root    : ``{base}/{tenant_id}/{workspace_id}``
User work copy : ``{base}/{tenant_id}/{workspace_id}/{user_id}``

Rationale: a workspace is a TENANT-level shared resource whose only durable
code carrier is Git. The local directory is merely a disposable per-user Git
working copy, so it is never persisted in the database - it is always derived
from (base_path, tenant_id, workspace_id, user_id) at runtime.

All functions here are pure so they can be unit-tested without a filesystem
or database.
"""

from __future__ import annotations

import re
from pathlib import Path

# A path segment must be a conservative, filesystem-safe token. Rejecting
# anything else is a defence-in-depth measure against path traversal, because
# these values originate from authentication claims and user input.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Number of segments between base_path and the user working copy:
# {tenant_id}/{workspace_id}/{user_id}
_LAYOUT_DEPTH = 3

WORKSPACE_ID_PREFIX = "ws-"


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


def build_workspace_id(tenant_id: str, slug: str) -> str:
    """Build the canonical workspace ID ``ws-{tenant_id}-{slug}``."""
    validate_segment(tenant_id, field="tenant_id")
    return f"{WORKSPACE_ID_PREFIX}{tenant_id}-{slug}"


def workspace_shared_root(base_path: str, tenant_id: str, workspace_id: str) -> str:
    """Return the directory holding every user's working copy of a workspace."""
    validate_segment(tenant_id, field="tenant_id")
    validate_segment(workspace_id, field="workspace_id")
    return str(Path(base_path) / tenant_id / workspace_id)


def user_workspace_path(
    base_path: str, tenant_id: str, workspace_id: str, user_id: str
) -> str:
    """Return the current user's private working-copy path for a workspace."""
    validate_segment(user_id, field="user_id")
    return str(Path(workspace_shared_root(base_path, tenant_id, workspace_id)) / user_id)


def parse_workspace_path(base_path: str, path: str) -> tuple[str, str, str]:
    """Decompose a working-copy path into ``(tenant_id, workspace_id, user_id)``.

    Enforces the exact ``{base}/{tenant}/{workspace}/{user}`` layout: the path
    must be inside ``base_path`` and expose exactly three segments below it.
    Any extra or missing level is rejected, which prevents a caller from
    reaching a sibling tenant/user directory or the shared root itself.
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
            "path must match {base}/{tenant_id}/{workspace_id}/{user_id}, "
            f"got {len(parts)} segment(s) below base: {path}"
        )

    tenant_id, workspace_id, user_id = parts
    validate_segment(tenant_id, field="tenant_id")
    validate_segment(workspace_id, field="workspace_id")
    validate_segment(user_id, field="user_id")

    if not workspace_id.startswith(f"{WORKSPACE_ID_PREFIX}{tenant_id}-"):
        raise WorkspacePathLayoutError(
            f"workspace_id {workspace_id!r} does not belong to tenant {tenant_id!r}"
        )

    return tenant_id, workspace_id, user_id
