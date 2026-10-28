"""Ontology REST API endpoints.

Provides 9 endpoints covering list, get, validate, and search operations
on semantic model ontologies. All endpoints require ``workspace_path`` as
a query parameter for workspace isolation and path-traversal protection.

Route prefix: ``/api/v1/ontology``
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException, Query

from src.ontology.api_errors import raise_from_exception
from src.ontology.config import settings
from src.ontology.cross_ref_validator import CrossRefValidator
from src.ontology.loader import ModelRegistry
from src.ontology.models import (
    EntityDetail,
    EntityGraph,
    EntitySummary,
    MetricDetail,
    MetricSummary,
    ModelSummary,
    OntologySchemaError,
    RelationshipSummary,
    SemanticModel,
    ValidationError,
)
from src.ontology.semantic_search import SemanticSearchService
from src.ontology.sqlmesh_metadata import SqlMeshMetadataClient, SqlMeshMetadataUnavailable

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/ontology", tags=["ontology"])


def _validate_workspace_containment(workspace_path: str) -> Path:
    """Ensure workspace_path is absolute and within the allowed base directory."""
    base = Path(settings.workspace_base_path).expanduser().resolve()
    target = Path(workspace_path).resolve()
    if not target.is_absolute():
        raise HTTPException(
            status_code=400,
            detail={"error": "workspace_path must be an absolute path"},
        )
    if not str(target).startswith(str(base)):
        raise HTTPException(
            status_code=400,
            detail={"error": f"workspace_path must be under {base}"},
        )
    if not target.exists():
        raise HTTPException(
            status_code=400,
            detail={"error": "workspace_path does not exist"},
        )
    return target


# ---------------------------------------------------------------------------
# Helper: build registry + search service for a workspace
# ---------------------------------------------------------------------------

def _build_services(workspace_path: str) -> tuple[ModelRegistry, SemanticSearchService]:
    """Create ModelRegistry and SemanticSearchService for a workspace path.

    Raises:
        HTTPException 400: if workspace_path is a relative path or invalid.
    """
    ws = _validate_workspace_containment(workspace_path)
    registry = ModelRegistry(ws)
    search = SemanticSearchService(registry)
    return registry, search


async def _fetch_sqlmesh_metadata(
    workspace_path: str,
) -> tuple[Optional[set[str]], Optional[set[str]], Optional[dict[str, str]]]:
    """Fetch SQLMesh model / metric metadata IN-PROCESS.

    The model list is fetched once and used to derive both the identifier set
    (name + fqn) and the model -> gateway map, avoiding a redundant call.
    Metrics are fetched independently so a partial failure still allows the
    other checks to run. A value is ``None`` when its metadata is unavailable,
    in which case the corresponding check is skipped rather than failing the
    whole validation.

    P3 NOTE: these were blocking HTTP calls dispatched via ``asyncio.to_thread``
    when ontology was a separate service; they are now plain async in-process
    calls, so the gather below runs them concurrently on the loop.
    """
    client = SqlMeshMetadataClient()

    async def _fetch_models() -> Optional[list[dict]]:
        try:
            return await client.fetch_models(workspace_path)
        except SqlMeshMetadataUnavailable:
            return None

    async def _fetch_metrics() -> Optional[set[str]]:
        try:
            return await client.fetch_metric_names(workspace_path)
        except SqlMeshMetadataUnavailable:
            return None

    raw_models, metrics = await asyncio.gather(
        _fetch_models(),
        _fetch_metrics(),
    )

    models: Optional[set[str]] = None
    gateways: Optional[dict[str, str]] = None
    if raw_models is not None:
        models = client.identifiers_from_models(raw_models)
        gateways = client.gateways_from_models(raw_models)
    return models, metrics, gateways


async def _validate_model(
    model: SemanticModel,
    workspace_path: str,
    check_sqlmesh: bool,
) -> tuple[list[ValidationError], bool]:
    """Validate a model, optionally including SQLMesh existence checks.

    Returns:
        A tuple of (validation errors, sqlmesh_checked). ``sqlmesh_checked`` is
        True only when both model and metric metadata were successfully
        retrieved and used.
    """
    sqlmesh_models: Optional[set[str]] = None
    sqlmesh_metrics: Optional[set[str]] = None
    sqlmesh_model_gateways: Optional[dict[str, str]] = None
    if check_sqlmesh:
        (
            sqlmesh_models,
            sqlmesh_metrics,
            sqlmesh_model_gateways,
        ) = await _fetch_sqlmesh_metadata(workspace_path)

    errors = CrossRefValidator.validate(
        model,
        sqlmesh_models=sqlmesh_models,
        sqlmesh_metrics=sqlmesh_metrics,
        sqlmesh_model_gateways=sqlmesh_model_gateways,
    )
    sqlmesh_checked = sqlmesh_models is not None and sqlmesh_metrics is not None
    return errors, sqlmesh_checked


# ---------------------------------------------------------------------------
# 1. GET /ontology — list all ontologies in a workspace
# ---------------------------------------------------------------------------

@router.get("")
async def list_ontologies(
    workspace_path: str = Query(..., description="Absolute path to the SQLMesh workspace"),
    keyword: Optional[str] = Query(
        None, description="Optional keyword to filter model names/descriptions"
    ),
) -> list[ModelSummary]:
    """List all available ontologies in the workspace's ontology/ directory.

    Returns lightweight summaries (name, description, keywords).
    Use ``keyword`` to filter by model name or description substring.
    """
    try:
        _, _ = _build_services(workspace_path)
        registry = ModelRegistry(Path(workspace_path))
        summaries = registry.list_models()

        if keyword:
            kw = keyword.lower()
            summaries = [
                s for s in summaries
                if kw in s.name.lower()
                or (s.description and kw in s.description.lower())
                or any(kw in k.lower() for k in s.keywords)
            ]

        logger.info(
            "ontology_list_success",
            workspace=workspace_path,
            count=len(summaries),
            keyword=keyword,
        )
        return summaries

    except HTTPException:
        raise
    except Exception as e:
        raise_from_exception(e)


# ---------------------------------------------------------------------------
# 2. GET /ontology/{name} — get full model with validation
# ---------------------------------------------------------------------------

@router.get("/{name:path}")
async def get_ontology(
    name: str,
    workspace_path: str = Query(..., description="Absolute path to the SQLMesh workspace"),
    check_sqlmesh: bool = Query(
        True,
        description="Verify Entity.model / Metric.maps_to against the SQLMesh workspace",
    ),
) -> dict:
    """Get a full semantic model by ontology name, including validation results.

    The ``name`` parameter matches the YAML file stem (without .yaml extension).

    Returns:
        dict with ``model`` (SemanticModel), ``validation_errors`` (list of
        ValidationError), and ``sqlmesh_checked`` (whether SQLMesh existence
        checks were applied).

        When the YAML fails schema validation the response degrades
        gracefully: ``model`` is ``null``, ``schema_invalid`` is ``True``, and
        ``validation_errors`` carries per-field errors — a 400 here would only
        show the user an opaque failure and leave the agent nothing to act on.
    """
    try:
        registry, _ = _build_services(workspace_path)

        try:
            model = registry.get_model(name)
        except OntologySchemaError as e:
            logger.info(
                "ontology_schema_invalid",
                workspace=workspace_path,
                ontology=name,
                error_count=len(e.errors),
            )
            return {
                "model": None,
                "schema_invalid": True,
                "validation_errors": e.to_validation_errors(),
                "sqlmesh_checked": False,
            }
        errors, sqlmesh_checked = await _validate_model(model, workspace_path, check_sqlmesh)

        logger.info(
            "ontology_get_success",
            workspace=workspace_path,
            ontology=name,
            validation_error_count=len(errors),
            sqlmesh_checked=sqlmesh_checked,
        )
        return {
            "model": model,
            "schema_invalid": False,
            "validation_errors": errors,
            "sqlmesh_checked": sqlmesh_checked,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise_from_exception(e)


# ---------------------------------------------------------------------------
# 3. POST /ontology/{name}/validate — validate cross-references
# ---------------------------------------------------------------------------

@router.post("/{name:path}/validate")
async def validate_ontology(
    name: str,
    workspace_path: str = Query(..., description="Absolute path to the SQLMesh workspace"),
    check_sqlmesh: bool = Query(
        True,
        description="Verify Entity.model / Metric.maps_to against the SQLMesh workspace",
    ),
) -> dict:
    """Validate cross-references in an ontology (entities, relationships, metrics).

    When ``check_sqlmesh`` is enabled, also verifies that ``Entity.model`` and
    ``Metric.maps_to`` reference objects that actually exist in the SQLMesh
    workspace. If the sqlmesh-service is unavailable these checks are skipped
    (reflected by ``sqlmesh_checked=False``) rather than failing validation.

    Returns:
        dict with ``ontology_name``, ``valid`` (bool), ``errors`` (list of
        ValidationError), and ``sqlmesh_checked`` (bool).

    A YAML that fails schema validation is reported as ``valid=False`` with
    ``error_kind="schema"`` and per-field errors — NOT as an HTTP 400. The
    validate output is consumed by the ``sqlmesh ontology validate`` CLI and
    by agents that fix the YAML from the reported paths, so an invalid file
    is a normal, actionable result, not a transport-level failure.
    """
    try:
        registry, _ = _build_services(workspace_path)
        try:
            model = registry.get_model(name)
        except OntologySchemaError as e:
            logger.info(
                "ontology_schema_invalid",
                workspace=workspace_path,
                ontology=name,
                error_count=len(e.errors),
            )
            return {
                "ontology_name": name,
                "valid": False,
                "error_kind": "schema",
                "errors": e.to_validation_errors(),
                "sqlmesh_checked": False,
            }
        errors, sqlmesh_checked = await _validate_model(model, workspace_path, check_sqlmesh)

        logger.info(
            "ontology_validate_success",
            workspace=workspace_path,
            ontology=name,
            error_count=len(errors),
            sqlmesh_checked=sqlmesh_checked,
        )
        return {
            "ontology_name": name,
            "valid": len(errors) == 0,
            "error_kind": "cross_reference",
            "errors": errors,
            "sqlmesh_checked": sqlmesh_checked,
        }

    except FileNotFoundError as e:
        raise_from_exception(e)
    except ValueError as e:
        raise_from_exception(e)
    except HTTPException:
        raise
    except Exception as e:
        raise_from_exception(e)


# ---------------------------------------------------------------------------
# 4. GET /ontology/{name}/entities/search — search entities
# ---------------------------------------------------------------------------

@router.get("/{name:path}/entities/search")
async def search_entities(
    name: str,
    keyword: str = Query(..., description="Search keyword (case-insensitive)"),
    workspace_path: str = Query(..., description="Absolute path to the SQLMesh workspace"),
    limit: int = Query(5, ge=1, le=50, description="Maximum number of results"),
) -> list[EntitySummary]:
    """Search entities within an ontology by keyword.

    Matches against entity name, description, synonyms, and sub-concept names.
    Results are sorted by match score (descending).
    """
    try:
        _, search = _build_services(workspace_path)
        results = search.search_entities(name, keyword, limit=limit)

        logger.info(
            "ontology_entities_search",
            workspace=workspace_path,
            ontology=name,
            keyword=keyword,
            result_count=len(results),
        )
        return results

    except FileNotFoundError as e:
        raise_from_exception(e)
    except HTTPException:
        raise
    except Exception as e:
        raise_from_exception(e)


# ---------------------------------------------------------------------------
# 5. GET /ontology/{name}/metrics/search — search metrics
# ---------------------------------------------------------------------------

@router.get("/{name:path}/metrics/search")
async def search_metrics(
    name: str,
    keyword: str = Query(..., description="Search keyword (case-insensitive)"),
    workspace_path: str = Query(..., description="Absolute path to the SQLMesh workspace"),
    limit: int = Query(5, ge=1, le=50, description="Maximum number of results"),
) -> list[MetricSummary]:
    """Search metrics within an ontology by keyword.

    Matches against metric name, description, and synonyms.
    Results are sorted by match score (descending).
    """
    try:
        _, search = _build_services(workspace_path)
        results = search.search_metrics(name, keyword, limit=limit)

        logger.info(
            "ontology_metrics_search",
            workspace=workspace_path,
            ontology=name,
            keyword=keyword,
            result_count=len(results),
        )
        return results

    except FileNotFoundError as e:
        raise_from_exception(e)
    except HTTPException:
        raise
    except Exception as e:
        raise_from_exception(e)


# ---------------------------------------------------------------------------
# 6. GET /ontology/{name}/relationships/search — search relationships
# ---------------------------------------------------------------------------

@router.get("/{name:path}/relationships/search")
async def search_relationships(
    name: str,
    keyword: str = Query(..., description="Search keyword (case-insensitive)"),
    workspace_path: str = Query(..., description="Absolute path to the SQLMesh workspace"),
    limit: int = Query(5, ge=1, le=50, description="Maximum number of results"),
) -> list[RelationshipSummary]:
    """Search relationships within an ontology by keyword.

    Matches against relationship name, verbalizes templates, and involved entity names.
    Results are sorted by match score (descending).
    """
    try:
        _, search = _build_services(workspace_path)
        results = search.search_relationships(name, keyword, limit=limit)

        logger.info(
            "ontology_relationships_search",
            workspace=workspace_path,
            ontology=name,
            keyword=keyword,
            result_count=len(results),
        )
        return results

    except FileNotFoundError as e:
        raise_from_exception(e)
    except HTTPException:
        raise
    except Exception as e:
        raise_from_exception(e)


# ---------------------------------------------------------------------------
# 7. GET /ontology/{name}/entities/{entity_name} — entity detail
# ---------------------------------------------------------------------------

@router.get("/{name:path}/entities/{entity_name:path}")
async def get_entity_detail(
    name: str,
    entity_name: str,
    workspace_path: str = Query(..., description="Absolute path to the SQLMesh workspace"),
) -> EntityDetail:
    """Get full detail of a specific entity.

    Includes attributes, foreign keys, sub-concepts, constraints,
    and upstream/downstream relationships with JOIN conditions.
    """
    try:
        _, search = _build_services(workspace_path)
        result = search.get_entity_detail(name, entity_name)

        logger.info(
            "ontology_entity_detail",
            workspace=workspace_path,
            ontology=name,
            entity=entity_name,
        )
        return result

    except FileNotFoundError as e:
        raise_from_exception(e)
    except HTTPException:
        raise
    except Exception as e:
        raise_from_exception(e)


# ---------------------------------------------------------------------------
# 8. GET /ontology/{name}/metrics/{metric_name} — metric detail
# ---------------------------------------------------------------------------

@router.get("/{name:path}/metrics/{metric_name:path}")
async def get_metric_detail(
    name: str,
    metric_name: str,
    workspace_path: str = Query(..., description="Absolute path to the SQLMesh workspace"),
) -> MetricDetail:
    """Get full definition of a specific metric.

    Includes aggregate, expression, filter, derived formula, and AI context.
    """
    try:
        _, search = _build_services(workspace_path)
        result = search.get_metric_detail(name, metric_name)

        logger.info(
            "ontology_metric_detail",
            workspace=workspace_path,
            ontology=name,
            metric=metric_name,
        )
        return result

    except FileNotFoundError as e:
        raise_from_exception(e)
    except HTTPException:
        raise
    except Exception as e:
        raise_from_exception(e)


# ---------------------------------------------------------------------------
# 9. GET /ontology/{name}/entities/{entity_name}/graph — entity graph
# ---------------------------------------------------------------------------

@router.get("/{name:path}/entities/{entity_name:path}/graph")
async def get_entity_graph(
    name: str,
    entity_name: str,
    workspace_path: str = Query(..., description="Absolute path to the SQLMesh workspace"),
    depth: int = Query(1, ge=1, le=3, description="BFS expansion depth (1-3)"),
) -> EntityGraph:
    """Get a relationship subgraph centred on a specific entity.

    Uses BFS to expand outward up to ``depth`` hops.
    ``depth=1`` returns direct neighbours; ``depth=2`` returns neighbours of neighbours.
    """
    try:
        _, search = _build_services(workspace_path)
        result = search.get_entity_graph(name, entity_name, depth=depth)

        logger.info(
            "ontology_entity_graph",
            workspace=workspace_path,
            ontology=name,
            entity=entity_name,
            depth=depth,
            node_count=len(result.nodes),
            edge_count=len(result.edges),
        )
        return result

    except FileNotFoundError as e:
        raise_from_exception(e)
    except HTTPException:
        raise
    except Exception as e:
        raise_from_exception(e)
