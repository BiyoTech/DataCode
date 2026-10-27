"""A code location that cannot start must say so, and must not linger.

Two defects, found together in one production incident.

**A dead location was written into workspace.yaml anyway.** ``ensure()`` spawned
the gRPC server, waited, and then serialised every known location into the file
regardless of whether it was up. The webserver then loaded the dead entry on
every single request, failed to reach the port, and answered ``Could not reach
user code server. gRPC Error code: UNAVAILABLE``. That is a network-shaped error
for what is almost always a crashed import in user code, so the operator debugged
connectivity while the real cause sat unread in a subprocess stderr. The two log
lines proving it — process exited, then workspace.yaml written — were 0.5ms apart.

**The death was reported without how.** ``code_location_grpc_process_exited``
carried no exit status, while the supervisor's own event a few lines away did.
Distinguishing "exited 1, traceback on stderr" from "killed by SIGKILL, nothing
at all" is the difference between reading the user's import error and hunting a
memory limit, and it was not knowable from the log.

Together they made a broken code location look like a broken network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from structlog.testing import capture_logs

from src.codeloc.manager import CodeLocationManager, CodeLocationSpec
from tests.test_codeloc_process import WS_ENV, FakeProcessManager  # noqa: E402


@pytest.fixture
def process_manager(tmp_path, monkeypatch):
    """A process-mode manager whose OS side is faked, as in the routing tests."""
    base = tmp_path / "base"
    workspace = base / "t1" / "ws-t1-abc" / "u1" / "envs" / "dev"
    asset_dag = workspace / "asset-dag"
    asset_dag.mkdir(parents=True)
    (asset_dag / "definitions.py").write_text("# user code\n", encoding="utf-8")

    cfg = _settings(base, tmp_path)
    manager = CodeLocationManager(cfg)
    processes = FakeProcessManager()
    monkeypatch.setattr("src.codeloc.process.get_process_manager", lambda: processes)
    monkeypatch.setattr(manager, "_allocate_port", lambda name: 4321)
    monkeypatch.setattr(manager, "_ensure_segment", lambda: asyncio.sleep(0))
    monkeypatch.setattr(manager, "_propagate", lambda entry: asyncio.sleep(0))
    # The real readiness wait: these tests ARE about what happens when it fails.
    monkeypatch.setattr(manager._settings, "process_start_timeout", 2)

    spec = CodeLocationSpec(
        location_name="ws-t1-abc-dev",
        workspace_path=str(workspace),
        workspace_name="ws-t1-abc",
        env=dict(WS_ENV),
    )
    return manager, spec, processes


def _settings(base: Path, tmp_path: Path):
    from src.config import Settings

    cfg = Settings()
    cfg.workspace_base_path = str(base)
    cfg.code_location_mode = "process"
    cfg.dagster_home = str(tmp_path / "dagster_home")
    cfg.code_location_advertise_host = "runtime-1.datacode"
    return cfg


def _kill(processes, name: str, status: int | None = 1) -> None:
    proc = processes.spawned[name]
    proc.alive = False
    proc.exit_status = status


def _locations_in(home: str) -> list[str]:
    path = Path(home) / "workspace.yaml"
    content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [
        entry["grpc_server"]["location_name"] for entry in content.get("load_from", [])
    ]


class TestDeadLocationsAreOmitted:
    """The webserver must never be handed an endpoint that is not there."""

    async def test_a_location_that_dies_on_startup_is_not_in_the_file(
        self, process_manager, monkeypatch
    ):
        manager, spec, processes = process_manager
        # Spawn keeps the fake alive; the crash happens before the port opens.
        monkeypatch.setattr(
            manager,
            "_wait_grpc_ready",
            CodeLocationManager._wait_grpc_ready.__get__(manager),
        )
        original = processes.spawn

        def spawn_then_die(name, command, **kw):
            original(name, command, **kw)
            _kill(processes, name, status=1)
            return processes.spawned[name]

        monkeypatch.setattr(processes, "spawn", spawn_then_die)

        runtime = await manager.ensure(spec)

        assert runtime.alive is False
        assert _locations_in(manager._settings.dagster_home) == [], (
            "a location that never opened its port was written into "
            "workspace.yaml: the webserver will load it on every request and "
            "report an unreachable code server instead of the real cause"
        )

    async def test_a_later_healthy_location_is_still_written(
        self, process_manager, monkeypatch
    ):
        """Filtering must not turn into 'write nothing'."""
        manager, spec, processes = process_manager
        monkeypatch.setattr(
            manager, "_wait_grpc_ready", lambda entry: asyncio.sleep(0, result=True)
        )

        await manager.ensure(spec)

        assert _locations_in(manager._settings.dagster_home) == [spec.location_name]

    async def test_a_location_that_dies_later_drops_out_of_the_file(
        self, process_manager, monkeypatch
    ):
        """Once it is gone, the next write must not resurrect it."""
        manager, spec, processes = process_manager
        monkeypatch.setattr(
            manager, "_wait_grpc_ready", lambda entry: asyncio.sleep(0, result=True)
        )

        await manager.ensure(spec)
        assert _locations_in(manager._settings.dagster_home) == [spec.location_name]

        _kill(processes, manager._process_name(spec.location_name), status=1)
        manager._write_workspace_yaml()

        assert _locations_in(manager._settings.dagster_home) == []

    async def test_the_omission_is_logged_with_the_location_name(
        self, process_manager, monkeypatch
    ):
        """A location absent from the file is a decision; it has to be visible."""
        manager, spec, processes = process_manager
        monkeypatch.setattr(
            manager, "_wait_grpc_ready", lambda entry: asyncio.sleep(0, result=True)
        )
        await manager.ensure(spec)
        _kill(processes, manager._process_name(spec.location_name), status=1)

        with capture_logs() as logs:
            manager._write_workspace_yaml()

        written = [e for e in logs if e.get("event") == "workspace_yaml_written"]
        assert written, f"nothing was logged: {logs}"
        assert written[-1]["omitted_not_serving"] == [spec.location_name]
        assert written[-1]["locations"] == 0


class TestRestartAddsItBack:
    """The counterpart: dropping dead locations is only safe if a recovered one
    returns. Otherwise a single crash would hide the location permanently."""

    async def test_a_revived_location_reappears_in_the_file(
        self, process_manager, monkeypatch
    ):
        manager, spec, processes = process_manager
        name = manager._process_name(spec.location_name)
        monkeypatch.setattr(
            manager, "_wait_grpc_ready", lambda entry: asyncio.sleep(0, result=True)
        )
        await manager.ensure(spec)

        _kill(processes, name, status=1)
        manager._write_workspace_yaml()
        assert _locations_in(manager._settings.dagster_home) == []

        # The supervisor respawns it and the port opens again.
        processes.spawned[name].alive = True
        await manager._after_restart(manager._locations[spec.location_name])

        assert _locations_in(manager._settings.dagster_home) == [spec.location_name]

    async def test_a_restart_that_never_opens_its_port_is_not_added(
        self, process_manager, monkeypatch
    ):
        """Ready is not the same as alive: the port is what the webserver dials,
        so a respawned process that never binds one must stay out."""
        manager, spec, processes = process_manager
        name = manager._process_name(spec.location_name)
        await manager.ensure(spec)

        _kill(processes, name, status=1)
        manager._write_workspace_yaml()
        assert _locations_in(manager._settings.dagster_home) == []

        # The supervisor respawns it, but the port never opens.
        processes.spawned[name].alive = True
        monkeypatch.setattr(
            manager, "_wait_grpc_ready", lambda entry: asyncio.sleep(0, result=False)
        )

        with capture_logs() as logs:
            await manager._after_restart(manager._locations[spec.location_name])

        assert _locations_in(manager._settings.dagster_home) == []
        assert any(
            e.get("event") == "code_location_restart_not_serving" for e in logs
        ), f"the failed re-attach was not reported: {logs}"


class TestDeathReportsHow:
    """``exit_code`` is the whole diagnosis: 1 is a Python failure with a
    traceback on stderr, -9 is the kernel and produces no output at all."""

    @pytest.mark.parametrize(
        "status,expected",
        [
            (1, {"exit_code": 1}),
            (2, {"exit_code": 2}),
            (-9, {"exit_code": -9, "terminated_by": "SIGKILL"}),
            (-15, {"exit_code": -15, "terminated_by": "SIGTERM"}),
        ],
    )
    async def test_the_status_is_reported(self, process_manager, status, expected):
        manager, spec, processes = process_manager

        await manager.ensure(spec)
        entry = manager._locations[spec.location_name]
        _kill(processes, manager._process_name(spec.location_name), status=status)

        with capture_logs() as logs:
            ready = await manager._wait_grpc_ready(entry)

        assert ready is False
        exited = [e for e in logs if e.get("event") == "code_location_grpc_process_exited"]
        assert exited, f"the death was not reported: {logs}"
        for key, value in expected.items():
            assert exited[-1][key] == value, (
                f"{key} missing from {exited[-1]}: a killed process is "
                "indistinguishable from a failed one"
            )

    async def test_a_live_process_reports_no_status(self, process_manager, monkeypatch):
        """No death, no exit status: a stale code would mislead."""
        manager, spec, processes = process_manager
        monkeypatch.setattr(
            manager, "_wait_grpc_ready", lambda entry: asyncio.sleep(0, result=True)
        )
        await manager.ensure(spec)
        entry = manager._locations[spec.location_name]

        assert manager._exit_detail(entry) == {}

    async def test_a_vehicle_without_a_status_does_not_break_the_report(
        self, process_manager, monkeypatch
    ):
        """Enriching a warning must never break the path that emits it."""
        class _StatuslessProcess:
            """A vehicle that can report life but not how it ended."""

            def is_alive(self) -> bool:
                return False

        manager, spec, processes = process_manager
        await manager.ensure(spec)
        entry = manager._locations[spec.location_name]
        processes.spawned[manager._process_name(spec.location_name)] = _StatuslessProcess()

        with capture_logs() as logs:
            ready = await manager._wait_grpc_ready(entry)

        assert ready is False
        assert any(
            e.get("event") == "code_location_grpc_process_exited" for e in logs
        ), "the death went unreported because its detail could not be read"
