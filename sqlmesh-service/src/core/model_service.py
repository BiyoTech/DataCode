"""Model service - SQLMesh model operations.

Provides model listing, detail retrieval, rendering (with expand), and validation.
Matches the SQLMesh web server's model endpoint behavior.
"""

import asyncio
from typing import Any, Dict, List, Optional

from src.core.context_pool import get_context_pool
from src.core.environment_topology import (
    build_environment_model_items,
    is_local_environment,
)
from src.core.format import format_model_detail, format_model_list_item
from src.utils.errors import (
    ErrorCode,
    ModelNotFoundError,
    ServiceError,
    service_error_from_exception,
)
from src.utils.error_archive import ErrorArchiveContext
from src.utils.logging import get_logger

logger = get_logger(__name__)


class ModelService:
    """Model read operations using SQLMesh SDK."""

    async def list_models(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List all models in the project.

        Matches SQLMesh web server's GET /models behavior:
        - Refreshes context
        - Returns sorted list with display info

        Environment handling: when ``environment`` names a promoted SQLMesh
        virtual environment (e.g. ``prod``/``dev``), the listing is derived from
        that environment's snapshots so it reflects what is actually deployed.
        When it is empty or refers to the local definition view, the in-memory
        ``context.models`` are listed (the previous behavior).

        Gateway handling: SQLMesh loads a separate context per gateway and only
        includes external models whose ``gateway`` field matches the loaded
        gateway (models without a gateway form a shared base set). Therefore:

        - When an explicit ``gateway`` is requested, only that gateway's models
          are listed (native single-gateway behavior).
        - When no gateway is requested, models are aggregated across ALL
          configured gateways so external models bound to any gateway are
          visible in a single unified listing.
        """
        pool = get_context_pool()

        try:
            # Environment-scoped listing takes precedence: build directly from
            # the promoted environment's snapshots (single context is enough,
            # snapshots already capture the promoted external models).
            entry = await pool.get_context(workspace_path, gateway)
            await pool.refresh_if_dirty(entry)
            if not is_local_environment(entry.context, environment):
                models = await asyncio.to_thread(
                    build_environment_model_items, entry.context, environment or ""
                )
                models.sort(key=lambda m: m.get("fqn", m.get("name", "")))
                return {"models": models}

            if gateway is not None:
                models = await self._list_gateway_models(pool, workspace_path, gateway)
            else:
                models = await self._list_all_gateway_models(pool, workspace_path)

            models.sort(key=lambda m: m.get("fqn", m.get("name", "")))
            return {"models": models}
        except ServiceError:
            # Already a structured, actionable error (e.g. context refresh /
            # load failure) — surface it as-is instead of masking it as a
            # generic INTERNAL_ERROR.
            raise
        except Exception as e:
            logger.error("list_models_failed", error=str(e), workspace_path=workspace_path)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to list models",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="model.list",
                    gateway=gateway,
                ),
            ) from e

    async def _list_gateway_models(
        self,
        pool,
        workspace_path: str,
        gateway: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Load a single gateway's context and return formatted list items."""
        entry = await pool.get_context(workspace_path, gateway)
        await pool.refresh_if_dirty(entry)
        # Pick up on-disk edits to tracked files (external CLI/editor changes).
        await asyncio.to_thread(entry.context.refresh)

        models_dict = await asyncio.to_thread(lambda: entry.context.models)
        return [format_model_list_item(m) for m in models_dict.values()]

    async def _list_all_gateway_models(
        self,
        pool,
        workspace_path: str,
    ) -> List[Dict[str, Any]]:
        """Aggregate models across every configured gateway.

        Loads the default context first (which also exposes the full gateway
        set via its parsed config), then loads the remaining gateway contexts
        concurrently and merges the results. Deduplication is keyed by FQN
        because the base (non-gateway) models appear in every context; only
        gateway-specific external models differ between gateways.

        A single gateway failing to load (e.g. missing connection credentials)
        is logged and skipped so it never blanks out the whole listing.
        """
        # Default context: provides base + default-gateway external models and
        # lets us discover the configured gateway set from the loaded config.
        base_entry = await pool.get_context(workspace_path, None)
        await pool.refresh_if_dirty(base_entry)
        await asyncio.to_thread(base_entry.context.refresh)

        config = base_entry.context.config
        gateway_names = list(config.gateways.keys())
        default_gw = config.default_gateway_name

        merged: Dict[str, Dict[str, Any]] = {}
        base_models = await asyncio.to_thread(lambda: base_entry.context.models)
        for model in base_models.values():
            item = format_model_list_item(model)
            merged.setdefault(item.get("fqn", item.get("name", "")), item)

        # Load the non-default gateway contexts concurrently. The default
        # gateway is already covered by ``base_entry`` above, so skip it to
        # avoid a redundant context load.
        other_gateways = [gw for gw in gateway_names if gw and gw != default_gw]
        if other_gateways:
            results = await asyncio.gather(
                *(
                    self._list_gateway_models(pool, workspace_path, gw)
                    for gw in other_gateways
                ),
                return_exceptions=True,
            )
            for gw, result in zip(other_gateways, results):
                if isinstance(result, Exception):
                    logger.warning(
                        "list_models_gateway_skipped",
                        workspace_path=workspace_path,
                        gateway=gw,
                        error=str(result),
                    )
                    continue
                for item in result:
                    merged.setdefault(item.get("fqn", item.get("name", "")), item)

        return list(merged.values())

    async def get_model(
        self,
        workspace_path: str,
        model_name: str,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get detailed info for a single model including rendered SQL.

        Matches SQLMesh web server's GET /models/{name} behavior:
        - Full column info with descriptions
        - Model details (cron, partitioned_by, etc.)
        - Rendered SQL query
        """
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)
        await pool.refresh_if_dirty(entry)

        try:
            model = await asyncio.to_thread(
                entry.context.get_model, model_name, raise_if_missing=False
            )
            if not model:
                raise ModelNotFoundError(model_name)

            return format_model_detail(model, context=entry.context, render_query=True)
        except ModelNotFoundError:
            raise
        except Exception as e:
            logger.error("get_model_failed", error=str(e), model_name=model_name)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message=f"Failed to get model: {model_name}",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="model.get",
                    gateway=gateway,
                    model_fqn=model_name,
                ),
            ) from e

    async def list_metrics(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List all SQLMesh METRIC names defined in the project.

        Metric names are flat identifiers (not fully-qualified), e.g.
        ``total_orders``. Used by the ontology service to verify that a
        semantic-layer metric's ``maps_to`` points to a real SQLMesh METRIC.
        """
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)
        await pool.refresh_if_dirty(entry)

        try:
            # context.metrics is a mapping of {metric_name: Metric}
            metrics_dict = await asyncio.to_thread(lambda: entry.context.metrics)
            names = sorted(metrics_dict.keys())
            return {"metrics": [{"name": n} for n in names]}
        except Exception as e:
            logger.error("list_metrics_failed", error=str(e), workspace_path=workspace_path)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to list metrics",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="metric.list",
                    gateway=gateway,
                ),
            ) from e

    async def render_model(
        self,
        workspace_path: str,
        model_name: str,
        gateway: Optional[str] = None,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        execution_time: Optional[str] = None,
        expand: bool = False,
        pretty: bool = True,
        dialect: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Render a model's SQL with macro expansion.

        Matches SQLMesh web server's POST /commands/render behavior:
        - Supports expand (expand referenced models)
        - Supports start/end/execution_time parameters
        - Returns rendered SQL with dialect formatting
        """
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)
        await pool.refresh_if_dirty(entry)

        try:
            snapshot = await asyncio.to_thread(
                entry.context.get_snapshot, model_name, raise_if_missing=False
            )
            if not snapshot:
                raise ModelNotFoundError(model_name)

            rendered = await asyncio.to_thread(
                entry.context.render,
                snapshot,
                start=start,
                end=end,
                execution_time=execution_time,
                expand=expand,
            )

            effective_dialect = dialect or entry.context.config.dialect
            sql = rendered.sql(pretty=pretty, dialect=effective_dialect)

            return {"sql": sql, "dialect": effective_dialect}
        except ModelNotFoundError:
            raise
        except Exception as e:
            # Render failures are usually macro/SQL errors in the user's model
            # (a SQLMeshError). Surface the raw reason as the message so the CLI
            # and frontend show what to fix, matching the native `render` output.
            logger.error("render_model_failed", error=str(e), model_name=model_name)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message=f"Failed to render model: {model_name}",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="model.render",
                    gateway=gateway,
                    model_fqn=model_name,
                    context={
                        "start": start,
                        "end": end,
                        "expand": expand,
                        "dialect": dialect,
                    },
                ),
            ) from e

    async def validate(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate all model files by reloading context.

        Always returns HTTP 200 with a structured verdict (this is the validate
        contract): ``valid`` plus an ``errors`` list. On failure we surface the
        full reason text so the frontend can render actionable diagnostics; an
        empty exception string falls back to the exception class name so the
        list is never a blank entry.
        """
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)

        try:
            await asyncio.to_thread(entry.context.load)
            entry.dirty = False
            return {"valid": True, "errors": [], "warnings": []}
        except Exception as e:
            error_text = str(e).strip() or e.__class__.__name__
            logger.warning("validation_failed", error=error_text, workspace_path=workspace_path)
            return {"valid": False, "errors": [error_text], "warnings": []}


# Singleton
_model_service: Optional[ModelService] = None


def get_model_service() -> ModelService:
    global _model_service
    if _model_service is None:
        _model_service = ModelService()
    return _model_service
