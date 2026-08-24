"""Tests for workspace path validation.

Valid working-copy paths must match
``{base}/{tenant_id}/{workspace_id}/{user_id}/envs/{environment}``: a path
identifies a workspace AND an environment.
"""

from pathlib import Path

import pytest

from src.api.deps import validate_workspace_path
from src.utils.errors import WorkspacePathInvalidError

TENANT_ID = "t1"
WORKSPACE_ID = "ws-t1-project-a"
USER_ID = "u1"
ENVIRONMENT = "prod"


@pytest.fixture
def workspace_base(tmp_path, monkeypatch):
    base = tmp_path / "workspaces"
    ws = base / TENANT_ID / WORKSPACE_ID / USER_ID / "envs" / ENVIRONMENT
    ws.mkdir(parents=True)
    # ``workspace_base_path`` is a computed property sourced from platform.yaml
    # with a per-service env override, so the override is what tests set.
    monkeypatch.setenv("SQLMESH_SERVICE_WORKSPACE_BASE_PATH", str(base))
    return base, ws


def test_validate_accepts_well_formed_working_copy(workspace_base):
    _, ws = workspace_base
    assert validate_workspace_path(str(ws)) == str(ws.resolve())


def test_validate_rejects_path_outside_base(workspace_base, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(WorkspacePathInvalidError):
        validate_workspace_path(str(outside))


def test_validate_rejects_nonexistent_path(workspace_base):
    base, _ = workspace_base
    missing = base / TENANT_ID / WORKSPACE_ID / USER_ID / "envs" / "dev_missing"
    with pytest.raises(WorkspacePathInvalidError):
        validate_workspace_path(str(missing))


def test_validate_rejects_shared_root(workspace_base):
    """The shared root holds every member's material and is not a working copy."""
    base, _ = workspace_base
    with pytest.raises(WorkspacePathInvalidError):
        validate_workspace_path(str(base / TENANT_ID / WORKSPACE_ID))


def test_validate_rejects_user_root(workspace_base):
    """The user root holds ``.repo`` and ``envs/``; it is not a working copy."""
    base, _ = workspace_base
    with pytest.raises(WorkspacePathInvalidError):
        validate_workspace_path(str(base / TENANT_ID / WORKSPACE_ID / USER_ID))


def test_validate_rejects_bare_object_database(workspace_base):
    """``.repo`` is the shared object database and must never be operated on."""
    base, _ = workspace_base
    repo = base / TENANT_ID / WORKSPACE_ID / USER_ID / ".repo"
    repo.mkdir(parents=True)
    with pytest.raises(WorkspacePathInvalidError):
        validate_workspace_path(str(repo))


def test_validate_rejects_nested_subdirectory(workspace_base):
    """A directory below the working copy root is not a workspace path."""
    _, ws = workspace_base
    nested = ws / "sql-warehouse"
    nested.mkdir()
    with pytest.raises(WorkspacePathInvalidError):
        validate_workspace_path(str(nested))


def test_validate_rejects_workspace_id_from_another_tenant(workspace_base):
    """A workspace_id must carry the tenant it is nested under."""
    base, _ = workspace_base
    foreign = base / TENANT_ID / "ws-t2-project-b" / USER_ID / "envs" / ENVIRONMENT
    foreign.mkdir(parents=True)
    with pytest.raises(WorkspacePathInvalidError):
        validate_workspace_path(str(foreign))


def test_validate_rejects_sibling_prefix_path(workspace_base):
    """Paths that share a string prefix with base must still be rejected."""
    base, _ = workspace_base
    sibling = base.parent / f"{base.name}-evil"
    sibling.mkdir()
    try:
        with pytest.raises(WorkspacePathInvalidError):
            validate_workspace_path(str(sibling))
    finally:
        sibling.rmdir()


def test_validate_rejects_traversal_escape(workspace_base, tmp_path):
    """`..` segments must not be usable to climb out of the base."""
    base, _ = workspace_base
    escape = (
        base / TENANT_ID / WORKSPACE_ID / USER_ID / "envs" / ENVIRONMENT
        / ".." / ".." / ".." / ".." / ".."
    )
    with pytest.raises(WorkspacePathInvalidError):
        validate_workspace_path(str(escape))
    assert Path(base).exists()
