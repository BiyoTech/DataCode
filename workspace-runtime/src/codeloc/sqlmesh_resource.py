"""Dagster resource that executes SQLMesh operations via workspace-runtime.

This ``ConfigurableResource`` is imported by user code inside a Dagster code
location (``asset-dag/definitions.py``). It lets a Dagster asset/op trigger a
SQLMesh ``plan``/``apply`` by calling the runtime's REST API, so the SQL
warehouse and the Data+AI pipeline can be orchestrated together.

It intentionally holds no SQLMesh state itself — the runtime owns the Context
Pool. This keeps the code location process lightweight and avoids loading a
second SQLMesh engine.

The upstream address is injected by the code-location launcher as
``WORKSPACE_RUNTIME_URL`` (port 8002).
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from dagster import ConfigurableResource

# Fallback upstream when the launcher injected nothing — this runtime itself.
_DEFAULT_RUNTIME_URL = "http://localhost:8002"

# Deployment identity injected by the runtime when it launches the code
# location. Read at import time: the value is fixed for the life of the process.
_INJECTED_ENVIRONMENT = os.environ.get("DATACODE_ENVIRONMENT", "")


class SQLMeshResource(ConfigurableResource):
    """Trigger SQLMesh plan/apply from within a Dagster run.

    Attributes:
        sqlmesh_service_url: Base URL of the workspace-runtime REST API.
        workspace_path: Working copy of ONE environment; the runtime resolves
            the ``sql-warehouse/`` sub-directory internally.
        environment: SQLMesh environment the operations target. Defaults to the
            deployment's own environment, never to ``prod``: a branch deployment
            loads the same code as production, so a hard-coded default would let
            a feature branch's run write production data.
        gateway: Optional SQLMesh gateway name.
        request_timeout: Per-request timeout in seconds.
    """

    sqlmesh_service_url: str = _DEFAULT_RUNTIME_URL
    workspace_path: str = ""
    environment: str = _INJECTED_ENVIRONMENT
    gateway: str = ""
    request_timeout: float = 600.0

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.sqlmesh_service_url.rstrip('/')}{path}"
        with httpx.Client(timeout=self.request_timeout) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()

    def _payload(self, environment: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Build a request body, refusing to guess the target environment.

        An undetermined environment is an error rather than a fallback: the only
        safe default would be the deployment's own environment, and if that was
        not injected the process does not know where it is running.
        """
        target = environment or self.environment
        if not target:
            raise ValueError(
                "SQLMesh environment is undetermined: set the resource's "
                "`environment`, or run inside a deployment that injects "
                "DATACODE_ENVIRONMENT"
            )
        return {
            "workspace_path": self.workspace_path,
            "gateway": self.gateway,
            "environment": target,
            **kwargs,
        }

    def plan(self, environment: str = "", **kwargs: Any) -> dict[str, Any]:
        """Preview a SQLMesh plan for the configured workspace/environment."""
        return self._post("/api/v1/plan", self._payload(environment, kwargs))

    def apply(self, environment: str = "", **kwargs: Any) -> dict[str, Any]:
        """Apply a SQLMesh plan for the configured workspace/environment."""
        return self._post("/api/v1/apply", self._payload(environment, kwargs))


def make_sqlmesh_resource(
    workspace_path: str,
    sqlmesh_service_url: str = _DEFAULT_RUNTIME_URL,
    gateway: str = "",
    request_timeout: Optional[float] = None,
    environment: str = "",
) -> SQLMeshResource:
    """Factory used by generated code location templates."""
    return SQLMeshResource(
        sqlmesh_service_url=sqlmesh_service_url,
        workspace_path=workspace_path,
        environment=environment or _INJECTED_ENVIRONMENT,
        gateway=gateway,
        request_timeout=request_timeout or 600.0,
    )
