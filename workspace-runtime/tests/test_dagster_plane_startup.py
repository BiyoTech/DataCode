"""The Dagster instance processes must actually be started.

The defect these tests pin is an omission, not a wrong behaviour:
``DagsterProcessManager.start()`` — the only thing that spawns the Dagster
daemon, the webserver and the supervisor — was never called by anything.

The symptom is misleading by construction. Code locations are spawned
separately, per deployment, by the CodeLocationManager, so they come up and
their gRPC ports answer. What never appears is the INSTANCE: the GraphQL
endpoint on ``dagster.webserver_port`` (3001). Every asset-DAG query, schedule
listing and reload therefore fails with "Dagster instance is unavailable at
http://localhost:3001/graphql", logged as a transport error against a port that
was never bound — while code locations, SQLMesh, Git and files all look
perfectly healthy.

``TestLifespanStartsThePlane`` is the structural half: it fails if the wiring is
removed again. The rest pin the behaviour the wiring relies on.
"""

from __future__ import annotations

import ast
import os
import stat
import sys
from pathlib import Path

import pytest

from src.codeloc.errors import ProcessStartError
from src.codeloc.process import (
    DAEMON_PROCESS,
    WEBSERVER_PROCESS,
    DagsterProcessManager,
    ManagedProcess,
    dagster_search_path,
)
from src.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "workspace-runtime" / "src" / "main.py"


@pytest.fixture(autouse=True)
def _skip_instance_migrate(monkeypatch):
    """Stub out the pre-flight instance migration for this module.

    ``start()`` runs a real ``dagster instance migrate`` subprocess, which none
    of the assertions here are about; ``test_dagster_instance_migrate.py`` owns
    that behaviour.
    """

    async def noop(self) -> bool:
        return True

    monkeypatch.setattr(DagsterProcessManager, "_migrate_instance", noop)


def _manager(tmp_path, **overrides) -> DagsterProcessManager:
    """A manager in LOCAL mode, pointed at a throwaway Dagster home."""
    cfg = Settings()
    cfg.workspace_base_path = str(tmp_path / "base")
    cfg.dagster_home = str(tmp_path / "dagster_home")
    cfg.dagster_mode = "local"
    cfg.dagster_daemon_enabled = True
    cfg.dagster_webserver_enabled = True
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return DagsterProcessManager(cfg)


class TestLifespanStartsThePlane:
    """Structural: nothing else starts these processes, so the call must stay."""

    def test_lifespan_starts_the_process_manager(self) -> None:
        source = MAIN_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)

        lifespan = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
            ),
            None,
        )
        assert lifespan is not None, "main.py has no lifespan()"

        # Names bound to get_process_manager() inside lifespan, then whether any
        # of them has .start() called. Tracking the assignment rather than
        # guessing at the variable name: the name is an implementation detail,
        # the call is the contract.
        bound = {
            node.targets[0].id
            for node in ast.walk(lifespan)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "get_process_manager"
            and node.targets
            and isinstance(node.targets[0], ast.Name)
        }
        started = [
            node.func.attr
            for node in ast.walk(lifespan)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "start"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in bound
        ]
        assert bound, "lifespan() never obtains the Dagster process manager"
        assert started, (
            "lifespan() never calls the Dagster process manager's start(): the "
            "daemon, webserver and supervisor are never spawned, and every "
            "asset-DAG query fails against an unbound GraphQL port"
        )

    def test_lifespan_stops_the_process_manager(self) -> None:
        """Children run in their own session, so they outlive a group kill."""
        source = MAIN_PY.read_text(encoding="utf-8")
        assert "stop_all()" in source, (
            "lifespan() never stops the Dagster processes; they are spawned with "
            "start_new_session=True and would survive as orphans holding their ports"
        )


class TestLocalModeStartsTheInstance:
    async def test_both_instance_processes_are_spawned(self, tmp_path, monkeypatch):
        manager = _manager(tmp_path)
        spawned: list[str] = []
        monkeypatch.setattr(
            manager, "spawn", lambda name, cmd, **kw: spawned.append(name)
        )

        await manager.start()

        assert sorted(spawned) == sorted([DAEMON_PROCESS, WEBSERVER_PROCESS])
        # The supervisor is what respawns them (and the code locations) later.
        assert manager.health_check()["supervised"] is True

    async def test_both_commands_read_the_same_workspace_yaml(
        self, tmp_path, monkeypatch
    ):
        manager = _manager(tmp_path)
        commands: dict[str, list[str]] = {}
        monkeypatch.setattr(
            manager,
            "spawn",
            lambda name, cmd, **kw: commands.__setitem__(name, list(cmd)),
        )

        await manager.start()

        assert commands[DAEMON_PROCESS][:2] == ["dagster-daemon", "run"]
        assert commands[WEBSERVER_PROCESS][0] == "dagster-webserver"
        # The same file the code-location manager keeps in sync: without it
        # neither would see the locations registered later.
        daemon_ws = commands[DAEMON_PROCESS][commands[DAEMON_PROCESS].index("-w") + 1]
        web_ws = commands[WEBSERVER_PROCESS][commands[WEBSERVER_PROCESS].index("-w") + 1]
        assert daemon_ws == web_ws
        assert "3001" in commands[WEBSERVER_PROCESS]


class TestConsoleScriptsAreResolvable:
    """The second half of the same omission: the processes are started, but the
    ``dagster*`` console scripts could not be FOUND.

    The dev flow runs this service as ``.venv/bin/python -m uvicorn`` without
    activating the virtualenv, so ``sys.executable`` is the venv interpreter
    while ``.venv/bin`` — the only directory holding its console scripts — is
    absent from PATH. Every spawn then failed with ENOENT and the asset DAG
    reported an unbound GraphQL port instead.
    """

    def test_the_interpreters_bin_comes_first(self) -> None:
        path = dagster_search_path().split(os.pathsep)
        assert path[0] == str(Path(sys.executable).absolute().parent)

    def test_symlinks_are_not_resolved(self, tmp_path, monkeypatch) -> None:
        """A venv's ``python`` links to the base interpreter, whose ``bin/``
        holds no ``dagster`` — resolving would land in exactly the wrong place."""
        base_bin = tmp_path / "base" / "bin"
        venv_bin = tmp_path / "venv" / "bin"
        base_bin.mkdir(parents=True)
        venv_bin.mkdir(parents=True)
        (base_bin / "python3").write_text("", encoding="utf-8")
        (venv_bin / "python3").symlink_to(base_bin / "python3")

        monkeypatch.setattr(sys, "executable", str(venv_bin / "python3"))

        assert dagster_search_path().split(os.pathsep)[0] == str(venv_bin)

    def test_inherited_path_is_kept_after_it(self, monkeypatch) -> None:
        """Dagster's own subprocesses still need ``git`` and friends."""
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        assert dagster_search_path().split(os.pathsep)[1:] == ["/usr/bin", "/bin"]

    def test_every_dagster_subprocess_gets_it(self, tmp_path) -> None:
        """One place, all of them: instance processes AND code locations take
        their env from ``build_dagster_env``."""
        env = _manager(tmp_path).build_dagster_env()

        assert env["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).absolute().parent)

    def test_a_script_in_that_directory_is_executable(self, tmp_path) -> None:
        """Behavioural: PATH is what makes a bare argv[0] resolve, so a script
        that exists ONLY in the interpreter's bin/ must start."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        script = bin_dir / "dagster-fake"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

        proc = ManagedProcess(
            name="dagster-fake", command=["dagster-fake"], env={"PATH": str(bin_dir)}
        )
        proc.start()
        proc.stop(timeout=5)

    def test_an_unresolvable_script_names_what_was_searched(self, tmp_path) -> None:
        """The failure must be actionable: which executable, and which PATH."""
        proc = ManagedProcess(
            name=DAEMON_PROCESS,
            command=["dagster-daemon", "run"],
            env={"PATH": str(tmp_path / "empty")},
        )

        try:
            proc.start()
        except ProcessStartError as exc:
            assert "dagster-daemon" in (exc.detail or "")
            assert "PATH=" in (exc.detail or "")
        else:  # pragma: no cover - would mean an empty PATH resolved something
            raise AssertionError("spawn should have failed with ENOENT")


class TestFlagsAreHonoured:
    async def test_disabled_processes_are_not_spawned(self, tmp_path, monkeypatch):
        manager = _manager(
            tmp_path, dagster_daemon_enabled=False, dagster_webserver_enabled=False
        )
        commands: dict[str, list[str]] = {}
        monkeypatch.setattr(
            manager,
            "spawn",
            lambda name, cmd, **kw: commands.__setitem__(name, list(cmd)),
        )

        await manager.start()

        assert commands == {}

    async def test_only_the_enabled_one_is_spawned(self, tmp_path, monkeypatch):
        manager = _manager(tmp_path, dagster_daemon_enabled=False)
        commands: dict[str, list[str]] = {}
        monkeypatch.setattr(
            manager,
            "spawn",
            lambda name, cmd, **kw: commands.__setitem__(name, list(cmd)),
        )

        await manager.start()

        assert set(commands) == {WEBSERVER_PROCESS}


class TestRemoteMode:
    async def test_no_local_processes_are_spawned(self, tmp_path, monkeypatch):
        """A remote instance is somebody else's to run."""
        manager = _manager(tmp_path, dagster_mode="remote", dagster_remote_graphql_url="http://remote/graphql")
        commands: dict[str, list[str]] = {}
        monkeypatch.setattr(
            manager,
            "spawn",
            lambda name, cmd, **kw: commands.__setitem__(name, list(cmd)),
        )

        await manager.start()

        assert commands == {}
        # The supervisor still runs: it respawns this replica's code locations.
        assert manager.health_check()["supervised"] is True


class TestStartupDegradesInsteadOfFailing:
    """A broken data plane must not take the runtime down with it."""

    async def test_a_missing_binary_does_not_raise(self, tmp_path, monkeypatch):
        from src.codeloc.errors import ProcessStartError

        def fail(name, cmd, **kw):
            raise ProcessStartError(name, detail="No such file or directory")

        manager = _manager(tmp_path)
        monkeypatch.setattr(manager, "spawn", fail)

        # The whole point: this service also serves SQLMesh, Git, files and
        # leases, none of which need Dagster.
        await manager.start()

    async def test_one_failing_process_does_not_block_the_other(
        self, tmp_path, monkeypatch
    ):
        """Per-process guarding: the daemon must not take the webserver down."""
        from src.codeloc.errors import ProcessStartError

        manager = _manager(tmp_path)
        attempted: list[str] = []

        def flaky(name, cmd, **kw):
            attempted.append(name)
            if name == DAEMON_PROCESS:
                raise ProcessStartError(name, detail="No such file or directory")
            # The webserver succeeds — recorded without really spawning it,
            # which would bind the configured port during the test run.
            manager._processes[name] = object()
            return None

        monkeypatch.setattr(manager, "spawn", flaky)

        await manager.start()

        assert attempted == [DAEMON_PROCESS, WEBSERVER_PROCESS]
        assert WEBSERVER_PROCESS in manager._processes
        assert DAEMON_PROCESS not in manager._processes
