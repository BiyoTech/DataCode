"""What a working copy holds besides user content, and who may see it.

A working copy mixes three kinds of content:

* **user content** — SQL models, Dagster definitions, docs, agent rules and
  design notes. The reason the copy exists.
* **platform state** — everything DataCode itself writes into the tree: the
  rendered project of the write split, the HOME a sandboxed child is given,
  the log root.
* **regenerable output** — byte-code, tool caches, virtual environments,
  third-party dependency trees.

Only the first is worth showing in an explorer, indexing for search, feeding to
an LLM or committing to Git. The other two are defined HERE, once, because
three services act on the same tree and had each grown their own list:

* ``workspace-runtime`` — the explorer tree, the @-mention index, the
  ``.gitignore`` written at creation time, the sandbox environment;
* ``agent-service``     — ``list_dir`` / ``search_code``;
* ``task-runner``       — where the rendered project is written.

Three copies of "what is machine-written" drift in three directions, and the
drift is silent: a directory missing from one list is not an error anywhere, it
is just an artefact that suddenly shows up in a listing, in a search result or
in a commit.

Two visibility tiers, not one
-----------------------------
``hidden_from_tree`` — never shown ANYWHERE, not even in a file explorer.
Reserved for trees where a user decision is meaningless (Git's object store) or
actively misleading (a rendered copy of their own project).

``hidden_from_index`` — a superset: additionally drops machine-written trees
from recursive walks, search and @-mention indexing. These stay browsable, so a
user who wants to look into ``.venv/`` still can; they simply do not drown the
real project files, and a recursive walk does not pay to enumerate them.

Paths are workspace-relative. Matching an ABSOLUTE path against these names is
a defect, not a shortcut: the dev default workspace root is
``~/.datacode/workspaces/...``, so an absolute match on ``.datacode`` hides
every file in the workspace.
"""

from __future__ import annotations

import posixpath

# ── platform-owned state inside the working copy ─────────────────────────
#
# One root, hidden and ignored, so that "the platform wrote this" is a single
# place instead of a growing set of dotfiles at the workspace root.
PLATFORM_STATE_DIR = ".datacode"

# HOME for every sandboxed child. A child's HOME must stay inside the one
# working copy it may touch (a tool writing a dotfile then stays in the
# sandbox), but the workspace ROOT is the wrong place for it: every tool's
# dotfile would land among the user's models, and — worse — a ``.gitconfig``
# committed to the repository would become the sandbox's GLOBAL git config,
# which is an execution vector (aliases, ``core.pager``, ``diff.external``).
TOOL_HOME_DIR = f"{PLATFORM_STATE_DIR}/home"

# SQLMesh's personal directory. It holds the anonymous-telemetry user id
# (``user.yaml``) and — the reason this is pinned rather than left to HOME —
# an OPTIONAL personal ``config.yaml`` that SQLMesh merges AFTER the project's
# own, and therefore wins: a file dropped at the default location could
# repoint gateways, connections and state storage of a sandboxed run.
#
# Its value is exactly where HOME would put it, so a vehicle that sets HOME and
# a vehicle that sets ``SQLMESH_HOME`` agree. It must be set EXPLICITLY anyway:
# ``sqlmesh.core.constants`` resolves the path at IMPORT time, and containers
# run with a HOME the image chose (``/nonexistent`` for uid 65534 on a
# read-only root filesystem — SQLMesh would fail to create it).
SQLMESH_HOME_DIR = f"{TOOL_HOME_DIR}/.sqlmesh"

# Phase 1 of the write split renders the project to plain SQL here, and phase 2
# applies THAT. Platform state: a second copy of the user's project must never
# look like an uncommitted change of theirs.
RENDERED_PROJECT_DIR = f"{PLATFORM_STATE_DIR}/rendered"

# Where SQLMesh's personal directory USED to land, back when HOME was the
# workspace root. Kept only so existing copies stay clean; nothing writes it.
LEGACY_SQLMESH_HOME_DIR = ".sqlmesh"

# ── agent state ──────────────────────────────────────────────────────────
AGENT_DIR = ".agent"

# Per-user, per-turn runtime state under ``.agent/``. Rewritten constantly and
# already reachable through dedicated tools (todo board, memory index), so it
# is noise in a listing or a search — while everything ELSE under ``.agent/``
# (rules, memory notes, design documents, custom skills) is authored content
# and must stay visible. The design subagent's only artefact lives there.
#
# Matched as a path PREFIX, not as a directory name: a user directory that
# happens to be called ``tasks`` is theirs.
AGENT_STATE_PREFIXES: tuple[str, ...] = (
    f"{AGENT_DIR}/tasks",
    f"{AGENT_DIR}/plans",
    f"{AGENT_DIR}/context",
    f"{AGENT_DIR}/skills/installed",
)

# ── visibility tiers ─────────────────────────────────────────────────────

# Never listed, at any depth. Each entry earns its place by the same test:
# would showing it ever lead to a useful decision?
HIDDEN_DIR_NAMES: frozenset[str] = frozenset(
    {
        # Git's object store: thousands of machine-written files, and deleting
        # or editing any of them is never what the user meant.
        ".git",
        # Byte-code, regenerated on every run of the user's Python models.
        "__pycache__",
        # The platform's own state, including a full rendered copy of the
        # user's project — showing it invites "why are there two projects?".
        PLATFORM_STATE_DIR,
        LEGACY_SQLMESH_HOME_DIR,
    }
)

# Additionally dropped from recursive walks, search and @-mention indexing:
# regenerable or third-party trees, unambiguous by name at any depth.
NOISE_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".cache",  # SQLMesh's project cache
        ".dagster",  # Dagster's local instance state
        ".venv",
        "venv",
        "node_modules",
        ".tox",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".ipynb_checkpoints",
    }
)

# Build output — but ONLY at the workspace root. Unlike the names above,
# ``build`` and ``dist`` are ordinary English words: a user's
# ``models/dist/`` is a data mart, not a wheel. Anchoring mirrors the leading
# slash convention of the generated ``.gitignore``.
ROOT_NOISE_DIRS: frozenset[str] = frozenset({"build", "dist"})


def platform_state_root(workspace_path: str) -> str:
    """Absolute path of the platform's state directory in a working copy."""
    return posixpath.join(workspace_path, PLATFORM_STATE_DIR)


def tool_home(workspace_path: str) -> str:
    """Absolute ``HOME`` for a child sandboxed onto *workspace_path*."""
    return posixpath.join(workspace_path, TOOL_HOME_DIR)


def sqlmesh_home(workspace_path: str) -> str:
    """Absolute ``SQLMESH_HOME`` for a child sandboxed onto *workspace_path*."""
    return posixpath.join(workspace_path, SQLMESH_HOME_DIR)


def rendered_project_root(workspace_path: str) -> str:
    """Absolute path phase 1 of the write split renders into."""
    return posixpath.join(workspace_path, RENDERED_PROJECT_DIR)


def hidden_from_tree(rel_path: str) -> bool:
    """Whether *rel_path* must not appear even in a file explorer.

    Args:
        rel_path: Path relative to the workspace root. An absolute path is a
            caller defect (see the module docstring) and is treated as
            relative, which is the safe direction: it may reveal, never hide.
    """
    return any(part in HIDDEN_DIR_NAMES for part in _parts(rel_path))


def hidden_from_index(rel_path: str) -> bool:
    """Whether *rel_path* must be skipped by recursive walks and search.

    A superset of :func:`hidden_from_tree`: everything invisible in the tree
    plus the machine-written trees that stay browsable but are noise in a
    result set.
    """
    parts = _parts(rel_path)
    if not parts:
        return False
    if any(part in HIDDEN_DIR_NAMES or part in NOISE_DIR_NAMES for part in parts):
        return True
    if parts[0] in ROOT_NOISE_DIRS:
        return True

    joined = "/".join(parts)
    return any(
        joined == prefix or joined.startswith(prefix + "/")
        for prefix in AGENT_STATE_PREFIXES
    )


def _parts(rel_path: str) -> list[str]:
    """Split a workspace-relative path into meaningful components."""
    normalized = rel_path.replace("\\", "/")
    return [part for part in normalized.split("/") if part not in ("", ".")]


__all__ = [
    "AGENT_DIR",
    "AGENT_STATE_PREFIXES",
    "HIDDEN_DIR_NAMES",
    "LEGACY_SQLMESH_HOME_DIR",
    "NOISE_DIR_NAMES",
    "PLATFORM_STATE_DIR",
    "RENDERED_PROJECT_DIR",
    "ROOT_NOISE_DIRS",
    "SQLMESH_HOME_DIR",
    "TOOL_HOME_DIR",
    "hidden_from_index",
    "hidden_from_tree",
    "platform_state_root",
    "rendered_project_root",
    "sqlmesh_home",
    "tool_home",
]
