"""agent-service must not reach a working copy through the filesystem.

The copy belongs to the workspace-runtime replica that holds its lease. This
service is given no volume for it in any container deployment, and even where
the path happens to be reachable, opening it here puts a SECOND writer inside a
leased Git worktree.

The property is negative, so it is invisible at every call site: a tool that
does ``Path(workspace_path, file).read_text()`` reads perfectly reasonably and
works on the developer's laptop. It failed only in containers — with
``[Errno 13] Permission denied: '/data'`` — which is the worst possible place to
find out. Hence the structural assertion below: the modules that serve a working
copy may not name a filesystem primitive at all.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_SRC = Path(__file__).resolve().parents[1] / "src"

# Functions that touch a filesystem. Names, not resolved symbols: the guard has
# to fire on ``open(...)`` and on ``some_path.read_text()`` alike, and a name is
# what both have in common.
_FS_CALLS = frozenset(
    {
        "open",
        "makedirs",
        "mkdir",
        "listdir",
        "scandir",
        "walk",
        "remove",
        "unlink",
        "rmdir",
        "rmtree",
        "copyfile",
        "copytree",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "iterdir",
        "glob",
        "rglob",
        "touch",
    }
)

# Modules that serve a WORKING COPY and must therefore go through
# src.tools.workspace_fs.
_GUARDED = (
    "tools/read_file.py",
    "tools/create_file.py",
    "tools/list_dir.py",
    "tools/search_code.py",
    "tools/apply_patch.py",
    "tools/workspace_fs.py",
    "tools/_file_utils.py",
    "context/session.py",
    "runtime/memory_post.py",
    "runtime/todo_board.py",
)

# Where a filesystem call is legitimate, with the reason it is not a working
# copy. Keyed by module, valued by the call names allowed in it.
_ALLOWED: dict[str, frozenset[str]] = {
    # Office documents are parsed from bytes fetched over HTTP and staged in
    # this process's OWN temporary directory, never the working copy.
    "tools/read_file.py": frozenset({"open"}),
}


# The façade deliberately borrows pathlib's vocabulary (``read_text``,
# ``write_text``) because it does the same thing to the same conceptual object —
# so the guard has to look at the RECEIVER, not just the method name. Anything
# called ON the façade is the sanctioned route; anything else with those names
# is a path.
_FACADE = "workspace_fs"


def _fs_calls_in(path: Path) -> list[tuple[str, int]]:
    """Every filesystem-primitive call in *path*, as (name, line)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name) and func.value.id == _FACADE:
                continue
            name = func.attr
        else:
            continue
        if name in _FS_CALLS:
            found.append((name, node.lineno))
    return found


@pytest.mark.parametrize("module", _GUARDED)
def test_module_does_not_touch_the_filesystem(module: str) -> None:
    allowed = _ALLOWED.get(module, frozenset())
    offenders = [
        f"{module}:{line} -> {name}()"
        for name, line in _fs_calls_in(_SRC / module)
        if name not in allowed
    ]
    assert offenders == [], (
        "these modules serve a working copy, which belongs to workspace-runtime; "
        "use src.tools.workspace_fs instead of the filesystem: " + "; ".join(offenders)
    )


def test_the_guard_would_catch_a_regression(tmp_path: Path) -> None:
    """The guard is only worth having if it fires — prove it on a fake module."""
    offender = tmp_path / "offender.py"
    offender.write_text(
        "from pathlib import Path\n"
        "def read(workspace_path, name):\n"
        "    return Path(workspace_path, name).read_text()\n",
        encoding="utf-8",
    )
    assert [name for name, _ in _fs_calls_in(offender)] == ["read_text"]


def test_file_tools_go_through_the_runtime_facade() -> None:
    """Positive counterpart: the tools must actually import the façade.

    Without this, deleting the last workspace_fs call from a tool would leave
    the negative assertion above perfectly satisfied by a tool that does
    nothing at all.
    """
    missing = []
    for module in (
        "tools/read_file.py",
        "tools/create_file.py",
        "tools/list_dir.py",
        "tools/search_code.py",
        "tools/apply_patch.py",
        "context/session.py",
    ):
        source = (_SRC / module).read_text(encoding="utf-8")
        if "workspace_fs" not in source:
            missing.append(module)
    assert missing == [], f"no longer reaching the working copy at all: {missing}"


def test_shell_tool_creation_does_not_prepare_a_local_venv() -> None:
    """Building the tool must not write into the copy.

    ``create_native_shell_tool`` used to call ``ensure_ready()``, which creates
    a venv INSIDE the working copy — so every agent carrying ``shell`` died at
    construction time in a container, on a code path that never runs a command
    locally anyway.
    """
    source = (_SRC / "tools" / "shell.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    factory = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "create_native_shell_tool"
    )
    calls = [
        node.func.attr
        for node in ast.walk(factory)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "ensure_ready" not in calls
