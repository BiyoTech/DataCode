"""Ontology service configuration.

Configuration is sourced from environment variables only (secrets/URLs are
never hard-coded). All settings have sensible local-development defaults.
"""

from __future__ import annotations

import os


class Settings:
    """Runtime settings for the ontology service.

    Attributes:
        sqlmesh_service_url: Base URL of the sqlmesh-service used to verify
            that ``Entity.model`` / ``Metric.maps_to`` reference real SQLMesh
            objects. The ontology service stays lightweight by delegating all
            SQLMesh metadata access to that service over HTTP.
        sqlmesh_request_timeout: Per-request timeout (seconds) for calls to
            the sqlmesh-service.
    """

    def __init__(self) -> None:
        # Base URL of the sqlmesh-service (REST). Env-only, no secrets embedded.
        self.sqlmesh_service_url: str = os.getenv(
            "ONTOLOGY_SQLMESH_SERVICE_URL", "http://localhost:8002"
        ).rstrip("/")

        # HTTP timeout for sqlmesh-service metadata calls, in seconds.
        self.sqlmesh_request_timeout: float = float(
            os.getenv("ONTOLOGY_SQLMESH_REQUEST_TIMEOUT", "10")
        )

        # Allowed workspace root — paths must resolve under this base.
        self.workspace_base_path: str = os.getenv(
            "ONTOLOGY_WORKSPACE_BASE_PATH",
            os.path.expanduser("~/.agenticdata/workspaces"),
        )


# Module-level singleton used across the service.
settings = Settings()
