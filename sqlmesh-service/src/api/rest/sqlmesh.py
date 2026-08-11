"""SQLMesh operation endpoints.

Route layout matches the SQLMesh web server API structure:
- /plan         - Plan preview
- /apply        - Plan apply
- /cancel       - Plan cancel
- /models       - List models
- /models/{name} - Get model detail
- /render       - Render model SQL
- /validate     - Validate models
- /evaluate     - Evaluate model
- /query        - Execute ad-hoc SQL
- /lineage      - Model lineage
- /lineage/{model}/{column} - Column lineage
- /environments - List environments
- /environments/{name} - Delete environment
- /test         - Run tests
- /table-diff   - Table diff
- /schema/discover - Schema discovery
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from starlette.status import HTTP_204_NO_CONTENT
from src.utils.logging import get_logger

from src.api.deps import (
    WorkspacePath,
    get_command_service,
    get_environment_service,
    get_external_models_service,
    get_lineage_service,
    get_model_service,
    get_plan_service,
    get_query_service,
    get_schema_service,
)
from src.api.models.requests import (
    EvaluateRequest,
    ImportExternalModelsRequest,
    ListSchemasRequest,
    PlanApplyRequest,
    PlanCancelRequest,
    PlanPreviewRequest,
    QueryRequest,
    RenderModelRequest,
    SchemaDiscoverRequest,
    TableDiffRequest,
    TestRequest,
    ValidateRequest,
)

router = APIRouter(prefix="/api/v1", tags=["sqlmesh"])


# ---------------------------------------------------------------------------
# Plan / Apply / Cancel
# ---------------------------------------------------------------------------
logger = get_logger(__name__)
@router.post("/plan")
async def plan_preview(req: PlanPreviewRequest):
    """Create a plan preview without applying.

    Returns diff information and a state fingerprint for optimistic locking.
    Matches SQLMesh web server's POST /plan behavior.
    """
    result = await get_plan_service().preview(
            workspace_path=req.workspace_path,
            environment=req.environment,
            gateway=req.gateway,
            plan_dates=req.plan_dates.model_dump() if req.plan_dates else None,
            plan_options=req.plan_options.model_dump() if req.plan_options else None,
    )
    logger.debug(f"plan_preview result: {result}")
    return result


@router.post("/apply")
async def plan_apply(req: PlanApplyRequest):
    """Create and apply a plan (synchronous).

    If expected_fingerprint is provided, will reject if workspace
    was modified since the plan preview (optimistic locking).
    Progress is also broadcast via SSE /api/v1/events.

    Attribution fields (user_id, session_id, trigger_type) are injected
    by the CLI wrapper for agent-triggered applies, or by the frontend
    for human-triggered applies. They are persisted in the apply history
    for auditability.
    """
    result = await get_plan_service().apply(
            workspace_path=req.workspace_path,
            environment=req.environment,
            gateway=req.gateway,
            plan_dates=req.plan_dates.model_dump() if req.plan_dates else None,
            plan_options=req.plan_options.model_dump() if req.plan_options else None,
            categories=req.categories,
            expected_fingerprint=req.expected_fingerprint,
            user_id=req.user_id or "",
            session_id=req.session_id or "",
            trigger_type=req.trigger_type or "human",
    )
    return result


@router.post("/apply/async")
async def plan_apply_async(req: PlanApplyRequest):
    """Create and apply a plan (non-blocking, async).

    Returns immediately. Progress is delivered via SSE /api/v1/events.
    Monitor plan-apply events for stage updates.
    """
    result = await get_plan_service().apply_async(
            workspace_path=req.workspace_path,
            environment=req.environment,
            gateway=req.gateway,
            plan_dates=req.plan_dates.model_dump() if req.plan_dates else None,
            plan_options=req.plan_options.model_dump() if req.plan_options else None,
            categories=req.categories,
            expected_fingerprint=req.expected_fingerprint,
            user_id=req.user_id or "",
            session_id=req.session_id or "",
            trigger_type=req.trigger_type or "human",
    )
    return result


@router.post("/cancel")
async def plan_cancel(req: PlanCancelRequest):
    """Cancel a running plan application.

    Sets the circuit breaker to signal cancellation.
    Matches SQLMesh web server's POST /plan/cancel behavior.
    """
    return await get_plan_service().cancel(
            workspace_path=req.workspace_path,
            gateway=req.gateway,
    )



# ---------------------------------------------------------------------------
# Apply History
# ---------------------------------------------------------------------------

@router.get("/apply/history")
async def list_apply_history(workspace_path: WorkspacePath, limit: int = 50, offset: int = 0):
    """List apply history records for a workspace (newest first).

    Returns summary records (no plan_result). Use /apply/history/{id}
    to fetch the full detail with changes/backfills/test_results.
    """
    from src.core.history_service import get_history_service

    return await get_history_service().list(
    workspace_path=workspace_path,
    limit=min(limit, 200),
    offset=max(offset, 0),
    )


@router.get("/apply/history/{record_id}")
async def get_apply_history_detail(record_id: str):
    """Get a single apply history record with full plan_result payload.

    Includes complete changes (added/removed/direct/indirect/metadata),
    backfills, and test results for rendering in the History detail panel.
    """
    from src.core.history_service import get_history_service

    detail = await get_history_service().get_detail(record_id)
    if detail is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "History record not found"})
    return detail


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@router.get("/models")
async def list_models(workspace_path: WorkspacePath, gateway: str = "", environment: str = ""):
    """List all models in a workspace.

    Matches SQLMesh web server's GET /models behavior. When ``environment``
    names a promoted virtual environment (prod/dev), the listing is derived from
    that environment's snapshots; otherwise the local project definition is used.
    """
    return await get_model_service().list_models(
            workspace_path=workspace_path,
            gateway=gateway or None,
            environment=environment or None,
    )



@router.get("/models/{model_name:path}")
async def get_model(model_name: str, workspace_path: WorkspacePath, gateway: str = ""):
    """Get detailed info for a single model.

    Matches SQLMesh web server's GET /models/{name} behavior.
    Includes columns with descriptions, model details, and rendered SQL.
    """
    return await get_model_service().get_model(
            workspace_path=workspace_path,
            model_name=model_name,
            gateway=gateway or None,
    )



@router.get("/metrics")
async def list_metrics(workspace_path: WorkspacePath, gateway: str = ""):
    """List all SQLMesh METRIC names defined in a workspace.

    Returns flat metric identifiers (e.g. ``total_orders``). Consumed by the
    ontology service to validate that a metric's ``maps_to`` references an
    existing SQLMesh METRIC.
    """
    return await get_model_service().list_metrics(
            workspace_path=workspace_path,
            gateway=gateway or None,
    )



@router.post("/render")
async def render_model(req: RenderModelRequest):
    """Render a model's SQL with macro expansion.

    Matches SQLMesh web server's POST /commands/render behavior.
    Supports expand (expand referenced models), start/end/execution_time.
    """
    expand = req.expand if isinstance(req.expand, bool) else bool(req.expand)
    return await get_model_service().render_model(
            workspace_path=req.workspace_path,
            model_name=req.model_name,
            gateway=req.gateway,
            start=req.start,
            end=req.end,
            execution_time=req.execution_time,
            expand=expand,
            pretty=req.pretty,
            dialect=req.dialect,
    )



@router.post("/validate")
async def validate_models(req: ValidateRequest):
    """Validate all model files in the workspace."""
    return await get_model_service().validate(
            workspace_path=req.workspace_path,
            gateway=req.gateway,
    )



# ---------------------------------------------------------------------------
# Evaluate / Query
# ---------------------------------------------------------------------------

@router.post("/evaluate")
async def evaluate_model(req: EvaluateRequest):
    """Evaluate a model with a default limit of 1000.

    Matches SQLMesh web server's POST /commands/evaluate behavior.
    Returns column names and row data.
    """
    return await get_command_service().evaluate(
            workspace_path=req.workspace_path,
            model=req.model,
            gateway=req.gateway,
            start=req.start,
            end=req.end,
            execution_time=req.execution_time,
            limit=req.limit,
    )



@router.post("/query")
async def execute_query(req: QueryRequest):
    """Execute an ad-hoc SQL query with safe LIMIT enforcement.

    Matches SQLMesh web server's POST /commands/fetchdf behavior.
    """
    return await get_query_service().execute(
            workspace_path=req.workspace_path,
            sql=req.sql,
            gateway=req.gateway,
            row_limit=req.row_limit,
    )



# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------

@router.get("/lineage")
async def get_lineage(
    workspace_path: WorkspacePath,
    model_name: str = "",
    gateway: str = "",
    environment: str = "",
):
    """Get model dependency DAG.

    If **model_name** is provided, returns a focused subgraph containing both
    upstream ancestors and downstream descendants of that model, along with
    per-node metadata (``nodes`` field) for card rendering.

    Otherwise returns the full project DAG (``nodes`` will be empty).

    When **environment** names a promoted virtual environment (prod/dev), the
    graph is derived from that environment's snapshots; otherwise the local
    project definition (``context.dag``) is used.

    Response shape::

        {
          "graph": {"<fqn>": ["<upstream_dep_fqn>", ...]},
          "nodes": {"<fqn>": {"name", "fqn", "type", "kind", "columns": [{name, type}]}}
        }
    """
    return await get_lineage_service().get_lineage(
            workspace_path=workspace_path,
            model_name=model_name,
            gateway=gateway or None,
            environment=environment or None,
    )



@router.get("/lineage/{model_name:path}/{column_name}")
async def get_column_lineage(
    model_name: str,
    column_name: str,
    workspace_path: WorkspacePath,
    models_only: bool = False,
    gateway: str = "",
):
    """Get column-level lineage as adjacency list.

    Matches SQLMesh web server's GET /lineage/{model}/{column} behavior.
    When ``models_only`` is true, returns only model-level dependencies
    (no CTE/expression details).
    """
    return await get_lineage_service().get_column_lineage(
            workspace_path=workspace_path,
            model_name=model_name,
            column_name=column_name,
            models_only=models_only,
            gateway=gateway or None,
    )



# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------

@router.get("/environments")
async def list_environments(workspace_path: WorkspacePath, gateway: str = ""):
    """Get the environments.

    Matches SQLMesh web server's GET /environments behavior.
    """
    return await get_environment_service().list_environments(
            workspace_path=workspace_path,
            gateway=gateway or None,
    )



@router.delete("/environments/{environment}")
async def delete_environment(
    environment: str,
    workspace_path: WorkspacePath = Query(..., description="Workspace root path"),
    gateway: str = "",
):
    """Invalidate and delete an environment.

    Matches SQLMesh web server's DELETE /environments/{environment} behavior.
    """
    return await get_environment_service().delete_environment(
            workspace_path=workspace_path,
            environment=environment,
            gateway=gateway or None,
    )



# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@router.get("/test")
async def run_test(
    workspace_path: WorkspacePath,
    test: str = "",
    verbosity: str = "default",
    gateway: str = "",
):
    """Run one or all model tests.

    Matches SQLMesh web server's GET /commands/test behavior.
    """
    return await get_command_service().test(
            workspace_path=workspace_path,
            gateway=gateway or None,
            test=test or None,
            verbosity=verbosity,
    )



# ---------------------------------------------------------------------------
# Table Diff
# ---------------------------------------------------------------------------

@router.get("/table-diff")
async def table_diff(
    workspace_path: WorkspacePath,
    source: str,
    target: str,
    on: str = "",
    model_or_snapshot: str = "",
    where: str = "",
    temp_schema: str = "",
    limit: int = 20,
    gateway: str = "",
):
    """Calculate differences between tables.

    Matches SQLMesh web server's GET /table_diff behavior.
    """
    return await get_command_service().table_diff(
            workspace_path=workspace_path,
            source=source,
            target=target,
            gateway=gateway or None,
            on=on or None,
            model_or_snapshot=model_or_snapshot or None,
            where=where or None,
            temp_schema=temp_schema or None,
            limit=limit,
    )



# ---------------------------------------------------------------------------
# Schema Discovery
# ---------------------------------------------------------------------------

@router.post("/schema/discover")
async def discover_schema(req: SchemaDiscoverRequest):
    """Discover table schemas from the data source."""
    return await get_schema_service().discover(
            workspace_path=req.workspace_path,
            gateway=req.gateway,
            schema_name=req.schema_name,
            table_name=req.table_name,
    )



# ---------------------------------------------------------------------------
# External Models Import
# ---------------------------------------------------------------------------

@router.get("/gateways")
async def list_gateways(workspace_path: WorkspacePath = Query(..., description="Workspace root path")):
    """List all gateways configured in the workspace config.yaml."""
    gateways = await get_external_models_service().list_gateways(workspace_path)
    default_gw = ""
    for gw in gateways:
        if gw.get("is_default"):
            default_gw = gw["name"]
            break
    return {"gateways": gateways, "default_gateway": default_gw}


@router.get("/gateways/ping")
async def ping_gateway(
    workspace_path: WorkspacePath = Query(..., description="Workspace root path"),
    gateway: str = "",
):
    """Probe connectivity of a gateway's warehouse (lightweight, short timeout).

    Returns ``{"ok": true}`` when reachable, else ``{"ok": false, "error": ...}``.
    Used by the frontend before gateway-dependent operations (switch gateway,
    select environment, refresh changes) to show a local connection-error hint
    instead of failing the whole workspace load.
    """
    return await get_external_models_service().ping_gateway(
        workspace_path=workspace_path,
        gateway=gateway or None,
    )


@router.get("/schemas")
async def list_schemas(
    workspace_path: WorkspacePath = Query(..., description="Workspace root path"),
    gateway: Optional[str] = Query(None),
    database: Optional[str] = Query(
        None,
        description=(
            "For Postgres-family dialects: list schemas within this database "
            "(3-level cascade). Ignored for MySQL-family and others."
        ),
    ),
):
    """List databases/schemas accessible through a gateway.

    Without ``database``: returns level-1 databases (all dialects).
    With ``database`` (Postgres family only): returns level-2 schemas within
    that database.
    """
    schemas = await get_external_models_service().list_schemas(workspace_path, gateway, database)
    return {"schemas": schemas}


@router.post("/external-models/import")
async def import_external_models(req: ImportExternalModelsRequest):
    """Import external table schemas and write YAML files."""
    return await get_external_models_service().import_external_models(
            workspace_path=req.workspace_path,
            gateway=req.gateway,
            schema=req.schema,
            tables=req.tables,
            include=req.include,
            exclude=req.exclude,
            samples=req.samples,
            strict=req.strict,
    )

