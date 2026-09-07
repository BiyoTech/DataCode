"""Ownership contract: this service must never WRITE control-owned tables.

``workspaces`` and ``user_git_credentials`` belong to control-service. Control's
migrate step creates them and control's REST API performs every write to them
(workspace lifecycle in ``control-service/src/workspace``, credential CRUD in
``control-service/src/api/rest/git_credentials.py``). This service reads them
only, to resolve a Git remote or a token for an operation on a working copy.

The write methods used to exist here as verbatim copies of control's repository
and had no caller at all — which is precisely why they could rot unnoticed. The
copy of control's workspace-reclaim routine carried a real defect (an UPDATE
never populates the caller's in-memory model, so the response serializer hit
``None.isoformat()``) that had already been fixed on the control side while this
copy kept it. Sixteen dead methods were removed with it. (Control's own routine
is gone too: workspace deletion is now physical, so no tombstone is left to
reclaim — which is exactly the kind of divergence a dead copy would have hidden.)

These tests make that boundary structural rather than documented: re-adding a
write — anywhere, not just in the repository classes — fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
REPOSITORY = SRC / "workspace" / "repository.py"

# Tables control-service owns. ``file_drafts`` is deliberately absent: this
# service owns and writes it, and the checks below must not constrain it.
CONTROL_OWNED_MODELS = {"WorkspaceModel", "UserGitCredentialModel"}

# Methods each read-only repository may expose. The list is a whitelist on
# purpose: a new read is a decision, and a new write is a contract violation.
ALLOWED_METHODS = {
    "WorkspaceRepository": {"get_by_id"},
    "UserGitCredentialRepository": {
        "list_by_user",
        "get_default",
        "list_for_host",
        "get_for_host",
        "get_for_provider",
    },
}

# Reads the rest of the service depends on; removing one is a breaking change
# even though it is not a write.
REQUIRED_METHODS = {
    "WorkspaceRepository": {"get_by_id"},
    "UserGitCredentialRepository": {"get_default", "get_for_host", "get_for_provider"},
}


def _python_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def _walks() -> list[tuple[Path, ast.AST]]:
    return [(p, ast.parse(p.read_text(), filename=str(p))) for p in _python_files()]


def _repositories() -> dict[str, ast.ClassDef]:
    """The two read-only repository classes, by name."""
    tree = ast.parse(REPOSITORY.read_text(), filename=str(REPOSITORY))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name in ALLOWED_METHODS
    }


def _method_names(node: ast.ClassDef) -> set[str]:
    return {
        child.name
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_no_write_method_on_a_control_owned_repository() -> None:
    """A write method here is a second, unreviewed answer to a question
    control-service already answered — and, as the removed copies showed, one
    that drifts because nothing exercises it.
    """
    for class_name, node in _repositories().items():
        extra = _method_names(node) - ALLOWED_METHODS[class_name]
        assert not extra, (
            f"{REPOSITORY.name}:{node.lineno} class {class_name} defines "
            f"{sorted(extra)}, which write control-owned tables. Control-service "
            "is the sole writer; add the operation there and call it over HTTP."
        )


def test_read_methods_the_service_depends_on_still_exist() -> None:
    """Deleting a live read is just as breaking as adding a write: the lease
    wiring and three Git call sites resolve through these.
    """
    for class_name, node in _repositories().items():
        missing = REQUIRED_METHODS[class_name] - _method_names(node)
        assert not missing, (
            f"{REPOSITORY.name}:{node.lineno} class {class_name} is missing "
            f"{sorted(missing)}, which this service calls."
        )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_update_statement_targets_a_control_owned_table(path: Path) -> None:
    """``update(WorkspaceModel)`` / ``update(UserGitCredentialModel)`` is a
    write, wherever it appears — not only inside the repository classes.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "update"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name) and arg.id in CONTROL_OWNED_MODELS:
                pytest.fail(
                    f"{path.relative_to(SRC)}:{node.lineno} issues UPDATE against "
                    f"{arg.id}, a control-owned table. Control-service is the "
                    "sole writer."
                )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_insert_of_a_control_owned_row(path: Path) -> None:
    """``session.add(WorkspaceModel(...))`` inserts a row this service does not
    own; control-service's migrate step is the table's only creator and its API
    the only inserter.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_session_add = (
            isinstance(func, ast.Attribute)
            and func.attr == "add"
            and isinstance(func.value, ast.Name)
            and func.value.id == "session"
        )
        if not is_session_add:
            continue
        for arg in node.args:
            if (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Name)
                and arg.func.id in CONTROL_OWNED_MODELS
            ):
                pytest.fail(
                    f"{path.relative_to(SRC)}:{node.lineno} inserts a "
                    f"{arg.func.id} row. Control-service owns this table; "
                    "create it through its API instead."
                )
