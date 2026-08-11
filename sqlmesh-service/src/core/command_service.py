"""Command service - evaluate, test, fetchdf, table_diff operations.

Matches SQLMesh web server's commands and table_diff endpoints.
"""

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlglot import exp

from src.core.context_pool import get_context_pool
from src.core.format import format_query_result, format_table_diff, format_test_result
from src.utils.errors import (
    ErrorCode,
    ModelNotFoundError,
    service_error_from_exception,
)
from src.utils.error_archive import ErrorArchiveContext
from src.utils.logging import get_logger

logger = get_logger(__name__)


class CommandService:
    """SQLMesh command operations: evaluate, test, table_diff, fetchdf."""

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
        """Evaluate a model and return results.

        Matches SQLMesh web server's POST /commands/evaluate behavior.
        Returns Arrow-streaming compatible data as JSON.

        External models (schema-only stubs) are queried directly against
        their physical table instead of going through context.evaluate(),
        which would render CAST(NULL AS ...) for every column.
        """
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)
        await pool.refresh_if_dirty(entry)

        try:
            import pandas as pd

            start_time = time.time()

            # External models are schema-only stubs whose render_query()
            # generates CAST(NULL AS ...) placeholders. Query the real
            # physical table directly via the engine adapter instead.
            m = entry.context.get_model(model, raise_if_missing=False)
            if m is not None and getattr(m, 'source_type', None) == 'external':
                table = exp.to_table(m.fqn)
                query = exp.select("*").from_(table, copy=False).limit(limit)
                logger.info(
                    "evaluating_external_model_direct_query",
                    model=model,
                    fqn=m.fqn,
                )
                df = entry.context.engine_adapter.fetchdf(query)
            else:
                df = await asyncio.to_thread(
                    entry.context.evaluate,
                    model,
                    start=start,
                    end=end,
                    execution_time=execution_time,
                    limit=limit,
                )
            execution_time_ms = int((time.time() - start_time) * 1000)

            if not isinstance(df, pd.DataFrame):
                df = df.toPandas()

            # Replace NaN/None for JSON serialization
            df = df.replace({pd.NA: None}).where(df.notna(), None)
            columns = list(df.columns)
            rows = [
                [None if pd.isna(v) else str(v) if not isinstance(v, (int, float, bool)) else v for v in row]
                for _, row in df.iterrows()
            ]

            logger.info(
                "model_evaluated",
                model=model,
                row_count=len(rows),
                execution_time_ms=execution_time_ms,
            )
            return format_query_result(columns, rows, execution_time_ms)

        except Exception as e:
            # Evaluation errors are typically SQL/macro problems in the model
            # (a SQLMeshError). Surface the raw reason so clients see the exact
            # failing SQL/expression rather than a generic wrapper.
            logger.error("evaluate_failed", error=str(e), model=model)
            raise service_error_from_exception(
                e,
                code=ErrorCode.QUERY_FAILED,
                fallback_message=f"Failed to evaluate model: {model}",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="command.evaluate",
                    gateway=gateway,
                    model_fqn=model,
                    context={"start": start, "end": end, "limit": limit},
                ),
            ) from e

    async def test(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
        *,
        test: Optional[str] = None,
        verbosity: str = "default",
    ) -> Dict[str, Any]:
        """Run model tests.

        Matches SQLMesh web server's GET /commands/test behavior.
        """
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)
        await pool.refresh_if_dirty(entry)

        try:
            from sqlmesh.core.console import Verbosity

            verbosity_enum = Verbosity.DEFAULT
            if verbosity == "verbose":
                verbosity_enum = Verbosity.VERBOSE
            elif verbosity == "quiet":
                verbosity_enum = Verbosity.QUIET

            # Test files live inside the SQLMesh project (sql-warehouse/).
            from src.workspace.layout import sqlmesh_root

            test_paths = (
                [str(Path(sqlmesh_root(workspace_path)) / Path(test))] if test else None
            )

            result = await asyncio.to_thread(
                entry.context.test,
                tests=test_paths,
                verbosity=verbosity_enum,
            )

            logger.info(
                "tests_completed",
                workspace_path=workspace_path,
                tests_run=result.testsRun,
            )
            return format_test_result(result, context_path=workspace_path)

        except Exception as e:
            logger.error("test_failed", error=str(e), workspace_path=workspace_path)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to run tests",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="command.test",
                    gateway=gateway,
                    context={"test": test, "verbosity": verbosity},
                ),
            ) from e

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
        """Calculate differences between tables.

        Matches SQLMesh web server's GET /table_diff behavior.
        """
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)
        await pool.refresh_if_dirty(entry)

        try:
            result = await asyncio.to_thread(
                format_table_diff,
                entry.context,
                source=source,
                target=target,
                on=on or "",
                model_or_snapshot=model_or_snapshot or "",
                where=where or "",
                temp_schema=temp_schema or "",
                limit=limit,
            )
            return result
        except Exception as e:
            logger.error("table_diff_failed", error=str(e), source=source, target=target)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to compute table diff",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="command.table_diff",
                    gateway=gateway,
                    context={
                        "source": source,
                        "target": target,
                        "model_or_snapshot": model_or_snapshot,
                    },
                ),
            ) from e


# Singleton
_command_service: Optional[CommandService] = None


def get_command_service() -> CommandService:
    global _command_service
    if _command_service is None:
        _command_service = CommandService()
    return _command_service
