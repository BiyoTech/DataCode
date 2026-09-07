"""The working-copy visibility policy, pinned.

Three services hide the same directories in the same tree; this module is the
one definition they share. What the tests below defend, in order of how much a
regression would cost:

1. Matching is workspace-RELATIVE. The platform's own default workspace root is
   ``~/.datacode/workspaces/...``, so an absolute match on the platform state
   directory hides every file of every workspace — total, silent blindness.
2. ``.agent/`` is not one thing. Rules, memory notes, design documents and
   custom skills are authored content and must stay visible; only per-user
   runtime state is noise.
3. The two tiers stay ordered: anything invisible in the tree is also out of
   the index, never the reverse.
"""

from __future__ import annotations

import pytest

from datacode_common.workspace_layout import (
    AGENT_STATE_PREFIXES,
    HIDDEN_DIR_NAMES,
    NOISE_DIR_NAMES,
    PLATFORM_STATE_DIR,
    RENDERED_PROJECT_DIR,
    SQLMESH_HOME_DIR,
    TOOL_HOME_DIR,
    hidden_from_index,
    hidden_from_tree,
    sqlmesh_home,
    tool_home,
)

# ── the platform's own state ─────────────────────────────────────────────


def test_platform_state_is_invisible_everywhere():
    for rel in (
        PLATFORM_STATE_DIR,
        f"{RENDERED_PROJECT_DIR}/models/orders.sql",
        f"{SQLMESH_HOME_DIR}/user.yaml",
        f"{TOOL_HOME_DIR}/.gitconfig",
    ):
        assert hidden_from_tree(rel), rel
        assert hidden_from_index(rel), rel


def test_the_sandbox_homes_live_under_the_state_directory():
    """Pinned as a PATH, because the .gitignore and the hidden-path policy
    only agree on it by construction."""
    assert tool_home("/w/copy") == f"/w/copy/{TOOL_HOME_DIR}"
    assert sqlmesh_home("/w/copy") == f"/w/copy/{SQLMESH_HOME_DIR}"
    assert SQLMESH_HOME_DIR.startswith(f"{TOOL_HOME_DIR}/")


# ── relative, never absolute ─────────────────────────────────────────────


def test_a_workspace_under_the_platform_directory_is_still_visible():
    """The dev default root is ``~/.datacode/workspaces``.

    Judging an ABSOLUTE path would match ``.datacode`` in the components ABOVE
    the workspace and hide the entire tree. Callers pass relative paths; this
    pins that the policy is written for them.
    """
    assert not hidden_from_index("sql-warehouse/models/orders.sql")
    assert not hidden_from_tree("sql-warehouse/models/orders.sql")


def test_leading_and_dot_components_are_ignored():
    assert hidden_from_tree("./.git/config")
    assert hidden_from_tree("/.git/config")
    assert not hidden_from_index("./models/orders.sql")


def test_the_workspace_root_itself_is_never_hidden():
    for rel in ("", ".", "/"):
        assert not hidden_from_tree(rel)
        assert not hidden_from_index(rel)


# ── the two tiers ────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(HIDDEN_DIR_NAMES))
def test_tree_hidden_names_are_hidden_at_any_depth(name):
    assert hidden_from_tree(f"sql-warehouse/models/{name}/x")


@pytest.mark.parametrize("name", sorted(NOISE_DIR_NAMES))
def test_noise_is_browsable_but_out_of_the_index(name):
    """Browsable on purpose: a user who wants to look into ``.venv/`` may."""
    assert not hidden_from_tree(f"{name}/lib/thing.py")
    assert hidden_from_index(f"{name}/lib/thing.py")


def test_the_index_tier_contains_the_tree_tier():
    for name in HIDDEN_DIR_NAMES:
        assert hidden_from_index(f"{name}/x"), name


# ── build output is anchored, unlike the rest ────────────────────────────


def test_root_build_output_is_noise():
    assert hidden_from_index("dist/app.js")
    assert hidden_from_index("build/lib/x.py")


def test_a_users_build_directory_deeper_in_the_tree_is_content():
    """``build`` and ``dist`` are ordinary words: ``models/dist/`` is a data
    mart, not a wheel."""
    assert not hidden_from_index("sql-warehouse/models/dist/revenue.sql")
    assert not hidden_from_index("sql-warehouse/models/build/staging.sql")


# ── .agent: content stays, per-user state goes ───────────────────────────


@pytest.mark.parametrize(
    "rel",
    [
        ".agent/rules/style.md",
        ".agent/memory/2026-09-01.md",
        ".agent/design/warehouse.md",
        ".agent/config.yaml",
        ".agent/skills/custom/my_skill/SKILL.md",
    ],
)
def test_agent_authored_content_stays_visible(rel):
    """The design subagent's ONLY artefact lives here.

    Hiding ``.agent/`` wholesale made the agent unable to see what it had just
    been instructed to write.
    """
    assert not hidden_from_tree(rel)
    assert not hidden_from_index(rel)


@pytest.mark.parametrize("prefix", AGENT_STATE_PREFIXES)
def test_agent_runtime_state_is_out_of_the_index(prefix):
    assert hidden_from_index(prefix)
    assert hidden_from_index(f"{prefix}/2026-09-01.json")
    # Still browsable: it is the user's own state, in their own copy.
    assert not hidden_from_tree(f"{prefix}/2026-09-01.json")


def test_a_user_directory_named_tasks_is_not_agent_state():
    """The prefixes are paths, not directory names."""
    assert not hidden_from_index("tasks/backfill.sql")
    assert not hidden_from_index("sql-warehouse/models/tasks/daily.sql")


def test_prefix_matching_is_separator_aware():
    """``.agent/tasks-archive`` is not ``.agent/tasks``."""
    assert not hidden_from_index(".agent/tasks-archive/old.json")
