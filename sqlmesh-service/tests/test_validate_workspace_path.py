"""Tests for workspace path validation."""

from pathlib import Path

import pytest

from src.api.deps import validate_workspace_path
from src.config import settings
from src.utils.errors import WorkspacePathInvalidError


@pytest.fixture
def workspace_base(tmp_path, monkeypatch):
    base = tmp_path / "workspaces"
    base.mkdir()
    ws = base / "project-a"
    ws.mkdir()
    monkeypatch.setattr(settings, "workspace_base_path", str(base))
    return base, ws


def test_validate_accepts_path_inside_base(workspace_base):
    _, ws = workspace_base
    assert validate_workspace_path(str(ws)) == str(ws.resolve())


def test_validate_rejects_path_outside_base(workspace_base, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(WorkspacePathInvalidError):
        validate_workspace_path(str(outside))


def test_validate_rejects_nonexistent_path(workspace_base):
    base, _ = workspace_base
    missing = base / "missing"
    with pytest.raises(WorkspacePathInvalidError):
        validate_workspace_path(str(missing))


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
