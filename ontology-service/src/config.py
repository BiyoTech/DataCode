"""Ontology service configuration.

Configuration is sourced from environment variables and the shared
``conf/platform.yaml``. Secrets/URLs are never hard-coded.

Shared cross-cutting config (sqlmesh service URL, workspace base path) is
sourced from ``conf/platform.yaml`` via
:mod:`datacode_common.config.platform`. Per-service env vars still take
precedence over platform defaults.
"""

from __future__ import annotations

import os

from datacode_common.config.platform import platform_config


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
        platform = platform_config()

        # ── SQLMesh service URL (from platform topology, env override wins) ──
        self.sqlmesh_service_url: str = (
            os.getenv("ONTOLOGY_SQLMESH_SERVICE_URL")
            or platform.services.url("sqlmesh", "rest_url")
            or "http://localhost:8002"
        ).rstrip("/")

        # HTTP timeout for sqlmesh-service metadata calls, in seconds.
        self.sqlmesh_request_timeout: float = float(
            os.getenv("ONTOLOGY_SQLMESH_REQUEST_TIMEOUT", "10")
        )

        # ── Workspace base path (from platform, env override wins) ──
        self.workspace_base_path: str = os.getenv(
            "ONTOLOGY_WORKSPACE_BASE_PATH",
            platform.workspace_base_path,
        )


# Module-level singleton used across the service.
settings = Settings()
