"""``dc deps`` and the ``pip install`` refusal, through the real router.

The routing decision is the property under test, so it is exercised from the
command STRING down: a test that called ``run_deps_command`` directly would pass
even if the router never claimed the verb, which is exactly the regression that
would let ``pip install`` run natively.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from datacode_common.task_protocol import TaskResult, TaskStatus


class _Submitter:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[str] = []

    async def submit(self, kind, workspace_path, **kwargs):
        self.calls.append(kind)
        return self.result


def _success() -> TaskResult:
    from datacode_common.python_deps import (
        interpreter_platform_tag,
        interpreter_python_version,
    )

    return TaskResult(
        task_id="t1",
        status=TaskStatus.SUCCEEDED,
        result={
            "packages": [{"name": "polars", "version": "1.2.0"}],
            "platform_provided": [{"name": "dagster", "version": "1.13.21"}],
            "shadowed": [],
            "python_version": interpreter_python_version(),
            "platform_tag": interpreter_platform_tag(),
            "size_bytes": 2048,
            "output_tail": "Successfully installed polars-1.2.0",
        },
    )


def _copy(tmp_path: Path, requirements: str) -> str:
    copy = tmp_path / "base" / "t1" / "ws-t1-shop" / "u1" / "envs" / "dev"
    (copy / "asset-dag").mkdir(parents=True)
    (copy / "asset-dag" / "pyproject.toml").write_text(
        f'[project]\nname = "assets"\ndependencies = [{requirements}]\n',
        encoding="utf-8",
    )
    return str(copy)


@pytest.fixture()
def router(monkeypatch):
    """The real router with a stubbed sandbox, returned with its submitter."""
    from src.config import settings
    from src.deps import service as deps_service
    from src.tools.command_router import get_command_router

    monkeypatch.setattr(settings, "deps_index_url", "https://index.invalid/simple")
    submitter = _Submitter(_success())
    monkeypatch.setattr(
        deps_service, "_service", deps_service.DepsService(submitter=submitter)
    )
    return get_command_router(), submitter


async def _run(router, command: str, workspace_path: str) -> dict:
    from src.tools.command_router import classify

    routed = classify(command)
    assert routed is not None, f"{command!r} must be claimed by the router"
    return await router.execute(
        routed,
        workspace_path=workspace_path,
        environment="dev",
        user_id="u1",
    )


# ── pip install ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pip install polars", "pip3 install polars==1.2"])
async def test_pip_install_is_refused_with_the_alternative(
    router, tmp_path, command
):
    """A package installed by hand vanishes with the vehicle that ran it."""
    instance, submitter = router
    path = _copy(tmp_path, "")

    result = await _run(instance, command, path)

    assert result["exit_code"] != 0
    assert "pyproject.toml" in result["stderr"]
    assert "dc deps sync" in result["stderr"]
    # Nothing was installed and no sandbox was asked for one.
    assert submitter.calls == []


@pytest.mark.asyncio
async def test_pip_list_is_not_governed(router, tmp_path):
    """Reading what is installed is how a dependency problem is diagnosed."""
    from src.tools.command_router import classify

    assert classify("pip list") is None


# ── dc deps status ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deps_status_reports_without_installing(router, tmp_path):
    instance, submitter = router
    path = _copy(tmp_path, '"polars>=1.0"')

    result = await _run(instance, "dc deps status", path)

    assert result["exit_code"] == 0
    assert "missing" in result["stdout"]
    assert "Declared  : 1" in result["stdout"]
    assert submitter.calls == []


@pytest.mark.asyncio
async def test_deps_status_json_is_machine_readable(router, tmp_path):
    instance, _ = router
    path = _copy(tmp_path, '"polars>=1.0"')

    result = await _run(instance, "dc deps status --json", path)

    payload = json.loads(result["stdout"])
    assert payload["state"] == "missing"
    assert payload["declared"] == 1


# ── dc deps sync ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deps_sync_installs_and_reports_what_it_did(router, tmp_path):
    instance, submitter = router
    path = _copy(tmp_path, '"polars>=1.0"')

    result = await _run(instance, "dc deps sync", path)

    assert result["exit_code"] == 0
    assert "Installed 1 package(s)." in result["stdout"]
    assert submitter.calls == ["deps.sync"]
    manifest = json.loads(
        (Path(path) / ".datacode" / "deps" / "manifest.json").read_text()
    )
    # Attribution: a surprising tree must be traceable to who asked for it.
    assert manifest["source"] == "cli"
    assert manifest["actor_user_id"] == "u1"
    assert manifest["platform_provided"] == [
        {"name": "dagster", "version": "1.13.21"}
    ]


@pytest.mark.asyncio
async def test_deps_sync_does_not_reload_the_code_location(router, tmp_path):
    """Installing and making it live for Dagster are two intents.

    An agent that just edited the declaration wants the packages importable in its
    next command; restarting the code location is what Reload is for.
    """
    import ast

    from src.tools import deps_commands

    tree = ast.parse(Path(deps_commands.__file__).read_text(encoding="utf-8"))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else
        node.func.id if isinstance(node.func, ast.Name) else ""
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not (called & {"reload", "start_reload_job", "reload_deployment"})


@pytest.mark.asyncio
async def test_a_failed_sync_exits_non_zero_and_points_at_the_archive(
    router, tmp_path
):
    instance, submitter = router
    submitter.result = TaskResult(
        task_id="t1",
        status=TaskStatus.FAILED,
        error_code="deps_install_failed",
        error_message="No matching distribution found for polars>=99",
    )
    path = _copy(tmp_path, '"polars>=99"')

    result = await _run(instance, "dc deps sync", path)

    assert result["exit_code"] != 0
    assert "No matching distribution" in result["stderr"]
    assert "logs/errors/" in result["stderr"]
    records = list((Path(path) / "logs" / "errors").glob("*.json"))
    assert records, "the failure must be archived where the agent looks"


@pytest.mark.asyncio
async def test_an_unknown_subcommand_shows_the_usage(router, tmp_path):
    instance, _ = router
    path = _copy(tmp_path, "")

    result = await _run(instance, "dc deps upgrade", path)

    assert result["exit_code"] == 1
    assert "Usage: dc deps" in result["stderr"]
