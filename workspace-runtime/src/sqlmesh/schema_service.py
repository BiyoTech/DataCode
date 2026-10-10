"""Schema discovery service - catalog/schema/table introspection.

discovery builds ONE gateway's engine adapter from the parsed
``config.yaml`` (:mod:`src.sqlmesh.config_access`) instead of loading a
Context. The old path — ``Context(load=True)`` — execs the user's Python
models/macros in the platform process (threat T2) just to reach an
introspection connection; the adapter needs neither the models nor the state
DB, so it is built directly.
"""

import asyncio
from typing import Any, Dict, Optional

from src.sqlmesh.config_access import engine_adapter_for
from src.sqlmesh.format import format_schema_tables
from src.utils.error_archive import ErrorArchiveContext
from src.utils.errors import ErrorCode, service_error_from_exception
from src.utils.logging import get_logger

logger = get_logger(__name__)


class SchemaService:
    """Schema discovery operations using a config-built engine adapter."""

    async def discover(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
        schema_name: str = "",
        table_name: str = "",
    ) -> Dict[str, Any]:
        """Discover table schemas from the data source.

        Args:
            workspace_path: Target workspace path.
            gateway: Optional gateway override.
            schema_name: Filter by schema name.
            table_name: Filter by specific table within schema.
        """
        try:
            results = await asyncio.to_thread(
                self._discover_sync, workspace_path, gateway, schema_name, table_name
            )
            return format_schema_tables(results)
        except Exception as e:
            logger.error("schema_discovery_failed", error=str(e), workspace_path=workspace_path)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Schema discovery failed",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="schema.discovery",
                    gateway=gateway,
                    context={"schema_name": schema_name, "table_name": table_name},
                ),
            ) from e

    def _discover_sync(
        self,
        workspace_path: str,
        gateway: Optional[str],
        schema_name: str,
        table_name: str,
    ) -> list:
        """Synchronous schema discovery (runs in thread)."""
        from sqlglot import exp

        adapter = engine_adapter_for(workspace_path, gateway)
        try:
            results = []

            if table_name and schema_name:
                # Specific table
                table_ref = exp.table_(table_name, db=schema_name)
                columns = adapter.columns(table_ref)
                results.append({
                    "schema_name": schema_name,
                    "table_name": table_name,
                    "columns": [{"name": n, "data_type": str(t)} for n, t in columns.items()],
                })
            elif schema_name:
                # All tables in schema
                data_objects = adapter.get_data_objects(schema_name)
                for obj in data_objects:
                    try:
                        columns = adapter.columns(obj.to_table())
                        results.append({
                            "schema_name": obj.schema_name,
                            "table_name": obj.name,
                            "columns": [
                                {"name": n, "data_type": str(t)}
                                for n, t in columns.items()
                            ],
                        })
                    except Exception:
                        # Skip tables that fail introspection
                        pass

            return results
        finally:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


# Singleton
_schema_service: Optional[SchemaService] = None


def get_schema_service() -> SchemaService:
    global _schema_service
    if _schema_service is None:
        _schema_service = SchemaService()
    return _schema_service
