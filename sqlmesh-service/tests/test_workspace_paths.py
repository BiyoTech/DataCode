"""Tests for workspace identifier / path conventions (pure functions)."""

import pytest

from src.workspace.paths import (
    WorkspacePathLayoutError,
    build_workspace_id,
    environment_workspace_path,
    envs_root,
    parse_workspace_path,
    repo_path,
    slugify,
    user_root_path,
    workspace_shared_root,
)


def test_slugify_normalizes_display_name():
    assert slugify("  My Sales  WS! ") == "my-sales-ws"


def test_slugify_rejects_name_without_alphanumerics():
    with pytest.raises(WorkspacePathLayoutError):
        slugify("---")


def test_workspace_id_embeds_tenant():
    assert build_workspace_id("t1", "sales") == "ws-t1-sales"


def test_paths_follow_the_worktree_layout():
    shared = workspace_shared_root("/base", "t1", "ws-t1-sales")
    assert shared == "/base/t1/ws-t1-sales"

    user_root = user_root_path("/base", "t1", "ws-t1-sales", "u9")
    assert user_root == f"{shared}/u9"
    assert repo_path("/base", "t1", "ws-t1-sales", "u9") == f"{user_root}/.repo"
    assert envs_root("/base", "t1", "ws-t1-sales", "u9") == f"{user_root}/envs"
    assert (
        environment_workspace_path("/base", "t1", "ws-t1-sales", "u9", "dev_alice")
        == f"{user_root}/envs/dev_alice"
    )


def test_parse_round_trips_a_valid_path():
    path = environment_workspace_path("/base", "t1", "ws-t1-sales", "u9", "prod")
    assert parse_workspace_path("/base", path) == ("t1", "ws-t1-sales", "u9", "prod")


@pytest.mark.parametrize(
    "path",
    [
        "/base/t1/ws-t1-sales",                    # shared root
        "/base/t1/ws-t1-sales/u9",                 # user root, not a working copy
        "/base/t1/ws-t1-sales/u9/.repo",           # bare object database
        "/base/t1/ws-t1-sales/u9/envs",            # envs container
        "/base/t1/ws-t1-sales/u9/envs/prod/models",  # too deep
        "/base/t1/ws-t1-sales/u9/copies/prod",     # wrong container directory
        "/base/t1/ws-t1-sales/u9/envs/Prod",       # environment names are lowercase
        "/base/t1/ws-t1-sales/u9/envs/my-env",     # "-" is not allowed in env names
        "/base/t1/ws-t2-sales/u9/envs/prod",       # workspace_id from another tenant
        "/elsewhere/t1/ws-t1-sales/u9/envs/prod",  # outside base
    ],
)
def test_parse_rejects_invalid_layouts(path):
    with pytest.raises(WorkspacePathLayoutError):
        parse_workspace_path("/base", path)


@pytest.mark.parametrize("segment", ["../evil", "a/b", "", "-lead", "x" * 65])
def test_identifier_segments_are_rejected_when_unsafe(segment):
    with pytest.raises(WorkspacePathLayoutError):
        user_root_path("/base", segment, "ws-t1-sales", "u9")


@pytest.mark.parametrize("environment", ["../evil", "prod/x", "", "9dev", "x" * 41])
def test_environment_segment_is_rejected_when_unsafe(environment):
    with pytest.raises(WorkspacePathLayoutError):
        environment_workspace_path("/base", "t1", "ws-t1-sales", "u9", environment)
