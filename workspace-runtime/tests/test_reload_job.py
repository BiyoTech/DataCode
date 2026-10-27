"""The reload job: what the endpoints promise, and what a failure looks like.

Asserted through the REAL FastAPI routes and the REAL job store, because the
properties under test are decisions those layers make:

* a reload is ACCEPTED (202) and never held open for the length of an install;
* a second trigger joins the running job instead of starting a competing one;
* the status endpoint refuses to answer without a ``workspace_path`` — the
  runtime is a headless Service and only the replica holding that copy's lease
  knows the job, so an unrouted query would be answered by a random replica;
* a dependency install failure FAILS the reload. Reporting an unfinished reload
  as successful is the single outcome this machinery exists to prevent;
* every failure leaves a structured record under ``{workspace}/logs/errors/`` so
  the agent can diagnose it from the filesystem.

One stub, at the narrowest seam: the code-location manager (a real one needs a
Dagster gRPC server). The dependency service is REAL — what is replaced is its
task submitter, one layer further down.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from datacode_common.task_protocol import TaskResult, TaskStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeRuntime:
    def __init__(self, alive: bool) -> None:
        self.host = "127.0.0.1"
        self.port = 4001
        self.definitions = "/copy/asset-dag/definitions.py"
        self.alive = alive


class _FakeCodeLocations:
    """Stands in for the gRPC code-location vehicle."""

    def __init__(self, *, alive: bool = True) -> None:
        self.alive = alive
        self.reloads: list[str] = []
        self.failure: dict = {}

    async def reload(self, spec):
        self.reloads.append(spec.location_name)
        return _FakeRuntime(self.alive)

    async def ensure(self, spec):
        return _FakeRuntime(self.alive)

    def load_failure(self, location_name: str) -> dict:
        return dict(self.failure)

    def get(self, name):
        return None

    def port_of(self, name):
        return 0


class _Submitter:
    """Sandbox stand-in: reports a result without running a container."""

    def __init__(self, *, result=None, error: str = "") -> None:
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    async def submit(self, kind, workspace_path, **kwargs):
        from src.tasks.submitter import TaskSubmissionError

        self.calls.append({"kind": kind, "workspace_path": workspace_path, **kwargs})
        if self.error:
            raise TaskSubmissionError(self.error)
        return self.result


def _installed_result() -> TaskResult:
    """What the runner reports after a successful install.

    The interpreter fields are THIS interpreter's on purpose: a manifest built
    for another Python or platform is deliberately treated as unusable, so
    hard-coding them would make every "already installed" assertion below test
    the incompatibility path instead.
    """
    from datacode_common.python_deps import (
        interpreter_platform_tag,
        interpreter_python_version,
    )

    return TaskResult(
        task_id="t1",
        status=TaskStatus.SUCCEEDED,
        result={
            "packages": [{"name": "polars", "version": "1.2.0"}],
            "platform_provided": [],
            "shadowed": [],
            "python_version": interpreter_python_version(),
            "platform_tag": interpreter_platform_tag(),
            "size_bytes": 4096,
            "output_tail": "Successfully installed polars-1.2.0",
        },
    )


def _copy(tmp_path: Path, *, requirements: str = "") -> str:
    """Create a five-segment working copy with an asset-dag declaration."""
    copy = tmp_path / "base" / "t1" / "ws-t1-shop" / "u1" / "envs" / "dev"
    (copy / "asset-dag").mkdir(parents=True)
    (copy / "asset-dag" / "pyproject.toml").write_text(
        f'[project]\nname = "assets"\ndependencies = [{requirements}]\n',
        encoding="utf-8",
    )
    return str(copy)


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """Real routes + real job store + real deps service, stubbed vehicles."""
    from src.api.dagster import deployments as api
    from src.codeloc import deployments as domain
    from src.codeloc import reload_jobs
    from src.config import settings
    from src.deps import service as deps_service

    monkeypatch.setattr(
        "src.api.deps.validate_workspace_path", lambda p: p, raising=False
    )
    # A fresh store per test: the "one running job per copy" slot is process
    # state, and a leaked slot would make the attach assertion pass for the
    # wrong reason.
    monkeypatch.setattr(reload_jobs, "_store", None)
    monkeypatch.setattr(settings, "workspace_base_path", str(tmp_path / "base"))
    monkeypatch.setattr(settings, "deps_index_url", "https://index.invalid/simple")

    locations = _FakeCodeLocations()
    manager = domain.DeploymentManager(code_locations=locations)
    monkeypatch.setattr(domain, "_deployment_manager", manager)

    submitter = _Submitter(result=_installed_result())
    monkeypatch.setattr(
        deps_service, "_service", deps_service.DepsService(submitter=submitter)
    )

    app = FastAPI()
    # The production handler, so a domain error is asserted as the STATUS the
    # client sees rather than as an exception escaping the route.
    from src.main import _register_exception_handlers

    _register_exception_handlers(app)
    app.include_router(api.router)
    # As a context manager on purpose: the reload runs as a detached task, and
    # outside this block TestClient tears its event loop down after every single
    # request — which would cancel the very job under test.
    with TestClient(app) as client:
        yield {
            "client": client,
            "locations": locations,
            "submitter": submitter,
        }


# Identity headers the gateway injects. Present on every reload request because
# the manifest records WHO triggered an install, and a request without a verified
# identity is refused before it reaches the handler.
_CALLER = {
    "X-User-ID": "u1",
    "X-Tenant-ID": "t1",
    "X-Workspace-ID": "ws-t1-shop",
    "X-Granted-Permissions": "workspace:pipeline:run",
}


def _post(harness, path: str):
    return harness["client"].post(
        "/api/v1/dagster/deployments/reload",
        json={"workspace_path": path},
        headers=_CALLER,
    )


def _start(harness, path: str) -> dict:
    return _post(harness, path).json()


async def _drain(client, workspace_path: str, *, timeout: float = 5.0) -> dict:
    """Poll the status endpoint until the job is terminal."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        body = client.get(
            "/api/v1/dagster/deployments/reload/status",
            params={"workspace_path": workspace_path},
        ).json()
        if body.get("state") != "running":
            return body
        await asyncio.sleep(0.02)
    raise AssertionError("the reload job never reached a terminal state")


# ── acceptance and progress ───────────────────────────────────────────────


def test_reload_is_accepted_not_awaited(harness, tmp_path):
    """202 with an envelope, never a request held open for the install."""
    path = _copy(tmp_path, requirements='"polars>=1.0"')

    response = _post(harness, path)

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"]
    assert body["state"] == "running"
    assert body["attached"] is False
    # The ladder travels with the envelope so the UI needs no copy of it.
    assert body["stages"][0] == "queued"
    assert body["stages"][-1] == "ready"


def test_a_second_trigger_attaches_to_the_same_job(harness, tmp_path):
    """Three buttons and a CLI express one intent, so they share one job."""
    path = _copy(tmp_path, requirements='"polars>=1.0"')

    first = _start(harness, path)
    second = _start(harness, path)

    assert second["job_id"] == first["job_id"]
    assert second["attached"] is True


@pytest.mark.asyncio
async def test_a_successful_reload_reaches_ready_with_the_deployment(
    harness, tmp_path
):
    path = _copy(tmp_path, requirements='"polars>=1.0"')

    _start(harness, path)
    final = await _drain(harness["client"], path)

    assert final["state"] == "succeeded"
    assert final["stage"] == "ready"
    assert final["deployment"]["deployment_name"] == "ws_t1_shop__dev"
    assert final["failure_code"] == ""
    assert harness["submitter"].calls[0]["kind"] == "deps.sync"
    manifest = json.loads(
        (Path(path) / ".datacode" / "deps" / "manifest.json").read_text()
    )
    assert manifest["state"] == "ready"
    assert manifest["packages"] == [{"name": "polars", "version": "1.2.0"}]


@pytest.mark.asyncio
async def test_an_empty_declaration_skips_the_sandbox_entirely(harness, tmp_path):
    """Nothing declared must cost nothing: no task, no vehicle, no wait."""
    path = _copy(tmp_path)

    _start(harness, path)
    final = await _drain(harness["client"], path)

    assert final["state"] == "succeeded"
    assert harness["submitter"].calls == []


@pytest.mark.asyncio
async def test_an_unchanged_declaration_does_not_reinstall(harness, tmp_path):
    """The fingerprint fast path: a reload after a reload installs nothing."""
    path = _copy(tmp_path, requirements='"polars>=1.0"')
    # The overlay directory must exist for the manifest to count as usable.
    (Path(path) / ".datacode" / "deps" / "site-packages").mkdir(parents=True)

    _start(harness, path)
    await _drain(harness["client"], path)
    from src.codeloc import reload_jobs

    monkey_store = reload_jobs.get_reload_job_store()
    monkey_store._active.clear()

    _start(harness, path)
    final = await _drain(harness["client"], path)

    assert final["state"] == "succeeded"
    # One install for the first reload, none for the second.
    assert len(harness["submitter"].calls) == 1


# ── failures ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_install_fails_the_reload(harness, tmp_path):
    """The property the whole design exists for.

    A code location started without the packages its assets import raises a
    definitions error nobody can act on, so "the install failed" must never be
    reported as "the reload succeeded".
    """
    path = _copy(tmp_path, requirements='"polars>=1.0"')
    harness["submitter"].result = TaskResult(
        task_id="t1",
        status=TaskStatus.FAILED,
        error_code="deps_install_failed",
        error_message="no version matching the declaration exists on the index",
    )

    _start(harness, path)
    final = await _drain(harness["client"], path)

    assert final["state"] == "failed"
    assert final["failure_code"] == "deps_install_failed"
    assert "no version matching" in final["message"]
    # The code location was never touched: the gate held.
    assert harness["locations"].reloads == []
    manifest = json.loads(
        (Path(path) / ".datacode" / "deps" / "manifest.json").read_text()
    )
    assert manifest["state"] == "failed"


@pytest.mark.asyncio
async def test_a_transitive_conflict_reaches_the_envelope_with_its_rewrite(
    harness, tmp_path
):
    """The failure shape no declaration check can see, end to end.

    `feast<0.35` is compatible with the baseline; its dependency `numpy<1.25`
    is not. Nothing in the file mentions numpy, so this is the one conflict the
    read path cannot report — and therefore the one the failed reload must
    carry as structure, since prose is all a user could otherwise get.
    """
    path = _copy(tmp_path, requirements='"feast[postgres]<0.35.0,>=0.34.0"')
    harness["submitter"].result = TaskResult(
        task_id="t1",
        status=TaskStatus.FAILED,
        error_code="deps_platform_transitive",
        error_message="'feast[postgres]<0.35.0,>=0.34.0' cannot be installed: it "
        "needs numpy 1.24.3, while the platform provides numpy==2.5.2.",
        # The runner's diagnosis: a failure carries it in its RESULT, not in
        # its message, so the UI can offer the edit instead of parsing prose.
        result={
            "transitive_conflicts": [
                {
                    "requirement": "feast[postgres]<0.35.0,>=0.34.0",
                    "name": "feast",
                    "blockers": [
                        {
                            "name": "numpy",
                            "required": "1.24.3",
                            "platform_version": "2.5.2",
                        }
                    ],
                    "suggested": "feast[postgres]>=0.34.0",
                    "resolved_version": "0.40.1",
                }
            ]
        },
    )

    _start(harness, path)
    final = await _drain(harness["client"], path)

    assert final["state"] == "failed"
    assert final["failure_code"] == "deps_platform_transitive"
    conflict = final["transitive_conflicts"][0]
    # The line to edit is the DECLARED package, not the blocking one.
    assert conflict["name"] == "feast"
    assert conflict["suggested"] == "feast[postgres]>=0.34.0"
    assert conflict["resolved_version"] == "0.40.1"
    assert conflict["blockers"][0]["name"] == "numpy"
    # Archived too, so the agent can act on it from the filesystem.
    records = list((Path(path) / "logs" / "errors").glob("*.json"))
    payload = json.loads(records[0].read_text())
    assert payload["context"]["transitive_conflicts"][0]["suggested"] == (
        "feast[postgres]>=0.34.0"
    )


@pytest.mark.asyncio
async def test_a_failed_install_is_archived_under_logs_errors(harness, tmp_path):
    """The agent diagnoses this from the filesystem, with no API call."""
    path = _copy(tmp_path, requirements='"polars>=1.0"')
    harness["submitter"].result = TaskResult(
        task_id="t1",
        status=TaskStatus.FAILED,
        error_code="deps_install_failed",
        error_message="ResolutionImpossible: polars 1.2 conflicts with pyarrow 12",
    )

    _start(harness, path)
    final = await _drain(harness["client"], path)

    records = list((Path(path) / "logs" / "errors").glob("*.json"))
    assert records, "a failed install must leave a record under logs/errors/"
    payload = json.loads(records[0].read_text())
    assert payload["error_code"] == "DEPS_INSTALL_FAILED"
    assert payload["operation"] == "deps.sync"
    assert "ResolutionImpossible" in payload["detail"]
    assert payload["context"]["failure_code"] == "deps_install_failed"
    # The envelope points at the record, so the UI can name the file too.
    assert final["error_log_id"] == payload["error_id"]


@pytest.mark.asyncio
async def test_a_location_that_never_serves_fails_with_its_traceback(
    harness, tmp_path
):
    """``alive=false`` with a 200 used to be the answer; it explained nothing."""
    path = _copy(tmp_path)
    harness["locations"].alive = False
    harness["locations"].failure = {
        "exit_code": 1,
        "output_tail": "ModuleNotFoundError: No module named 'polars'",
    }

    _start(harness, path)
    final = await _drain(harness["client"], path)

    assert final["state"] == "failed"
    assert final["failure_code"] == "code_load_failed"
    assert "ModuleNotFoundError" in final["output_tail"]
    records = list((Path(path) / "logs" / "errors").glob("*.json"))
    payload = json.loads(records[0].read_text())
    assert payload["error_code"] == "CODE_LOCATION_LOAD_FAILED"
    assert payload["operation"] == "deployment_reload"


@pytest.mark.asyncio
async def test_a_sandbox_that_cannot_start_is_not_reported_as_a_bad_declaration(
    harness, tmp_path
):
    """Nothing ran, so the user must not be sent to inspect their own file."""
    path = _copy(tmp_path, requirements='"polars>=1.0"')
    harness["submitter"].error = "the task execution plane is disabled"

    _start(harness, path)
    final = await _drain(harness["client"], path)

    assert final["state"] == "failed"
    assert final["failure_code"] == "sandbox_unavailable"
    assert "execution plane is disabled" in final["message"]


@pytest.mark.asyncio
async def test_an_index_that_is_not_configured_is_refused_before_submitting(
    harness, tmp_path, monkeypatch
):
    from src.config import settings

    monkeypatch.setattr(settings, "deps_index_url", "")
    path = _copy(tmp_path, requirements='"polars>=1.0"')

    _start(harness, path)
    final = await _drain(harness["client"], path)

    assert final["failure_code"] == "deps_index_not_configured"
    assert harness["submitter"].calls == []


@pytest.mark.asyncio
async def test_an_index_url_with_credentials_is_refused(
    harness, tmp_path, monkeypatch
):
    """The spec reaches audit records and a ConfigMap; a token must not."""
    from src.config import settings

    monkeypatch.setattr(
        settings, "deps_index_url", "https://user:tok@index.invalid/simple"
    )
    path = _copy(tmp_path, requirements='"polars>=1.0"')

    _start(harness, path)
    final = await _drain(harness["client"], path)

    assert final["failure_code"] == "deps_index_not_configured"
    assert "credentials" in final["message"]
    assert harness["submitter"].calls == []


@pytest.mark.asyncio
async def test_a_malformed_declaration_names_the_file(harness, tmp_path):
    path = _copy(tmp_path)
    (Path(path) / "asset-dag" / "pyproject.toml").write_text(
        '[project]\ndependencies = ["not a requirement=="]\n', encoding="utf-8"
    )

    _start(harness, path)
    final = await _drain(harness["client"], path)

    assert final["failure_code"] == "deps_declaration_invalid"
    assert "pyproject.toml" in final["message"]
    assert harness["locations"].reloads == []


# ── status endpoint contract ──────────────────────────────────────────────


def test_status_requires_a_workspace_path(harness):
    """Without it the gateway cannot route to the replica holding the lease."""
    response = harness["client"].get("/api/v1/dagster/deployments/reload/status")
    assert response.status_code == 422


def test_status_for_a_copy_that_was_never_reloaded_is_404(harness, tmp_path):
    """"Never reloaded" and "reloaded, here is the result" are different answers."""
    path = _copy(tmp_path)
    response = harness["client"].get(
        "/api/v1/dagster/deployments/reload/status",
        params={"workspace_path": path},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_terminal_state_survives_losing_the_in_memory_store(
    harness, tmp_path, monkeypatch
):
    """A page refresh after a restart must still show why the reload failed."""
    path = _copy(tmp_path, requirements='"polars>=1.0"')
    harness["submitter"].result = TaskResult(
        task_id="t1",
        status=TaskStatus.FAILED,
        error_code="deps_install_failed",
        error_message="ResolutionImpossible",
    )
    _start(harness, path)
    before = await _drain(harness["client"], path)

    # Simulate a restart: everything the process remembered is gone, and only
    # the record inside the working copy remains.
    from src.codeloc import reload_jobs

    monkeypatch.setattr(reload_jobs, "_store", None)

    after = harness["client"].get(
        "/api/v1/dagster/deployments/reload/status",
        params={"workspace_path": path},
    ).json()

    assert after["job_id"] == before["job_id"]
    assert after["state"] == "failed"
    assert after["failure_code"] == "deps_install_failed"
