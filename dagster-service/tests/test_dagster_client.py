"""Tests for the unified (local/remote) Dagster GraphQL client.

High-level operations are tested by stubbing ``execute`` so parsing logic is
covered without a live webserver. Transport/endpoint behavior is tested against
the real ``execute`` with an unset endpoint (no network involved).
"""

import pytest

from src.config import Settings
from src.core.dagster_client import DagsterGraphQLClient
from src.utils.errors import DagsterUnavailableError, GraphQLError, RunNotFoundError


@pytest.fixture
def local_client():
    return DagsterGraphQLClient(Settings(dagster_mode="local"))


@pytest.fixture
def remote_client():
    return DagsterGraphQLClient(
        Settings(
            dagster_mode="remote",
            dagster_remote_graphql_url="http://remote:3000/graphql",
            dagster_remote_token="secret-token",
        )
    )


def _stub_execute(monkeypatch, client, payload):
    """Replace client.execute with an async stub returning ``payload``."""

    async def _fake(_query, _variables=None):
        return payload

    monkeypatch.setattr(client, "execute", _fake)


# --- Endpoint / headers ------------------------------------------------------


def test_endpoint_selects_local_url(local_client):
    assert local_client.endpoint == local_client._settings.dagster_graphql_url


def test_endpoint_selects_remote_url(remote_client):
    assert remote_client.endpoint == "http://remote:3000/graphql"


def test_remote_headers_include_bearer_token(remote_client):
    headers = remote_client._headers()
    assert headers["Authorization"] == "Bearer secret-token"


def test_local_headers_have_no_authorization(local_client):
    assert "Authorization" not in local_client._headers()


# --- execute() transport guard (no network) ----------------------------------


async def test_execute_raises_when_endpoint_unset():
    client = DagsterGraphQLClient(
        Settings(dagster_mode="remote", dagster_remote_graphql_url="")
    )
    with pytest.raises(DagsterUnavailableError):
        await client.execute("query { __typename }")


# --- High-level operations ---------------------------------------------------


async def test_list_assets_returns_asset_nodes(local_client, monkeypatch):
    _stub_execute(
        monkeypatch,
        local_client,
        {"assetNodes": [{"assetKey": {"path": ["orders"]}, "groupName": "marts"}]},
    )
    nodes = await local_client.list_assets()
    assert len(nodes) == 1
    assert nodes[0]["assetKey"]["path"] == ["orders"]


async def test_list_assets_scoped_to_repository(local_client, monkeypatch):
    # With a location name, the client must return only that repository's assets.
    _stub_execute(
        monkeypatch,
        local_client,
        {
            "repositoryOrError": {
                "__typename": "Repository",
                "assetNodes": [{"assetKey": {"path": ["brazil_sales"]}}],
            }
        },
    )
    nodes = await local_client.list_assets("ws_test111", "__repository__")
    assert [n["assetKey"]["path"] for n in nodes] == [["brazil_sales"]]


async def test_list_assets_soft_fails_when_repository_not_found(local_client, monkeypatch):
    # A still-loading / unknown location must degrade to an empty list after the
    # bounded retry window. Zero out the delay so the test stays fast.
    monkeypatch.setattr("src.core.dagster_client._REPO_QUERY_RETRY_DELAY", 0)
    _stub_execute(
        monkeypatch,
        local_client,
        {"repositoryOrError": {"__typename": "RepositoryNotFoundError", "message": "nope"}},
    )
    nodes = await local_client.list_assets("ws_missing", "__repository__")
    assert nodes == []


async def test_list_assets_retries_until_repository_visible(local_client, monkeypatch):
    # The webserver refreshes its repository view asynchronously after a reload:
    # the first calls resolve to RepositoryNotFoundError, then it becomes
    # visible. The client must retry within the window and return the assets.
    monkeypatch.setattr("src.core.dagster_client._REPO_QUERY_RETRY_DELAY", 0)
    calls = {"n": 0}

    async def _fake(_query, _variables=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"repositoryOrError": {"__typename": "RepositoryNotFoundError", "message": "x"}}
        return {
            "repositoryOrError": {
                "__typename": "Repository",
                "assetNodes": [{"assetKey": {"path": ["late"]}}],
            }
        }

    monkeypatch.setattr(local_client, "execute", _fake)
    nodes = await local_client.list_assets("ws_test111", "__repository__")
    assert [n["assetKey"]["path"] for n in nodes] == [["late"]]
    assert calls["n"] == 3


async def test_launch_run_success_returns_run_id(local_client, monkeypatch):
    _stub_execute(
        monkeypatch,
        local_client,
        {
            "launchPipelineExecution": {
                "__typename": "LaunchRunSuccess",
                "run": {"runId": "r-1", "status": "STARTED"},
            }
        },
    )
    result = await local_client.launch_run(
        repository_location_name="ws_demo",
        repository_name="__repository__",
        job_name="my_job",
    )
    assert result == {"run_id": "r-1", "status": "STARTED"}


async def test_launch_run_error_raises_graphql_error(local_client, monkeypatch):
    _stub_execute(
        monkeypatch,
        local_client,
        {
            "launchPipelineExecution": {
                "__typename": "PipelineNotFoundError",
                "message": "no such job",
            }
        },
    )
    with pytest.raises(GraphQLError):
        await local_client.launch_run(
            repository_location_name="ws_demo",
            repository_name="__repository__",
            job_name="ghost",
        )


async def test_launch_run_sends_deployment_tags_as_execution_metadata(
    local_client, monkeypatch
):
    """Run tags are the only thing separating environments in a shared run store."""
    captured: dict = {}

    async def _fake(_query, variables=None):
        captured.update(variables or {})
        return {
            "launchPipelineExecution": {
                "__typename": "LaunchRunSuccess",
                "run": {"runId": "r-2", "status": "STARTED"},
            }
        }

    monkeypatch.setattr(local_client, "execute", _fake)
    await local_client.launch_run(
        repository_location_name="ws_t1_sales__dev_alice",
        repository_name="__repository__",
        job_name="my_job",
        tags={"datacode/environment": "dev_alice"},
    )

    assert captured["executionParams"]["executionMetadata"]["tags"] == [
        {"key": "datacode/environment", "value": "dev_alice"}
    ]


async def test_launch_run_omits_execution_metadata_without_tags(local_client, monkeypatch):
    captured: dict = {}

    async def _fake(_query, variables=None):
        captured.update(variables or {})
        return {
            "launchPipelineExecution": {
                "__typename": "LaunchRunSuccess",
                "run": {"runId": "r-3", "status": "STARTED"},
            }
        }

    monkeypatch.setattr(local_client, "execute", _fake)
    await local_client.launch_run(
        repository_location_name="ws_demo",
        repository_name="__repository__",
        job_name="my_job",
    )
    assert "executionMetadata" not in captured["executionParams"]


async def test_get_run_status_parses_run(local_client, monkeypatch):
    _stub_execute(
        monkeypatch,
        local_client,
        {
            "runOrError": {
                "__typename": "Run",
                "runId": "r-2",
                "status": "SUCCESS",
                "startTime": 1000.0,
                "endTime": 1005.0,
                "jobName": "j",
            }
        },
    )
    status = await local_client.get_run_status("r-2")
    assert status["status"] == "SUCCESS"
    assert status["start_time"] == 1000.0
    assert status["end_time"] == 1005.0


async def test_get_run_status_not_found_raises(local_client, monkeypatch):
    _stub_execute(
        monkeypatch,
        local_client,
        {"runOrError": {"__typename": "RunNotFoundError", "message": "gone"}},
    )
    with pytest.raises(RunNotFoundError):
        await local_client.get_run_status("missing")


async def test_cancel_run_success(local_client, monkeypatch):
    _stub_execute(
        monkeypatch,
        local_client,
        {
            "terminateRun": {
                "__typename": "TerminateRunSuccess",
                "run": {"runId": "r-3", "status": "CANCELED"},
            }
        },
    )
    result = await local_client.cancel_run("r-3")
    assert result == {"run_id": "r-3", "status": "CANCELED"}


async def test_list_schedules_returns_results(local_client, monkeypatch):
    _stub_execute(
        monkeypatch,
        local_client,
        {
            "schedulesOrError": {
                "__typename": "Schedules",
                "results": [{"name": "daily", "cronSchedule": "0 0 * * *"}],
            }
        },
    )
    schedules = await local_client.list_schedules("ws_demo", "__repository__")
    assert schedules[0]["name"] == "daily"


async def test_reload_code_location_success(local_client, monkeypatch):
    _stub_execute(
        monkeypatch,
        local_client,
        {
            "reloadRepositoryLocation": {
                "__typename": "WorkspaceLocationEntry",
                "name": "ws_demo",
                "loadStatus": "LOADED",
            }
        },
    )
    result = await local_client.reload_code_location("ws_demo")
    assert result == {"name": "ws_demo", "load_status": "LOADED"}
