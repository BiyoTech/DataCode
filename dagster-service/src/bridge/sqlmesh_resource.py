"""Dagster resource that executes SQLMesh operations via sqlmesh-service.

This ``ConfigurableResource`` is imported by user code inside a Dagster code
location (``asset-dag/definitions.py``). It lets a Dagster asset/op trigger a
SQLMesh ``plan``/``apply`` by calling the sqlmesh-service REST API, so the SQL
warehouse and the Data+AI pipeline can be orchestrated together.

It intentionally holds no SQLMesh state itself — sqlmesh-service owns the
Context Pool. This keeps the code location process lightweight and avoids
loading a second SQLMesh engine.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from dagster import ConfigurableResource


class SQLMeshResource(ConfigurableResource):
    """Trigger SQLMesh plan/apply from within a Dagster run.

    Attributes:
        sqlmesh_service_url: Base URL of the sqlmesh-service REST API.
        workspace_path: Workspace root; sqlmesh-service resolves the
            ``sql-warehouse/`` sub-directory internally.
        gateway: Optional SQLMesh gateway name.
        request_timeout: Per-request timeout in seconds.
    """

    sqlmesh_service_url: str = "http://localhost:8002"
    workspace_path: str = ""
    gateway: str = ""
    request_timeout: float = 600.0

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.sqlmesh_service_url.rstrip('/')}{path}"
        with httpx.Client(timeout=self.request_timeout) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()

    def plan(self, environment: str = "prod", **kwargs: Any) -> dict[str, Any]:
        """Preview a SQLMesh plan for the configured workspace/environment."""
        payload = {
            "workspace_path": self.workspace_path,
            "gateway": self.gateway,
            "environment": environment,
            **kwargs,
        }
        return self._post("/api/v1/plan", payload)

    def apply(self, environment: str = "prod", **kwargs: Any) -> dict[str, Any]:
        """Apply a SQLMesh plan for the configured workspace/environment."""
        payload = {
            "workspace_path": self.workspace_path,
            "gateway": self.gateway,
            "environment": environment,
            **kwargs,
        }
        return self._post("/api/v1/apply", payload)


def make_sqlmesh_resource(
    workspace_path: str,
    sqlmesh_service_url: str = "http://localhost:8002",
    gateway: str = "",
    request_timeout: Optional[float] = None,
) -> SQLMeshResource:
    """Factory used by generated code location templates."""
    return SQLMeshResource(
        sqlmesh_service_url=sqlmesh_service_url,
        workspace_path=workspace_path,
        gateway=gateway,
        request_timeout=request_timeout or 600.0,
    )
