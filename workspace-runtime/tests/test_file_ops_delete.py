"""Deleting a path in a working copy: its KIND picks the syscall.

Its scope decides whether the deletion is allowed at all — see below.

The defect this module exists for: ``os.remove`` on a directory reports EPERM
("Operation not permitted") on macOS and EISDIR on Linux. Both read like a
filesystem permissions problem while the real cause is that ``unlink(2)``
cannot remove a directory, so one "delete this folder" click became an opaque
500 that no chmod could fix.

These tests pin:

* one syscall per entry kind (file / directory / symlink), with a directory
  taking its subtree and a symlink never being followed;
* the two deletions that are refused outright (the root, and ``.git``);
* separator-aware containment, so a symlink cannot reach a SIBLING directory
  whose name merely extends the workspace's ("ws" vs "ws-backup");
* that the platform ``.gitignore`` is written BEFORE the first ``git add`` —
  a rule only stops a file from being ADDED, never one already tracked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.files import ops as file_ops  # noqa: E402
from src.files.layout import WORKSPACE_GITIGNORE  # noqa: E402
from src.git.ops import GitOperations  # noqa: E402
from src.utils.errors import InvalidRequestError  # noqa: E402


@pytest.fixture()
def ws(tmp_path) -> str:
    """A workspace root holding one file inside one directory."""
    root = tmp_path / "ws"
    (root / "models").mkdir(parents=True)
    (root / "models" / "top.sql").write_text("select 1", encoding="utf-8")
    return str(root)


# ── kind decides the syscall ─────────────────────────────────────────────


def test_delete_file(ws):
    assert file_ops.delete_path(ws, "models/top.sql") is True
    assert not os.path.exists(os.path.join(ws, "models/top.sql"))


def test_delete_empty_directory(ws):
    os.mkdir(os.path.join(ws, "empty"))
    assert file_ops.delete_path(ws, "empty") is True
    assert not os.path.exists(os.path.join(ws, "empty"))


def test_delete_directory_takes_its_subtree(ws):
    """The one delete semantic the explorer exposes: a folder goes whole."""
    nested = os.path.join(ws, "models", "staging")
    os.makedirs(nested)
    (Path(nested) / "inner.sql").write_text("select 2", encoding="utf-8")

    assert file_ops.delete_path(ws, "models") is True
    assert not os.path.exists(os.path.join(ws, "models"))
    assert not os.path.exists(os.path.join(nested, "inner.sql"))


def test_delete_symlink_never_follows_it(ws, tmp_path):
    """A link pointing at a directory is unlinked, not walked into."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious", encoding="utf-8")
    os.symlink(str(outside), os.path.join(ws, "link"))

    assert file_ops.delete_path(ws, "link") is True
    assert not os.path.lexists(os.path.join(ws, "link"))
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "precious"


def test_delete_broken_symlink(ws):
    os.symlink(os.path.join(ws, "gone"), os.path.join(ws, "dangling"))
    assert file_ops.delete_path(ws, "dangling") is True
    assert not os.path.lexists(os.path.join(ws, "dangling"))


def test_missing_path_reports_not_deleted(ws):
    assert file_ops.delete_path(ws, "models/absent.sql") is False


# ── scope: what may never be deleted ─────────────────────────────────────


@pytest.mark.parametrize("relative", ["", ".", "./"])
def test_workspace_root_is_refused(ws, relative):
    with pytest.raises(InvalidRequestError):
        file_ops.delete_path(ws, relative)
    assert os.path.isfile(os.path.join(ws, "models/top.sql"))


@pytest.mark.parametrize("relative", [".git", ".git/config", "models/.git"])
def test_git_metadata_is_refused(ws, relative):
    """``.git`` is the working copy's only durable carrier.

    The explorer never offers it, but the agent tool surface reaches this
    function directly, so the guard has to live with the implementation.
    """
    # The entries must exist: a path that is not there is reported as a miss
    # (False) long before any guard runs.
    (Path(ws) / ".git" / "objects").mkdir(parents=True)
    (Path(ws) / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (Path(ws) / "models" / ".git").mkdir()

    with pytest.raises(InvalidRequestError):
        file_ops.delete_path(ws, relative)

    assert (Path(ws) / ".git" / "config").exists()


@pytest.mark.parametrize("relative", ["../outside.txt", "/etc/passwd"])
def test_traversal_is_a_bad_request_not_a_miss(ws, relative):
    with pytest.raises(InvalidRequestError):
        file_ops.delete_path(ws, relative)


def test_symlink_cannot_reach_a_sibling_directory(tmp_path):
    """``startswith`` alone accepted ``ws-backup`` for a workspace named ``ws``."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sibling = tmp_path / "ws-backup"
    sibling.mkdir()
    (sibling / "secret.sql").write_text("select 3", encoding="utf-8")
    os.symlink(str(sibling), os.path.join(workspace, "backup"))

    with pytest.raises(InvalidRequestError):
        file_ops.delete_path(str(workspace), "backup/secret.sql")

    assert (sibling / "secret.sql").exists()


# ── byte-code caches: hidden in the tree, ignored by Git ─────────────────


def test_pycache_is_hidden_from_the_lazy_tree(ws):
    (Path(ws) / "__pycache__").mkdir()
    (Path(ws) / "__pycache__" / "mod.cpython-313.pyc").write_bytes(b"\x00")
    (Path(ws) / "models" / "__pycache__").mkdir()

    names = [f.path for f in file_ops.list_files(ws)]
    assert names == ["models"]


def test_pycache_is_hidden_from_the_recursive_index(ws):
    (Path(ws) / "__pycache__").mkdir()
    (Path(ws) / "__pycache__" / "mod.cpython-313.pyc").write_bytes(b"\x00")
    (Path(ws) / "models" / "__pycache__").mkdir()
    (Path(ws) / "models" / "__pycache__" / "inner.pyc").write_bytes(b"\x00")

    names = [f.path for f in file_ops.list_files(ws, recursive=True)]
    assert names == ["models/top.sql"]


def test_gitignore_covers_pycache_at_every_depth():
    """No leading slash: gitignore then matches the name in ANY directory."""
    assert "\n__pycache__/\n" in WORKSPACE_GITIGNORE


def test_git_init_writes_gitignore_before_the_first_add(tmp_path):
    """Rules written after ``git add`` would be too late: the file is tracked."""
    if shutil.which("git") is None:  # pragma: no cover - dev machines have git
        pytest.skip("git is not installed")

    workspace = tmp_path / "ws"
    (workspace / "models").mkdir(parents=True)
    (workspace / "models" / "top.sql").write_text("select 1", encoding="utf-8")
    cache = workspace / "models" / "__pycache__"
    cache.mkdir()
    (cache / "mod.cpython-313.pyc").write_bytes(b"\x00")
    (workspace / "logs").mkdir()
    (workspace / "logs" / "service.ndjson").write_text("{}\n", encoding="utf-8")

    assert GitOperations.init(str(workspace)) is True

    tracked = subprocess.run(
        ["git", "-C", str(workspace), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    assert "models/top.sql" in tracked
    assert ".gitignore" in tracked
    assert not [p for p in tracked if "__pycache__" in p]
    assert not [p for p in tracked if p.startswith("logs/")]
