"""GraphQL client for the Dagster webserver (unified local/remote).

The client talks to a dagster-webserver GraphQL endpoint. The endpoint differs
per mode but the API surface is identical, so callers (REST layer, run_manager)
are mode-agnostic:

- local  -> ``settings.dagster_graphql_url`` (subprocess webserver)
- remote -> ``settings.dagster_remote_graphql_url`` (remote cluster)

All queries go through :meth:`DagsterGraphQLClient.execute`, which normalizes
transport errors into ``DagsterUnavailableError`` and GraphQL-level errors into
``GraphQLError`` so the REST layer can map them to stable HTTP responses.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from src.config import Settings, settings
from src.utils.errors import DagsterUnavailableError, GraphQLError, RunNotFoundError
from src.utils.logging import get_logger

logger = get_logger(__name__)


# --- GraphQL documents -------------------------------------------------------

_LIST_ASSETS_QUERY = """
query AssetNodes {
  assetNodes {
    id
    assetKey { path }
    groupName
    description
    dependencyKeys { path }
    dependedByKeys { path }
  }
}
"""

# Asset nodes scoped to a single repository (code location). ``assetNodes`` at
# the query root returns the WHOLE instance graph and cannot distinguish
# workspaces, so per-workspace views must go through the repository, which owns
# only its own asset definitions.
_ASSETS_BY_REPO_QUERY = """
query RepositoryAssetNodes($repositorySelector: RepositorySelector!) {
  repositoryOrError(repositorySelector: $repositorySelector) {
    __typename
    ... on Repository {
      assetNodes {
        id
        assetKey { path }
        groupName
        description
        dependencyKeys { path }
        dependedByKeys { path }
      }
    }
    ... on RepositoryNotFoundError { message }
    ... on PythonError { message }
  }
}
"""

_LAUNCH_RUN_MUTATION = """
mutation LaunchRun($executionParams: ExecutionParams!) {
  launchPipelineExecution(executionParams: $executionParams) {
    __typename
    ... on LaunchRunSuccess { run { runId status } }
    ... on PythonError { message stack }
    ... on PipelineNotFoundError { message }
    ... on RunConfigValidationInvalid { errors { message } }
  }
}
"""

_RUN_STATUS_QUERY = """
query RunStatus($runId: ID!) {
  runOrError(runId: $runId) {
    __typename
    ... on Run {
      runId
      status
      startTime
      endTime
      jobName
    }
    ... on RunNotFoundError { message }
    ... on PythonError { message }
  }
}
"""

_RUN_LOGS_QUERY = """
query RunLogs($runId: ID!) {
  logsForRun(runId: $runId) {
    __typename
    ... on EventConnection {
      events {
        __typename
        ... on MessageEvent { message timestamp level stepKey }
      }
    }
    ... on RunNotFoundError { message }
    ... on PythonError { message }
  }
}
"""

_TERMINATE_RUN_MUTATION = """
mutation TerminateRun($runId: String!) {
  terminateRun(runId: $runId) {
    __typename
    ... on TerminateRunSuccess { run { runId status } }
    ... on TerminateRunFailure { message }
    ... on RunNotFoundError { message }
    ... on PythonError { message }
  }
}
"""

_LIST_SCHEDULES_QUERY = """
query Schedules($repositorySelector: RepositorySelector!) {
  schedulesOrError(repositorySelector: $repositorySelector) {
    __typename
    ... on Schedules {
      results {
        name
        cronSchedule
        pipelineName
        scheduleState { status }
      }
    }
    ... on PythonError { message }
  }
}
"""

_RELOAD_LOCATION_MUTATION = """
mutation ReloadLocation($location: String!) {
  reloadRepositoryLocation(repositoryLocationName: $location) {
    __typename
    ... on WorkspaceLocationEntry { name loadStatus }
    ... on ReloadNotSupported { message }
    ... on RepositoryLocationNotFound { message }
    ... on PythonError { message }
  }
}
"""

# Re-reads DAGSTER_HOME/workspace.yaml and (re)loads ALL locations it lists.
# Unlike reloadRepositoryLocation (which only refreshes a location the
# webserver already knows about), this is required to make the webserver
# aware of a NEWLY added code location entry.
_RELOAD_WORKSPACE_MUTATION = """
mutation ReloadWorkspace {
  reloadWorkspace {
    __typename
    ... on Workspace {
      locationEntries {
        name
        loadStatus
        locationOrLoadError {
          __typename
          ... on PythonError { message }
        }
      }
    }
    ... on PythonError { message }
  }
}
"""


# Bounded retry for the "repository not visible yet" window: after a code
# location reload the webserver refreshes its repository view asynchronously,
# so a scoped asset query may briefly resolve to RepositoryNotFoundError even
# though the gRPC server is already up.
_REPO_QUERY_MAX_ATTEMPTS = 5
_REPO_QUERY_RETRY_DELAY = 0.4  # seconds between attempts


class DagsterGraphQLClient:
    """Thin async GraphQL client over the Dagster webserver."""

    def __init__(self, cfg: Optional[Settings] = None):
        self._settings = cfg or settings

    @property
    def endpoint(self) -> str:
        return self._settings.active_graphql_url

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        # Remote mode may require a bearer token (env-only, never hard-coded).
        if self._settings.is_remote and self._settings.dagster_remote_token:
            headers["Authorization"] = f"Bearer {self._settings.dagster_remote_token}"
        return headers

    async def execute(
        self, query: str, variables: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """Execute a GraphQL document and return the ``data`` payload.

        Raises:
            DagsterUnavailableError: transport failure (webserver unreachable).
            GraphQLError: the response carried top-level ``errors``.
        """
        endpoint = self.endpoint
        if not endpoint:
            raise DagsterUnavailableError(
                endpoint="<unset>",
                detail="No GraphQL URL configured for the active Dagster mode",
            )

        payload = {"query": query, "variables": variables or {}}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(endpoint, json=payload, headers=self._headers())
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as e:
            logger.error("dagster_graphql_transport_error", endpoint=endpoint, error=str(e))
            raise DagsterUnavailableError(endpoint=endpoint, detail=str(e)) from e

        if body.get("errors"):
            messages = "; ".join(err.get("message", "") for err in body["errors"])
            logger.error("dagster_graphql_query_error", endpoint=endpoint, errors=messages)
            raise GraphQLError("Dagster GraphQL query failed", detail=messages)

        return body.get("data", {})

    # --- High-level operations ----------------------------------------------

    async def list_assets(
        self,
        repository_location_name: Optional[str] = None,
        repository_name: str = "__repository__",
    ) -> list[dict[str, Any]]:
        """Return asset nodes with their dependency keys (for DAG rendering).

        When ``repository_location_name`` is given, the result is scoped to that
        code location's repository so each workspace sees ONLY its own assets.
        Without it, the whole-instance asset graph is returned (legacy behavior).

        A location that is still (re)loading makes the webserver report a
        transient ``RepositoryNotFoundError``/``PythonError``; even after the
        code location's gRPC port is up, the webserver refreshes its repository
        view asynchronously, so we retry that specific window a few times before
        degrading to "no assets yet" (empty list). This keeps the endpoint
        stable during reload windows instead of surfacing a hard error.
        """
        if not repository_location_name:
            data = await self.execute(_LIST_ASSETS_QUERY)
            return data.get("assetNodes", [])

        variables = {
            "repositorySelector": {
                "repositoryLocationName": repository_location_name,
                "repositoryName": repository_name,
            }
        }
        # Retry only the "not yet visible" window; other errors are returned
        # immediately (already soft-failed to []).
        for attempt in range(_REPO_QUERY_MAX_ATTEMPTS):
            try:
                data = await self.execute(_ASSETS_BY_REPO_QUERY, variables)
            except (DagsterUnavailableError, GraphQLError) as e:
                logger.warning(
                    "list_assets_transient_error",
                    location=repository_location_name,
                    repository=repository_name,
                    detail=str(e),
                )
                return []
            result = data.get("repositoryOrError", {})
            typename = result.get("__typename")
            if typename == "Repository":
                return result.get("assetNodes", [])
            # Repository not visible yet: the webserver is still refreshing its
            # view after a reload. Back off briefly and retry within the window.
            if attempt < _REPO_QUERY_MAX_ATTEMPTS - 1:
                await asyncio.sleep(_REPO_QUERY_RETRY_DELAY)
                continue
            logger.warning(
                "list_assets_repository_unavailable",
                location=repository_location_name,
                repository=repository_name,
                typename=typename,
                detail=result.get("message"),
            )
        return []

    async def launch_run(
        self,
        *,
        repository_location_name: str,
        repository_name: str,
        job_name: str,
        run_config: Optional[dict[str, Any]] = None,
        tags: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Launch a job run. Returns ``{run_id, status}`` on success.

        ``tags`` become Dagster run tags through ``executionMetadata``: this is
        how a run stays attributable to its deployment in an instance whose run
        storage is shared by every environment.
        """
        variables: dict[str, Any] = {
            "executionParams": {
                "selector": {
                    "repositoryLocationName": repository_location_name,
                    "repositoryName": repository_name,
                    "jobName": job_name,
                },
                "runConfigData": run_config or {},
            }
        }
        if tags:
            variables["executionParams"]["executionMetadata"] = {
                "tags": [{"key": key, "value": value} for key, value in tags.items()]
            }
        data = await self.execute(_LAUNCH_RUN_MUTATION, variables)
        result = data.get("launchPipelineExecution", {})
        typename = result.get("__typename")
        if typename == "LaunchRunSuccess":
            run = result["run"]
            return {"run_id": run["runId"], "status": run["status"]}
        raise GraphQLError(
            "Failed to launch run",
            detail=result.get("message") or str(result),
        )

    async def get_run_status(self, run_id: str) -> dict[str, Any]:
        """Return run status metadata. Raises RunNotFoundError if unknown."""
        data = await self.execute(_RUN_STATUS_QUERY, {"runId": run_id})
        result = data.get("runOrError", {})
        typename = result.get("__typename")
        if typename == "Run":
            return {
                "run_id": result["runId"],
                "status": result["status"],
                "start_time": result.get("startTime"),
                "end_time": result.get("endTime"),
                "job_name": result.get("jobName"),
            }
        if typename == "RunNotFoundError":
            raise RunNotFoundError(run_id)
        raise GraphQLError("Failed to get run status", detail=result.get("message"))

    async def get_run_logs(self, run_id: str) -> list[dict[str, Any]]:
        """Return structured log events for a run."""
        data = await self.execute(_RUN_LOGS_QUERY, {"runId": run_id})
        result = data.get("logsForRun", {})
        typename = result.get("__typename")
        if typename == "EventConnection":
            return result.get("events", [])
        if typename == "RunNotFoundError":
            raise RunNotFoundError(run_id)
        raise GraphQLError("Failed to get run logs", detail=result.get("message"))

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Terminate a run. Returns ``{run_id, status}`` on success."""
        data = await self.execute(_TERMINATE_RUN_MUTATION, {"runId": run_id})
        result = data.get("terminateRun", {})
        typename = result.get("__typename")
        if typename == "TerminateRunSuccess":
            run = result["run"]
            return {"run_id": run["runId"], "status": run["status"]}
        if typename == "RunNotFoundError":
            raise RunNotFoundError(run_id)
        raise GraphQLError("Failed to cancel run", detail=result.get("message"))

    async def list_schedules(
        self, repository_location_name: str, repository_name: str
    ) -> list[dict[str, Any]]:
        """List schedules for a repository.

        Returns an empty list when the code location has no schedules or is
        still loading (webserver reports a transient ``PythonError`` while the
        gRPC server re-imports user code). This keeps the endpoint stable
        during reload windows instead of surfacing a hard error.
        """
        variables = {
            "repositorySelector": {
                "repositoryLocationName": repository_location_name,
                "repositoryName": repository_name,
            }
        }
        try:
            data = await self.execute(_LIST_SCHEDULES_QUERY, variables)
        except (DagsterUnavailableError, GraphQLError) as e:
            # Webserver may return 5xx while the location's gRPC server is
            # (re)loading user code. Treat as "no schedules yet".
            logger.warning(
                "list_schedules_transient_error",
                location=repository_location_name,
                repository=repository_name,
                detail=str(e),
            )
            return []
        result = data.get("schedulesOrError", {})
        typename = result.get("__typename")
        if typename == "Schedules":
            return result.get("results", [])
        # Location not loaded yet / transient repo resolution error: soft-fail.
        logger.warning(
            "list_schedules_unavailable",
            location=repository_location_name,
            repository=repository_name,
            typename=typename,
            detail=result.get("message"),
        )
        return []

    async def reload_code_location(self, location_name: str) -> dict[str, Any]:
        """Make the webserver load/refresh a code location.

        Tries an in-place ``reloadRepositoryLocation`` first (cheap refresh of a
        location the webserver already tracks). If the webserver doesn't know
        the location yet — which is always the case right after a NEW entry is
        appended to ``workspace.yaml`` — it returns ``RepositoryLocationNotFound``.
        In that case we fall back to ``reloadWorkspace``, which re-reads
        ``workspace.yaml`` and picks up the newly added location.
        """
        data = await self.execute(_RELOAD_LOCATION_MUTATION, {"location": location_name})
        result = data.get("reloadRepositoryLocation", {})
        typename = result.get("__typename")
        if typename == "WorkspaceLocationEntry":
            return {"name": result["name"], "load_status": result.get("loadStatus")}
        if typename == "RepositoryLocationNotFound":
            # Newly added location: force the webserver to re-read workspace.yaml.
            logger.info("code_location_unknown_reloading_workspace", location=location_name)
            return await self.reload_workspace()
        raise GraphQLError("Failed to reload code location", detail=result.get("message"))

    async def reload_workspace(self) -> dict[str, Any]:
        """Re-read workspace.yaml and (re)load every code location it lists.

        Required to surface a newly added code location to the webserver, since
        it only reads workspace.yaml at startup and on this explicit reload.
        """
        data = await self.execute(_RELOAD_WORKSPACE_MUTATION)
        result = data.get("reloadWorkspace", {})
        typename = result.get("__typename")
        if typename == "Workspace":
            entries = result.get("locationEntries", [])
            for entry in entries:
                load_error = entry.get("locationOrLoadError") or {}
                if load_error.get("__typename") == "PythonError":
                    logger.warning(
                        "code_location_load_error",
                        location=entry.get("name"),
                        detail=load_error.get("message"),
                    )
            return {
                "locations": [
                    {"name": e.get("name"), "load_status": e.get("loadStatus")}
                    for e in entries
                ]
            }
        raise GraphQLError("Failed to reload workspace", detail=result.get("message"))


_client: Optional[DagsterGraphQLClient] = None


def get_dagster_client() -> DagsterGraphQLClient:
    """Return the process-wide GraphQL client singleton."""
    global _client
    if _client is None:
        _client = DagsterGraphQLClient()
    return _client
