"""Tests for workspace identifier / path conventions (pure functions)."""

import pytest

from src.workspace.paths import (
    WorkspacePathLayoutError,
    build_workspace_id,
    parse_workspace_path,
    slugify,
    user_workspace_path,
    workspace_shared_root,
)


def test_slugify_normalizes_display_name():
    assert slugify("  My Sales  WS! ") == "my-sales-ws"


def test_slugify_rejects_name_without_alphanumerics():
    with pytest.raises(WorkspacePathLayoutError):
        slugify("---")


def test_workspace_id_embeds_tenant():
    assert build_workspace_id("t1", "sales") == "ws-t1-sales"


def test_paths_follow_four_segment_layout():
    shared = workspace_shared_root("/base", "t1", "ws-t1-sales")
    assert shared == "/base/t1/ws-t1-sales"
    assert user_workspace_path("/base", "t1", "ws-t1-sales", "u9") == f"{shared}/u9"


def test_parse_round_trips_a_valid_path():
    path = user_workspace_path("/base", "t1", "ws-t1-sales", "u9")
    assert parse_workspace_path("/base", path) == ("t1", "ws-t1-sales", "u9")


@pytest.mark.parametrize(
    "path",
    [
        "/base/t1/ws-t1-sales",           # shared root, not a working copy
        "/base/t1/ws-t1-sales/u9/models",  # too deep
        "/base/t1/ws-t2-sales/u9",         # workspace_id from another tenant
        "/elsewhere/t1/ws-t1-sales/u9",    # outside base
    ],
)
def test_parse_rejects_invalid_layouts(path):
    with pytest.raises(WorkspacePathLayoutError):
        parse_workspace_path("/base", path)


@pytest.mark.parametrize("segment", ["../evil", "a/b", "", "-lead", "x" * 65])
def test_identifier_segments_are_rejected_when_unsafe(segment):
    with pytest.raises(WorkspacePathLayoutError):
        user_workspace_path("/base", segment, "ws-t1-sales", "u9")
