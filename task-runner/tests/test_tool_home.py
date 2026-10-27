"""Where a task's tools write their dotfiles.

The runner is the only place that knows the path the vehicle actually mounted
(the submitter rewrites the working-copy path for a container), so it is the
place that pins HOME and ``SQLMESH_HOME``. Pinning matters for two reasons that
have nothing to do with tidiness:

* SQLMesh reads a PERSONAL ``config.yaml`` from its home and merges it OVER the
  project's, so the default location is a configuration-override surface;
* in a container HOME belongs to the image — ``/nonexistent`` for the runner's
  unprivileged uid, on a read-only root filesystem — and SQLMesh would fail to
  create its directory there at all.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datacode_common.task_protocol import TaskSpec  # noqa: E402

from src.main import _pin_tool_home, run_task  # noqa: E402


def _spec(workspace_path: str) -> TaskSpec:
    return TaskSpec(
        task_id="t1",
        kind="agent.shell",
        workspace_path=workspace_path,
        params={"command": "true"},
    )


def test_homes_land_in_the_platform_state_directory(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("SQLMESH_HOME", raising=False)

    _pin_tool_home("/workspace")

    assert os.environ["HOME"] == "/workspace/.datacode/home"
    assert os.environ["SQLMESH_HOME"] == "/workspace/.datacode/home/.sqlmesh"


def test_an_image_supplied_home_is_replaced(monkeypatch):
    """``/nonexistent`` is what the runner's uid gets from the base image."""
    monkeypatch.setenv("HOME", "/nonexistent")

    _pin_tool_home("/workspace")

    assert os.environ["HOME"] != "/nonexistent"


def test_the_home_directory_is_created(monkeypatch, tmp_path):
    """``git`` does not create its home; SQLMesh does. The one that does not
    must still find it, and only the vehicle knows the mounted path."""
    _pin_tool_home(str(tmp_path))

    assert (tmp_path / ".datacode" / "home").is_dir()


def test_an_uncreatable_home_is_not_fatal(monkeypatch, tmp_path):
    """A read-only mount is a legitimate sandbox shape.

    Tools that only READ a home are unaffected, and a task that genuinely needs
    to write one fails with a message naming the tool — more useful than
    failing the whole task here.
    """
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("", encoding="utf-8")

    _pin_tool_home(str(blocked))

    assert os.environ["HOME"] == f"{blocked}/.datacode/home"


def test_a_login_shell_does_not_source_a_committed_profile(tmp_path):
    """``agent.shell`` runs ``bash -lc``, and a login shell sources
    ``$HOME/.bash_profile``.

    While HOME was the working-copy ROOT, a ``.bash_profile`` arriving with the
    repository was executed before EVERY agent command — arbitrary code from a
    merged pull request, running with the sandbox's credentials. Moving HOME
    into the platform's state directory removes the vector, and this test is
    what keeps it removed.
    """
    from src.shell import op_shell

    (tmp_path / ".bash_profile").write_text(
        "export DATACODE_PWNED=yes\n", encoding="utf-8"
    )
    _pin_tool_home(str(tmp_path))

    result = op_shell(str(tmp_path), {"command": "echo ${DATACODE_PWNED:-clean}"})

    assert result["stdout"].strip() == "clean"


def test_nothing_is_pinned_without_a_workspace(monkeypatch):
    """A spec with no path is rejected elsewhere; do not invent ``/.datacode``."""
    monkeypatch.setenv("HOME", "/original")

    _pin_tool_home("")

    assert os.environ["HOME"] == "/original"


def test_running_a_task_pins_before_the_handler_runs(monkeypatch, tmp_path):
    """Pinned from the SPEC, on every task — the warm form serves many.

    Import order is the whole point: SQLMesh resolves its personal directory at
    import time, so a handler must never be the first thing to touch it.
    """
    monkeypatch.delenv("SQLMESH_HOME", raising=False)
    seen: dict[str, str] = {}

    def _fake_run(kind, workspace_path, params, **kwargs):
        seen["sqlmesh_home"] = os.environ.get("SQLMESH_HOME", "")
        return {"ok": True}

    monkeypatch.setattr("src.handlers.run", _fake_run)

    run_task(_spec(str(tmp_path)))

    assert seen["sqlmesh_home"] == f"{tmp_path}/.datacode/home/.sqlmesh"
