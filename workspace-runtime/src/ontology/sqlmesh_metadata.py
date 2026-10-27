"""In-process accessor for SQLMesh metadata (formerly an HTTP client).

P3 NOTE: the ontology service used to query the sqlmesh-service over HTTP to
verify that a semantic model's ``Entity.model`` and ``Metric.maps_to``
reference real SQLMesh objects. After the merge both sides live in
workspace-runtime, so this module now calls the model service directly — same
fail-soft contract (``SqlMeshMetadataUnavailable`` => validation skips the
existence checks with a warning), zero network hops.

The module boundary is kept on purpose: the validator stays pure and
unit-testable (it only receives plain identifier sets).
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


class SqlMeshMetadataUnavailable(Exception):
    """Raised when SQLMesh metadata cannot be retrieved.

    The caller should treat SQLMesh existence checks as "skipped" (a warning),
    rather than failing the whole validation, so that ontology validation
    still works when the metadata side is unavailable.
    """


class SqlMeshMetadataClient:
    """Fetches model / metric identifiers from the in-process model service.

    A single instance can be reused across requests; it holds no state.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        # Arguments kept for call-site compatibility with the former HTTP
        # client; they carry no meaning anymore.
        del base_url, timeout

    async def fetch_models(self, workspace_path: str) -> list[dict]:
        """Return the raw SQLMesh model list.

        Each item carries at least ``name`` and ``fqn``, and (for external
        models bound to a non-default gateway) a ``gateway`` field. Callers
        derive identifier sets and gateway maps from this single payload.

        Raises:
            SqlMeshMetadataUnavailable: If the model service fails.
        """
        from src.sqlmesh.model_service import get_model_service

        try:
            data = await get_model_service().list_models(
                workspace_path=workspace_path, gateway=None, environment=None
            )
        except Exception as e:  # noqa: BLE001 — fail-soft by contract
            logger.warning(
                "sqlmesh_metadata_unavailable",
                workspace=workspace_path,
                error=str(e),
            )
            raise SqlMeshMetadataUnavailable(
                f"Failed to fetch SQLMesh metadata: {e}"
            ) from e
        models = list(data.get("models", []))
        logger.info(
            "sqlmesh_models_fetched",
            workspace=workspace_path,
            model_count=len(models),
        )
        return models

    async def fetch_model_identifiers(self, workspace_path: str) -> set[str]:
        """Return the set of known SQLMesh model identifiers.

        The set contains both the model ``name`` and its fully-qualified
        ``fqn`` for every model, so the validator can match a semantic-layer
        ``Entity.model`` (e.g. ``sushi.customers``) against either form.
        """
        return self.identifiers_from_models(await self.fetch_models(workspace_path))

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

    async def fetch_metric_names(self, workspace_path: str) -> set[str]:
        """Return the set of known SQLMesh METRIC names (flat identifiers).

        Raises:
            SqlMeshMetadataUnavailable: If the model service fails.
        """
        from src.sqlmesh.model_service import get_model_service

        try:
            data = await get_model_service().list_metrics(
                workspace_path=workspace_path, gateway=None
            )
        except Exception as e:  # noqa: BLE001 — fail-soft by contract
            logger.warning(
                "sqlmesh_metadata_unavailable",
                workspace=workspace_path,
                error=str(e),
            )
            raise SqlMeshMetadataUnavailable(
                f"Failed to fetch SQLMesh metrics: {e}"
            ) from e
        names = {item.get("name") for item in data.get("metrics", []) if item.get("name")}
        logger.info(
            "sqlmesh_metrics_fetched",
            workspace=workspace_path,
            metric_count=len(names),
        )
        return names
