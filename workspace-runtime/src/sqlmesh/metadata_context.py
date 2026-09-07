"""MetadataContext — the runtime's read-only view of the sandbox's metadata.

Design 04 §3.7, the Context split:

    workspace-runtime (trusted)          executor sandbox (untrusted)
    ───────────────────────────          ────────────────────────────
    MetadataContext (this module)  ◀──   the real sqlmesh.Context
    pure data, cached, ms reads    ───▶  ops that exec user code

Every read endpoint goes through here; NOTHING in this module ever constructs
a Context, imports a model definition or renders a query. Constructing a
Context execs the user's Python models and macros (threat T2), so the only
Context the platform process may ever see is this module's cached, serialized
output.

Caching policy:
* **Pure metadata** (model lists, metrics, environments, model-level lineage)
  is cached per (workspace, gateway, params) and served until one of three
  things happens: the runtime marks the working copy dirty (file writes), a
  git switch / apply invalidates it, or the TTL expires. The TTL is the
  backstop for edits made outside our API (native sqlmesh CLI, editors):
  the sandbox re-checks file mtimes on every op, but only a cache MISS
  reaches it.
* **Execute-class ops** (model detail with rendered SQL, column lineage,
  query, evaluate, render, tests, table diff) are never cached — they run on
  the sandbox's hot Context and their latency is one round trip.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from datacode_common.executor_protocol import (
    OP_CONTEXT_REFRESH,
    OP_EVALUATE,
    OP_METADATA_COLUMNS,
    OP_METADATA_ENVIRONMENTS,
    OP_METADATA_LINEAGE,
    OP_METADATA_METRICS,
    OP_METADATA_MODELS,
    OP_QUERY,
    OP_RENDER,
    OP_TABLE_DIFF,
    OP_TEST,
)

from src.executor.process import ExecutorError, get_executor_manager
from src.utils.error_archive import ErrorArchiveContext
from src.utils.errors import (
    ErrorCode,
    ModelNotFoundError,
    ServiceError,
    service_error_from_exception,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)


def _retire_warm_runner(workspace_path: str) -> None:
    """Retire this copy's resident task runner, if the pool holds one.

    Fire-and-forget: retirement is a cluster round trip, and this is called from
    synchronous invalidation points (a file write, a git switch). Scheduling it
    keeps those callers synchronous without leaving a Pod serving a tree that no
    longer exists. When no event loop is running (a test, a CLI path) there is
    nothing to retire either, because the pool only exists inside the service.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        from src.tasks.warm_pool import get_warm_pool

        loop.create_task(get_warm_pool().invalidate(workspace_path))
    except Exception as exc:  # noqa: BLE001 - invalidation must never fail a write
        logger.warning(
            "warm_pool_invalidate_skipped",
            workspace_path=workspace_path,
            error=str(exc),
        )

# Ops whose results may be cached (pure metadata). Everything else is
# execute-class and always goes to the sandbox.
_CACHEABLE_OPS = frozenset(
    {OP_METADATA_MODELS, OP_METADATA_METRICS, OP_METADATA_ENVIRONMENTS}
)
# Model-level lineage is cacheable; column-level lineage (same op, extra
# params) is execute-class, so lineage is special-cased in _cacheable().
_LINEAGE_OP = OP_METADATA_LINEAGE

# Default TTL for cached metadata (seconds). Overridable via settings.
_DEFAULT_TTL_SECONDS = 60.0


def _cacheable(op: str, params: Dict[str, Any]) -> bool:
    if op == _LINEAGE_OP:
        return not params.get("column")
    return op in _CACHEABLE_OPS


class _CachedPayload:
    __slots__ = ("payload", "fetched_at")

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.fetched_at = time.monotonic()


class MetadataContextStore:
    """Cache + gateway for the sandbox's metadata ops (process-wide singleton)."""

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._cache: Dict[str, _CachedPayload] = {}
        self._ttl = ttl_seconds
        # Workspaces whose cache was explicitly invalidated; cleared as the
        # entries are re-fetched. Mirrors ExecutorManager's dirty set, kept
        # separate so the two classes stay independently testable.
        self._dirty: set[str] = set()

    # ── cache control (called from the same sites as the executor manager) ──

    def mark_dirty(self, workspace_path: str) -> None:
        """Files changed under the working copy: drop its cached metadata.

        The sandbox's warm Context is flagged for reload TOO: a newly created
        file is invisible to its mtime-based refresh (it only watches files
        tracked at load time), so both holders must see the same dirty event
        or one of them serves fresh-looking data from a stale tree. Keeping
        the two in step here makes a single call sufficient for every caller.
        """
        from src.executor.process import get_executor_manager

        get_executor_manager().mark_context_dirty(workspace_path)
        self._dirty.add(workspace_path)
        self._evict_workspace(workspace_path)

    def invalidate(self, workspace_path: str) -> None:
        """Structural change (git switch, apply, environment ops): drop now.

        Mirrors ``mark_dirty``'s sandbox coupling: the executor children are
        stopped outright (their Context describes a tree that no longer
        exists), not merely flagged for refresh. The resident task runner of
        this copy (warm pool, P6c) is retired for the same reason — it holds no
        Context, but it does hold a working directory and a SQLMesh cache built
        from the old tree.
        """
        from src.executor.process import get_executor_manager

        get_executor_manager().invalidate(workspace_path)
        _retire_warm_runner(workspace_path)
        self._dirty.add(workspace_path)
        self._evict_workspace(workspace_path)

    def invalidate_under_path(self, root_path: str) -> int:
        """Drop every cached entry under a subtree (workspace teardown)."""
        # Cache keys carry the "j:" prefix (see _cache_key); match it too or a
        # path prefix would never hit anything.
        prefix = "j:" + root_path.rstrip("/") + "/"
        doomed = [k for k in self._cache if k.startswith(prefix)]
        for key in doomed:
            del self._cache[key]
        return len(doomed)

    def _evict_workspace(self, workspace_path: str) -> None:
        prefix = f"j:{workspace_path}:"
        doomed = [k for k in self._cache if k.startswith(prefix)]
        for key in doomed:
            del self._cache[key]

    def cached_entry_count(self) -> int:
        """Observability: number of live cache entries."""
        return len(self._cache)

    # ── public read API ──────────────────────────────────────────────────

    async def models(
        self,
        workspace_path: str,
        environment: Optional[str] = None,
        gateway: Optional[str] = None,
        *,
        include_metrics: bool = False,
    ) -> Dict[str, Any]:
        """List models — one gateway's view, or the aggregate across all.

        Aggregation mirrors the pre-split behavior: with no explicit gateway,
        every configured gateway's listing is fetched and merged (deduplicated
        by FQN), and a gateway that fails to load is skipped rather than
        blanking the whole listing.
        """
        if gateway is not None:
            return await self._call(
                workspace_path,
                OP_METADATA_MODELS,
                {
                    "environment": environment or "",
                    "include_metrics": include_metrics,
                },
                gateway=gateway,
                operation="model.list",
            )

        # Aggregate across every configured gateway.
        from src.sqlmesh.config_access import load_workspace_config

        try:
            config = await asyncio.to_thread(load_workspace_config, workspace_path)
            gateway_names = [g for g in config.gateways.keys() if g]
        except Exception as e:
            raise self._translate(
                e,
                workspace_path,
                operation="model.list",
                gateway=None,
            ) from e

        merged: Dict[str, Dict[str, Any]] = {}
        metrics: List[Dict[str, Any]] = []
        errors: List[str] = []

        # The default gateway is just one of the names; no special casing —
        # every gateway contributes its external models.
        results = await asyncio.gather(
            *(
                self._call(
                    workspace_path,
                    OP_METADATA_MODELS,
                    {
                        "environment": environment or "",
                        "include_metrics": include_metrics,
                    },
                    gateway=gw,
                    operation="model.list",
                )
                for gw in gateway_names
            ),
            return_exceptions=True,
        )
        for gw, result in zip(gateway_names, results):
            if isinstance(result, BaseException):
                logger.warning(
                    "list_models_gateway_skipped",
                    workspace_path=workspace_path,
                    gateway=gw,
                    error=str(result),
                )
                errors.append(f"{gw}: {result}")
                continue
            for item in result.get("models", []):
                merged.setdefault(item.get("fqn", item.get("name", "")), item)
            metrics.extend(result.get("metrics", []))

        models = sorted(merged.values(), key=lambda m: m.get("fqn", m.get("name", "")))
        payload: Dict[str, Any] = {"models": models}
        if include_metrics:
            # Metric names are gateway-independent; dedupe + sort.
            seen = {m.get("name") for m in metrics}
            payload["metrics"] = [{"name": n} for n in sorted(seen)]
        return payload

    async def model_detail(
        self,
        workspace_path: str,
        model_name: str,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Full detail for one model, rendered SQL included (execute-class)."""
        return await self._call(
            workspace_path,
            OP_METADATA_COLUMNS,
            {"model": model_name},
            gateway=gateway,
            operation="model.get",
        )

    async def metrics(
        self, workspace_path: str, gateway: Optional[str] = None
    ) -> Dict[str, Any]:
        """The project's METRIC names."""
        return await self._call(
            workspace_path,
            OP_METADATA_METRICS,
            {},
            gateway=gateway,
            operation="metric.list",
        )

    async def environments(
        self, workspace_path: str, gateway: Optional[str] = None
    ) -> Dict[str, Any]:
        """The engine-side environment view (UI shape)."""
        return await self._call(
            workspace_path,
            OP_METADATA_ENVIRONMENTS,
            {},
            gateway=gateway,
            operation="environment.list",
        )

    async def raw_environment_names(
        self, workspace_path: str, gateway: Optional[str] = None
    ) -> Optional[List[str]]:
        """Environment names that ACTUALLY exist in state (no synthesis).

        Returns None when the state could not be read — deliberately not the
        same as an empty list (reconciliation honesty, see the binder).
        """
        try:
            result = await self._call(
                workspace_path,
                OP_METADATA_ENVIRONMENTS,
                {"raw_names": True},
                gateway=gateway,
                operation="environment.raw_names",
            )
        except ServiceError as e:
            logger.warning(
                "state_environment_names_read_failed",
                workspace_path=workspace_path,
                error=e.message,
            )
            return None
        return list(result.get("names", []))

    async def lineage(
        self,
        workspace_path: str,
        model_name: str = "",
        gateway: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Model-level dependency DAG (full or focused). Cached."""
        return await self._call(
            workspace_path,
            OP_METADATA_LINEAGE,
            {"model": model_name, "environment": environment or ""},
            gateway=gateway,
            operation="lineage.get",
        )

    async def column_lineage(
        self,
        workspace_path: str,
        model_name: str,
        column_name: str,
        models_only: bool = False,
        gateway: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Column-level lineage (execute-class: renders the traced column)."""
        return await self._call(
            workspace_path,
            OP_METADATA_LINEAGE,
            {
                "model": model_name,
                "column": column_name,
                "models_only": models_only,
            },
            gateway=gateway,
            operation="lineage.get_column",
        )

    async def query(
        self,
        workspace_path: str,
        sql: str,
        gateway: Optional[str] = None,
        row_limit: int = 0,
    ) -> Dict[str, Any]:
        """Ad-hoc SQL execution with the safe row limit applied in-sandbox."""
        from src.config import settings

        return await self._call(
            workspace_path,
            OP_QUERY,
            {
                "sql": sql,
                "row_limit": row_limit,
                "max_row_limit": settings.query_max_row_limit,
            },
            gateway=gateway,
            operation="query.execute",
        )

    async def evaluate(
        self,
        workspace_path: str,
        model: str,
        gateway: Optional[str] = None,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        execution_time: Optional[str] = None,
        limit: int = 1000,
    ) -> Dict[str, Any]:
        """Evaluate a model and return its rows (execute-class)."""
        return await self._call(
            workspace_path,
            OP_EVALUATE,
            {
                "model": model,
                "start": start,
                "end": end,
                "execution_time": execution_time,
                "limit": limit,
            },
            gateway=gateway,
            operation="command.evaluate",
        )

    async def render(
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
        """Render a model's SQL with macro expansion (execute-class)."""
        return await self._call(
            workspace_path,
            OP_RENDER,
            {
                "model": model_name,
                "start": start,
                "end": end,
                "execution_time": execution_time,
                "expand": expand,
                "pretty": pretty,
                "dialect": dialect,
            },
            gateway=gateway,
            operation="model.render",
        )

    async def test(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
        *,
        test: Optional[str] = None,
        verbosity: str = "default",
    ) -> Dict[str, Any]:
        """Run the project's unit tests, rich per-test result (execute-class)."""
        return await self._call(
            workspace_path,
            OP_TEST,
            {"detailed": True, "test": test, "verbosity": verbosity},
            gateway=gateway,
            operation="command.test",
        )

    async def table_diff(
        self,
        workspace_path: str,
        source: str,
        target: str,
        gateway: Optional[str] = None,
        *,
        on: Optional[str] = None,
        model_or_snapshot: Optional[str] = None,
        where: Optional[str] = None,
        temp_schema: Optional[str] = None,
        limit: int = 20,
    ) -> Optional[Dict[str, Any]]:
        """Differences between two environments' tables (execute-class).

        Returns None when there is nothing to compare (the endpoint's
        historical contract).
        """
        result = await self._call(
            workspace_path,
            OP_TABLE_DIFF,
            {
                "source": source,
                "target": target,
                "on": on,
                "model_or_snapshot": model_or_snapshot,
                "where": where,
                "temp_schema": temp_schema,
                "limit": limit,
            },
            gateway=gateway,
            operation="command.table_diff",
        )
        return result.get("result")

    async def validate(
        self, workspace_path: str, gateway: Optional[str] = None
    ) -> Dict[str, Any]:
        """Reload the project in the sandbox and report the verdict.

        Uses the frozen ``context.refresh`` op: a forced re-scan is exactly
        what validation is. The op failing IS the validation failing — its
        translated message becomes the verdict's error text.
        """
        try:
            await self._exchange(workspace_path, gateway, OP_CONTEXT_REFRESH, {})
            return {"valid": True, "errors": [], "warnings": []}
        except ServiceError as e:
            error_text = (e.detail if isinstance(e.detail, str) and e.detail else e.message)
            error_text = (error_text or "").strip() or e.code.value
            logger.warning(
                "validation_failed", error=error_text, workspace_path=workspace_path
            )
            return {"valid": False, "errors": [error_text], "warnings": []}

    # ── internals ────────────────────────────────────────────────────────

    async def _call(
        self,
        workspace_path: str,
        op: str,
        params: Dict[str, Any],
        *,
        gateway: Optional[str],
        operation: str,
    ) -> Dict[str, Any]:
        """One sandbox op, through the cache when the op is cacheable."""
        cache_key = ""
        if _cacheable(op, params):
            cache_key = self._cache_key(workspace_path, gateway, op, params)
            cached = self._cache.get(cache_key)
            if cached is not None:
                age = time.monotonic() - cached.fetched_at
                if age <= self._ttl and workspace_path not in self._dirty:
                    return cached.payload

        result = await self._exchange(workspace_path, gateway, op, params)
        if cache_key:
            self._cache[cache_key] = _CachedPayload(result)
            self._dirty.discard(workspace_path)
        return result

    async def _exchange(
        self,
        workspace_path: str,
        gateway: Optional[str],
        op: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Call the executor manager and translate protocol failures."""
        from src.workspace.identity import resolve_workspace_identity

        identity = resolve_workspace_identity(workspace_path)
        try:
            return await get_executor_manager().call(
                workspace_path,
                identity.environment,
                op,
                params,
                gateway=gateway or "",
            )
        except ExecutorError as exc:
            raise self._translate_error(
                exc, workspace_path, operation=op, gateway=gateway
            ) from exc

    def _translate(
        self,
        exc: Exception,
        workspace_path: str,
        *,
        operation: str,
        gateway: Optional[str],
        context: Optional[Dict[str, Any]] = None,
    ) -> ServiceError:
        """Wrap an exception into the service error the REST layer expects."""
        if isinstance(exc, ServiceError):
            return exc
        return service_error_from_exception(
            exc,
            code=ErrorCode.INTERNAL_ERROR,
            fallback_message=f"operation failed: {operation}",
            archive=ErrorArchiveContext(
                workspace_path=workspace_path,
                operation=operation,
                gateway=gateway,
                context=context,
            ),
        )

    def _translate_error(
        self,
        exc: ExecutorError,
        workspace_path: str,
        *,
        operation: str,
        gateway: Optional[str],
    ) -> ServiceError:
        """Map an executor op failure onto the API's error contract.

        ``model_not_found`` keeps its 404; lint-style structured payloads ride
        through untouched; everything else is archived as an internal error.
        """
        if exc.code == "model_not_found":
            return ModelNotFoundError(str(exc).removeprefix("model not found: "))
        if exc.code == "lint_failed":
            # Structured lint results travel inside the error payload; keep
            # them on the ServiceError for the REST layer's 422 handling.
            svc = ServiceError(
                code=ErrorCode.PLAN_FAILED,
                message=exc.message,
                detail=exc.detail,
                status_code=422,
            )
            return svc
        return service_error_from_exception(
            exc,
            code=ErrorCode.INTERNAL_ERROR,
            fallback_message=f"sandbox operation failed: {operation}",
            archive=ErrorArchiveContext(
                workspace_path=workspace_path,
                operation=operation,
                gateway=gateway,
                context={"executor_code": exc.code},
            ),
        )

    @staticmethod
    def _cache_key(
        workspace_path: str,
        gateway: Optional[str],
        op: str,
        params: Dict[str, Any],
    ) -> str:
        # "j:" prefix keeps workspace paths from colliding with the
        # invalidate_under_path prefix scan (which matches on raw path).
        stable = json.dumps(params, sort_keys=True, default=str)
        return f"j:{workspace_path}:{gateway or ''}:{op}:{stable}"


# ── singleton ───────────────────────────────────────────────────────────────

_store: Optional[MetadataContextStore] = None


def get_metadata_context() -> MetadataContextStore:
    """Process-wide MetadataContext singleton."""
    global _store
    if _store is None:
        from src.config import settings

        ttl = float(getattr(settings, "metadata_cache_ttl_seconds", _DEFAULT_TTL_SECONDS))
        _store = MetadataContextStore(ttl_seconds=ttl)
    return _store
