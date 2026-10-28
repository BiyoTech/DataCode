"""The scaffold must reach the ``sqlmesh`` CLI through THIS interpreter.

The defect this pins is environmental, which is what made it invisible: nothing
in the code was wrong, the process simply could not SEE the tool it asked for.

``subprocess.run(["sqlmesh", ...])`` resolves the name against PATH, and this
service is started as ``<svc>/.venv/bin/python -m uvicorn`` WITHOUT activating
the virtualenv - so ``.venv/bin``, the only directory holding the ``sqlmesh``
console script, is absent from PATH. Every scaffold then raised
``FileNotFoundError``, was caught, and took the hand-written fallback: a
``sql-warehouse/`` whose ``models/`` is empty and whose ``config.yaml`` carries
none of what ``sqlmesh init`` generates. The workspace was still created and
reported SUCCESS, so the user met the damage later, as "there is nothing here".

Two properties follow, and both are pinned below:

* the command is RESOLVED before it is run, so it survives an environment whose
  PATH knows nothing about this interpreter;
* what SQLMesh itself generates is what lands on disk, so a regression shows up
  as a missing sample model rather than as a silent empty directory.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.workspace.scaffold import WorkspaceProvisioner  # noqa: E402


@pytest.fixture()
def captured_init(monkeypatch):
    """Run ``_scaffold_sqlmesh`` with the subprocess replaced by a recorder.

    The fake creates ``models/`` because the reconciler that runs afterwards
    reads that directory, not because the scaffold is expected to.
    """
    calls: list = []

    def record(command, **kwargs):
        calls.append({"command": command, "kwargs": kwargs})
        Path(kwargs.get("cwd", "."), "models").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", record)
    return calls


@pytest.fixture()
def project(tmp_path):
    return str(tmp_path)


def test_init_resolves_the_script_next_to_the_running_interpreter(captured_init, project) -> None:
    """The scaffold never leaves ``sqlmesh`` to be resolved by name.

    A bare name is what broke: PATH did not contain this interpreter's ``bin/``,
    so POSIX answered ENOENT before SQLMesh ran.
    """
    WorkspaceProvisioner()._scaffold_sqlmesh(project, "ws-test", "duckdb", "ws-test-id")

    assert captured_init, "no subprocess was spawned, so nothing was verified"
    executable = captured_init[0]["command"][0]
    assert Path(executable).is_absolute(), (
        f"the sqlmesh command {executable!r} is a bare name, so POSIX resolves "
        f"it against PATH - which omits this interpreter's bin/ whenever the "
        f"service runs as .venv/bin/python -m uvicorn"
    )
    assert Path(executable).name in ("sqlmesh", "sqlmesh.exe")


def test_init_resolution_survives_an_unnavigable_path(monkeypatch, captured_init, project) -> None:
    """Resolution happens OUTSIDE PATH, so an unusable PATH changes nothing."""
    monkeypatch.setenv("PATH", "/nonexistent-bin")

    WorkspaceProvisioner()._scaffold_sqlmesh(project, "ws-test", "duckdb", "ws-test-id")

    assert captured_init, "no subprocess was spawned, so nothing was verified"
    assert Path(captured_init[0]["command"][0]).is_absolute()


def test_init_is_non_interactive_and_offline(captured_init, project) -> None:
    """A server cannot answer a prompt, and must not send telemetry either."""
    WorkspaceProvisioner()._scaffold_sqlmesh(project, "ws-test", "duckdb", "ws-test-id")

    command, kwargs = captured_init[0]["command"], captured_init[0]["kwargs"]
    assert command[1:] == ["init", "duckdb"]
    assert kwargs["stdin"] is subprocess.DEVNULL, (
        "an inherited stdin lets a prompt block a workspace-creation request for the whole timeout"
    )
    assert kwargs["env"]["SQLMESH__DISABLE_ANONYMIZED_ANALYTICS"] == "true"


def test_an_empty_dialect_still_generates_a_project(captured_init, project) -> None:
    """``init`` prompts for an engine when none is given - so supply the default.

    That prompt is what makes this shape dangerous rather than merely wrong: a
    server has no way to answer it.
    """
    WorkspaceProvisioner()._scaffold_sqlmesh(project, "ws-test", "", "ws-test-id")

    assert captured_init[0]["command"][1:] == ["init", "duckdb"]


@pytest.mark.skipif(
    shutil.which("sqlmesh") is None and not (Path(sys.executable).parent / "sqlmesh").is_file(),
    reason="no sqlmesh CLI in this interpreter's environment",
)
def test_the_real_cli_generates_the_example_models(tmp_path) -> None:
    """Behavioural: what actually lands on disk, produced by SQLMesh itself.

    Deliberately outside the mocked fixtures - this is the assertion the
    fallback silently failed: an empty ``models/`` and a four-key
    ``config.yaml``. Skipped rather than weakened when sqlmesh is absent,
    because then falling back IS the correct behaviour.
    """
    WorkspaceProvisioner()._scaffold_sqlmesh(str(tmp_path), "ws-test", "duckdb", "ws-test-id")

    models = sorted(p.name for p in (tmp_path / "models").glob("*.sql"))
    assert models, (
        "models/ is empty: the scaffold fell back to its hand-written skeleton "
        "instead of generating SQLMesh's own example project"
    )

    config = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "project: ws-test" in config
    assert "state_schema: sqlmesh__ws_test_id" in config
