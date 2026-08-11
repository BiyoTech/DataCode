"""Client for fetching SQLMesh metadata from the sqlmesh-service.

The ontology service is intentionally lightweight and does NOT depend on the
SQLMesh SDK. To verify that a semantic model's ``Entity.model`` and
``Metric.maps_to`` reference real SQLMesh objects, it queries the
sqlmesh-service over HTTP (which owns the SQLMesh Context Pool).

All network access is isolated in this module so that the validator itself
stays pure and unit-testable (it only receives plain identifier sets).
"""

from __future__ import annotations

import httpx
import structlog

from src.config import settings

logger = structlog.get_logger(__name__)


class SqlMeshMetadataUnavailable(Exception):
    """Raised when SQLMesh metadata cannot be retrieved.

    The caller should treat SQLMesh existence checks as "skipped" (a warning),
    rather than failing the whole validation, so that ontology validation still
    works when the sqlmesh-service is unavailable.
    """


class SqlMeshMetadataClient:
    """Fetches model / metric identifiers from the sqlmesh-service.

    A single instance can be reused across requests; it opens a short-lived
    HTTP client per call to keep the surface simple and stateless.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._base_url = (base_url or settings.sqlmesh_service_url).rstrip("/")
        self._timeout = timeout if timeout is not None else settings.sqlmesh_request_timeout

    def fetch_models(self, workspace_path: str) -> list[dict]:
        """Return the raw SQLMesh model list from the sqlmesh-service.

        Each item carries at least ``name`` and ``fqn``, and (for external
        models bound to a non-default gateway) a ``gateway`` field. Callers
        derive identifier sets and gateway maps from this single payload to
        avoid issuing multiple HTTP calls.

        Raises:
            SqlMeshMetadataUnavailable: If the sqlmesh-service cannot be reached
                or returns an error response.
        """
        data = self._get("/api/v1/models", workspace_path)
        models = list(data.get("models", []))
        logger.info(
            "sqlmesh_models_fetched",
            workspace=workspace_path,
            model_count=len(models),
        )
        return models

    def fetch_model_identifiers(self, workspace_path: str) -> set[str]:
        """Return the set of known SQLMesh model identifiers.

        The set contains both the model ``name`` and its fully-qualified
        ``fqn`` for every model, so the validator can match a semantic-layer
        ``Entity.model`` (e.g. ``sushi.customers``) against either form.

        Raises:
            SqlMeshMetadataUnavailable: If the sqlmesh-service cannot be reached
                or returns an error response.
        """
        return self.identifiers_from_models(self.fetch_models(workspace_path))

    @staticmethod
    def identifiers_from_models(models: list[dict]) -> set[str]:
        """Build the identifier set (name + fqn) from a raw model list."""
        identifiers: set[str] = set()
        for item in models:
            name = item.get("name")
            fqn = item.get("fqn")
            if name:
                identifiers.add(name)
            if fqn:
                identifiers.add(fqn)
        return identifiers

    @staticmethod
    def gateways_from_models(models: list[dict]) -> dict[str, str]:
        """Build an identifier -> gateway map from a raw model list.

        Only models explicitly bound to a gateway are included. Base models
        (no ``gateway`` field) are intentionally omitted: they are shared
        across every gateway, so they impose no gateway constraint on the
        ontology that references them.
        """
        mapping: dict[str, str] = {}
        for item in models:
            gateway = item.get("gateway")
            if not gateway:
                continue
            name = item.get("name")
            fqn = item.get("fqn")
            if name:
                mapping[name] = gateway
            if fqn:
                mapping[fqn] = gateway
        return mapping

    def fetch_metric_names(self, workspace_path: str) -> set[str]:
        """Return the set of known SQLMesh METRIC names (flat identifiers).

        Raises:
            SqlMeshMetadataUnavailable: If the sqlmesh-service cannot be reached
                or returns an error response.
        """
        data = self._get("/api/v1/metrics", workspace_path)
        names = {item.get("name") for item in data.get("metrics", []) if item.get("name")}
        logger.info(
            "sqlmesh_metrics_fetched",
            workspace=workspace_path,
            metric_count=len(names),
        )
        return names

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, path: str, workspace_path: str) -> dict:
        """Perform a GET request against the sqlmesh-service.

        Raises:
            SqlMeshMetadataUnavailable: On any transport or HTTP error.
        """
        url = f"{self._base_url}{path}"
        try:
            response = httpx.get(
                url,
                params={"workspace_path": workspace_path},
                timeout=self._timeout,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.warning(
                "sqlmesh_metadata_unavailable",
                url=url,
                workspace=workspace_path,
                error=str(e),
            )
            raise SqlMeshMetadataUnavailable(
                f"Failed to fetch SQLMesh metadata from {url}: {e}"
            ) from e
