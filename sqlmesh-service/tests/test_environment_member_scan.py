"""Tests for member enumeration behind multi-member resource release.

The platform keeps no membership table of its own (the gateway owns the role
bindings), so destroying an environment finds the copies it must release by
scanning the workspace's shared root. Getting this wrong is expensive in both
directions: missing a member leaves a worktree and a local branch that can
resurrect the environment on the next push, while claiming an unrelated
directory as a member would send Git operations at a path we do not own.
"""

from __future__ import annotations

import os

import pytest

from src.environments.git_binder import GitBinder
from src.workspace import paths as ws_paths

_TENANT = "t1"
_WORKSPACE = "ws-t1-sales"


def _make_member(base: str, user_id: str, *, with_repo: bool = True) -> str:
    """Create a member root the way ``ensure_repository`` / worktrees would."""
    user_root = ws_paths.user_root_path(base, _TENANT, _WORKSPACE, user_id)
    os.makedirs(
        os.path.join(user_root, ws_paths.REPO_DIR if with_repo else ws_paths.ENVS_DIR),
        exist_ok=True,
    )
    return user_root


@pytest.fixture
def base(tmp_path):
    return str(tmp_path)


async def test_lists_every_member_holding_a_local_root(base):
    _make_member(base, "alice")
    _make_member(base, "bob", with_repo=False)  # only worktrees, no bare repo yet

    members = await GitBinder(base).list_member_user_ids(
        tenant_id=_TENANT, workspace_id=_WORKSPACE
    )

    assert members == ["alice", "bob"]


async def test_unknown_workspace_has_no_members(base):
    members = await GitBinder(base).list_member_user_ids(
        tenant_id=_TENANT, workspace_id=_WORKSPACE
    )
    assert members == []


async def test_directories_that_are_not_user_roots_are_ignored(base):
    """A stray directory under the shared root is not a member: acting on it
    would run Git against a path outside the platform layout."""
    _make_member(base, "alice")
    shared = ws_paths.workspace_shared_root(base, _TENANT, _WORKSPACE)
    os.makedirs(os.path.join(shared, "scratch"), exist_ok=True)
    with open(os.path.join(shared, "notes.txt"), "w", encoding="utf-8") as handle:
        handle.write("not a member")

    members = await GitBinder(base).list_member_user_ids(
        tenant_id=_TENANT, workspace_id=_WORKSPACE
    )

    assert members == ["alice"]


async def test_names_that_are_not_valid_path_segments_are_ignored(base):
    """Defence in depth: only names that survive segment validation may be fed
    back into path derivation."""
    _make_member(base, "alice")
    shared = ws_paths.workspace_shared_root(base, _TENANT, _WORKSPACE)
    hostile = os.path.join(shared, ".hidden")
    os.makedirs(os.path.join(hostile, ws_paths.REPO_DIR), exist_ok=True)

    members = await GitBinder(base).list_member_user_ids(
        tenant_id=_TENANT, workspace_id=_WORKSPACE
    )

    assert members == ["alice"]


async def test_uncommitted_change_count_is_zero_without_a_working_copy(base):
    """Probing a member who never materialized the environment must not raise:
    the destroy path asks this for EVERY member."""
    _make_member(base, "alice")

    count = await GitBinder(base).uncommitted_change_count(
        tenant_id=_TENANT,
        workspace_id=_WORKSPACE,
        user_id="alice",
        environment="dev_alice",
    )

    assert count == 0
