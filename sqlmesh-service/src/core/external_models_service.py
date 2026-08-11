"""External models service — gateway listing, schema discovery, and YAML import.

Ports the core logic from agent-service's build_external_models.py, running
directly inside sqlmesh-service via SQLMesh Context (no subprocess).
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from src.core.context_pool import get_context_pool, load_workspace_config, ping_gateway
from src.utils.errors import ErrorCode, ServiceError, service_error_from_exception
from src.utils.error_archive import ErrorArchiveContext
from src.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants (mirrored from build_external_models.py)
# ---------------------------------------------------------------------------
DEFAULT_SAMPLES = 3
MAX_SAMPLE_VALUE_LENGTH = 64

UNSAMPLEABLE_TYPE_PREFIXES: Set[str] = {
    "BLOB", "TEXT", "LONGTEXT", "MEDIUMTEXT", "TINYTEXT",
    "JSON", "JSONB", "GEOMETRY", "GEOGRAPHY",
    "BYTEA", "BYTE", "XML", "VARBINARY", "BINARY",
}


# ======================================================================
# Public API
# ======================================================================

class ExternalModelsService:
    """Import external table schemas as SQLMesh external model YAML files."""

    # ------------------------------------------------------------------
    # list_gateways
    # ------------------------------------------------------------------
    async def list_gateways(self, workspace_path: str) -> List[Dict[str, Any]]:
        """List all gateways configured in the workspace config.yaml.

        Reads the SQLMesh ``config.yaml`` directly (no ``Context`` creation,
        no database connection). Listing gateways only needs the parsed config
        (gateway names + dialect), so it must never depend on the target
        warehouse being reachable — otherwise opening a workspace whose gateway
        DB is down (or a freshly created, unconfigured workspace) would fail.
        """
        try:
            config = await asyncio.to_thread(load_workspace_config, workspace_path)
            return self._list_gateways_sync(config)
        except Exception as e:
            logger.error("list_gateways_failed", error=str(e))
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to list gateways",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="external_models.list_gateways",
                ),
            ) from e

    def _list_gateways_sync(self, config) -> List[Dict[str, Any]]:
        """Synchronous gateway listing from a parsed SQLMesh ``Config``.

        Each gateway entry includes a ``dialect`` field (e.g. ``"postgres"``,
        ``"mysql"``, ``"duckdb"``) so the frontend can render a dialect-aware
        import cascade — PostgreSQL needs 3 levels (database → schema → table)
        while MySQL needs only 2 (database → table). The dialect is read from
        the gateway's connection config class variable ``DIALECT``.
        """
        gateways = []
        default_gw = config.default_gateway_name or ""
        for gw_name in config.gateways:
            gw_config = config.gateways[gw_name]
            dialect = ""
            if gw_config and gw_config.connection:
                # ``DIALECT`` is a ClassVar on every connection config class
                # (e.g. PostgresConnectionConfig.DIALECT == "postgres").
                dialect = getattr(type(gw_config.connection), "DIALECT", "") or ""
            gateways.append({
                "name": gw_name if gw_name else "(default)",
                "is_default": gw_name == default_gw,
                "dialect": dialect.lower(),
            })
        if not gateways and default_gw:
            gw_config = config.gateways.get(default_gw)
            dialect = ""
            if gw_config and gw_config.connection:
                dialect = getattr(type(gw_config.connection), "DIALECT", "") or ""
            gateways.append({
                "name": default_gw,
                "is_default": True,
                "dialect": dialect.lower(),
            })
        return gateways

    # ------------------------------------------------------------------
    # ping_gateway
    # ------------------------------------------------------------------
    async def ping_gateway(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        """Probe a gateway's warehouse connectivity with a short timeout.

        Returns ``{"ok": True}`` when reachable, or ``{"ok": False,
        "error": "<reason>"}`` when not. This NEVER raises for a connectivity
        failure — the frontend uses the boolean to show a local "gateway
        connection error" hint rather than a global error toast. A short
        overall timeout guarantees fast failure instead of the ~20s full
        context-load path.
        """
        try:
            await asyncio.wait_for(
                asyncio.to_thread(ping_gateway, workspace_path, gateway),
                timeout=timeout,
            )
            return {"ok": True}
        except asyncio.TimeoutError:
            logger.warning("gateway_ping_timeout", gateway=gateway, timeout=timeout)
            return {"ok": False, "error": f"Connection timed out after {timeout:g}s"}
        except Exception as e:
            logger.warning("gateway_ping_failed", gateway=gateway, error=str(e))
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------
    # list_schemas
    # ------------------------------------------------------------------
    async def list_schemas(
        self,
        workspace_path: str,
        gateway: Optional[str] = None,
        database: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List databases/schemas accessible through a gateway.

        Two modes (dialect-aware):

        * **Level-1** (``database`` is None): returns the list of databases
          reachable from the gateway. For MySQL this is ``SHOW DATABASES``;
          for Postgres this is ``pg_database``; for others a generic
          ``SHOW SCHEMAS / SHOW DATABASES`` fallback.

        * **Level-2** (``database`` provided, Postgres family only): returns
          the list of schemas **within** the given database, via
          ``information_schema.schemata``. This enables the 3-level cascade
          (database → schema → tables) for PostgreSQL / Redshift / Greenplum.
          MySQL family ignores ``database`` (it has no schema sub-level).
        """
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)

        try:
            return await asyncio.to_thread(self._list_schemas_sync, entry.context, database)
        except Exception as e:
            logger.error("list_schemas_failed", error=str(e), gateway=gateway, database=database)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to list schemas",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="external_models.list_schemas",
                    gateway=gateway,
                    context={"database": database},
                ),
            ) from e

    def _list_schemas_sync(
        self, context, database: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Synchronous schema listing via engine-adapter.

        When ``database`` is provided and the dialect is Postgres-family,
        lists schemas within that database (level-2). Otherwise lists
        databases (level-1).
        """
        adapter = context.engine_adapter
        dialect = (adapter.dialect or "").lower()

        # Postgres 3-level: when a database is selected, list its schemas.
        if database and self._is_postgres_family(dialect):
            return self._list_schemas_within_db_postgres(adapter, database)

        # Level-1: list databases.
        if self._is_mysql_family(dialect):
            return self._list_schemas_mysql(adapter)
        if self._is_postgres_family(dialect):
            return self._list_schemas_postgres(adapter)

        # Generic fallback: try SHOW SCHEMAS / SHOW DATABASES
        for sql in ("SHOW SCHEMAS", "SHOW DATABASES"):
            try:
                rows = adapter.fetchall(sql)
                return [{"name": str(r[0])} for r in rows if r[0]]
            except Exception:
                continue
        return []

    @staticmethod
    def _list_schemas_within_db_postgres(adapter, database: str) -> List[Dict[str, Any]]:
        """List schemas within a specific Postgres database (level-2).

        Queries ``information_schema.schemata`` and filters out system schemas
        (``pg_*``, ``information_schema``). Assumes the gateway's connection
        is already established to ``database`` — Postgres does not support
        cross-database queries in a single connection.
        """
        sql = (
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT LIKE 'pg\\_%' "
            "  AND schema_name <> 'information_schema' "
            "ORDER BY schema_name"
        )
        try:
            rows = adapter.fetchall(sql)
        except Exception as e:
            logger.warning(
                "list_schemas_within_db_failed", database=database, error=str(e)
            )
            return []
        return [{"name": str(r[0])} for r in rows if r[0]]

    @staticmethod
    def _list_schemas_mysql(adapter) -> List[Dict[str, Any]]:
        rows = adapter.fetchall("SHOW DATABASES")
        # Filter out system databases
        exclude = {"information_schema", "mysql", "performance_schema", "sys"}
        return [{"name": str(r[0])} for r in rows if str(r[0]).lower() not in exclude]

    @staticmethod
    def _list_schemas_postgres(adapter) -> List[Dict[str, Any]]:
        sql = (
            "SELECT datname FROM pg_database "
            "WHERE datistemplate = false AND datallowconn = true "
            "ORDER BY datname"
        )
        rows = adapter.fetchall(sql)
        return [{"name": str(r[0])} for r in rows if r[0]]

    # ------------------------------------------------------------------
    # list_tables (reuses SchemaService._discover_sync pattern)
    # ------------------------------------------------------------------
    async def list_tables(
        self, workspace_path: str, gateway: Optional[str], schema: str,
    ) -> List[Dict[str, Any]]:
        """List all tables/views in a schema."""
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)

        try:
            return await asyncio.to_thread(self._list_tables_sync, entry.context, schema)
        except Exception as e:
            logger.error("list_tables_failed", error=str(e), schema=schema)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to list tables",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="external_models.list_tables",
                    gateway=gateway,
                    context={"schema": schema},
                ),
            ) from e

    @staticmethod
    def _list_tables_sync(context, schema: str) -> List[Dict[str, Any]]:
        """Synchronous table listing."""
        from sqlmesh.core.engine_adapter.shared import DataObjectType

        adapter = context.engine_adapter
        data_objects = adapter.get_data_objects(schema)
        results = []
        for obj in data_objects:
            if obj.type in {DataObjectType.TABLE, DataObjectType.VIEW}:
                results.append({
                    "schema_name": schema,
                    "table_name": obj.name,
                })
        return results

    # ------------------------------------------------------------------
    # import_external_models — core YAML generation logic
    # ------------------------------------------------------------------
    async def import_external_models(
        self,
        workspace_path: str,
        gateway: Optional[str],
        schema: str,
        tables: Optional[List[str]] = None,
        include: str = "",
        exclude: str = "",
        samples: int = DEFAULT_SAMPLES,
        strict: bool = False,
    ) -> Dict[str, Any]:
        """Import external table schemas and write YAML files.

        Mirrors build_external_models.py's build_external_models() function.
        """
        pool = get_context_pool()
        entry = await pool.get_context(workspace_path, gateway)

        try:
            result = await asyncio.to_thread(
                self._import_sync,
                entry.context, workspace_path, gateway, schema,
                tables, include, exclude, samples, strict,
            )
            # Newly written external-model YAML files are not tracked by the
            # cached context's incremental refresh (reload_needed only watches
            # files present at last load). Mark all gateway contexts dirty so
            # the next model listing performs a full reload and sees them.
            if result.get("success_count", 0) > 0:
                await pool.mark_dirty_all_for_workspace(workspace_path)
            return result
        except Exception as e:
            logger.error("import_external_models_failed", error=str(e), schema=schema)
            raise service_error_from_exception(
                e,
                code=ErrorCode.INTERNAL_ERROR,
                fallback_message="Failed to import external models",
                archive=ErrorArchiveContext(
                    workspace_path=workspace_path,
                    operation="external_models.import",
                    gateway=gateway,
                    context={"schema": schema, "tables": tables},
                ),
            ) from e

    def _import_sync(
        self, context, workspace_path: str, gateway: Optional[str],
        schema: str, tables: Optional[List[str]], include: str,
        exclude: str, samples: int, strict: bool,
    ) -> Dict[str, Any]:
        """Synchronous import logic."""
        # trunk-ignore(import-error)
        from sqlmesh.utils.yaml import dump as yaml_dump  # trunk-ignore: TID253

        # External models live inside the SQLMesh project (sql-warehouse/),
        # not the workspace root, so resolve the SQLMesh sub-directory.
        from src.workspace.layout import sqlmesh_root

        ws_path = Path(os.path.expanduser(sqlmesh_root(workspace_path))).resolve()
        adapter = context.engine_adapter
        dialect = self._detect_dialect(adapter)
        effective_gateway = gateway or context.selected_gateway

        # --- Discover tables ---
        all_tables = self._list_tables_sync(context, schema)
        all_names = [t["table_name"] for t in all_tables]

        # Apply explicit table list filter
        if tables and len(tables) > 0:
            table_set = set(tables)
            filtered_names = [n for n in all_names if n in table_set]
        else:
            filtered_names = list(all_names)

        # Apply include/exclude glob patterns
        inc_patterns = self._parse_patterns(include)
        exc_patterns = self._parse_patterns(exclude)
        filtered_names = self._filter_tables(filtered_names, inc_patterns, exc_patterns)

        if not filtered_names:
            return {"success": True, "written_files": [], "success_count": 0, "skipped_count": len(all_names)}

        logger.info(
            "importing_external_models", schema=schema, candidate_count=len(filtered_names),
            gateway=effective_gateway,
        )

        # --- Process each table ---
        out_base = ws_path / "external_models" / schema
        out_base.mkdir(parents=True, exist_ok=True)

        written_files: List[str] = []
        success_count = 0
        skip_count = 0

        for table_name in sorted(filtered_names):
            info = self._process_table(
                adapter, schema, table_name, effective_gateway, dialect, samples,
            )

            if info is None:
                if strict:
                    raise ServiceError(
                        code=ErrorCode.INTERNAL_ERROR,
                        message=f"Strict mode: failed to process table {schema}.{table_name}",
                    )
                skip_count += 1
                continue

            # Build YAML
            yaml_content = self._build_yaml(
                schema, table_name, effective_gateway, info["columns"],
                dialect, info.get("column_descriptions"),
            )

            # Write file
            out_file = out_base / f"{table_name}.yaml"
            out_file.write_text(yaml_content, encoding="utf-8")
            written_files.append(str(out_file.relative_to(ws_path)))
            success_count += 1

        logger.info(
            "import_complete", schema=schema, success=success_count, skipped=skip_count,
        )
        return {
            "success": True,
            "written_files": written_files,
            "success_count": success_count,
            "skipped_count": skip_count,
        }

    # ==================================================================
    # Internal helpers (mirrored from build_external_models.py)
    # ==================================================================

    # --- Filtering ---
    @staticmethod
    def _parse_patterns(raw: str) -> List[str]:
        if not raw:
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]

    @staticmethod
    def _matches_any(name: str, patterns: List[str]) -> bool:
        lower = name.lower()
        return any(fnmatch.fnmatch(lower, p.lower()) for p in patterns)

    @staticmethod
    def _filter_tables(all_names: List[str], include: List[str], exclude: List[str]) -> List[str]:
        result = all_names
        if include:
            result = [n for n in result if ExternalModelsService._matches_any(n, include)]
        if exclude:
            result = [n for n in result if not ExternalModelsService._matches_any(n, exclude)]
        return result

    # --- Dialect ---
    @staticmethod
    def _detect_dialect(adapter) -> str:
        return (adapter.dialect or "").lower()

    @staticmethod
    def _is_mysql_family(dialect: str) -> bool:
        return dialect in {"mysql", "doris", "starrocks", "tidb", "mariadb"}

    @staticmethod
    def _is_postgres_family(dialect: str) -> bool:
        return dialect in {"postgres", "postgresql", "redshift", "greenplum"}

    # --- Column comments ---
    def _fetch_comments(
        self, adapter, dialect: str, schema: str, table: str,
    ) -> Dict[str, str]:
        if self._is_mysql_family(dialect):
            return self._comments_mysql(adapter, schema, table)
        if self._is_postgres_family(dialect):
            return self._comments_postgres(adapter, schema, table)
        return {}

    @staticmethod
    def _comments_mysql(adapter, schema: str, table: str) -> Dict[str, str]:
        safe_schema = schema.replace("'", "''")
        safe_table = table.replace("'", "''")
        sql = (
            f"SELECT COLUMN_NAME, COLUMN_COMMENT "
            f"FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = '{safe_schema}' AND TABLE_NAME = '{safe_table}' "
            f"ORDER BY ORDINAL_POSITION"
        )
        try:
            rows = adapter.fetchall(sql)
        except Exception:
            return {}
        result: Dict[str, str] = {}
        for row in rows:
            name, comment = row[0], row[1] or ""
            if name and comment:
                result[str(name)] = str(comment)
        return result

    @staticmethod
    def _comments_postgres(adapter, schema: str, table: str) -> Dict[str, str]:
        safe_schema = schema.replace("'", "''")
        safe_table = table.replace("'", "''")
        sql = (
            f"SELECT a.attname, pg_catalog.col_description(a.attrelid, a.attnum) "
            f"FROM pg_catalog.pg_attribute a "
            f"JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            f"JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = '{safe_schema}' AND c.relname = '{safe_table}' "
            f"  AND a.attnum > 0 AND NOT a.attisdropped "
            f"ORDER BY a.attnum"
        )
        try:
            rows = adapter.fetchall(sql)
        except Exception:
            return {}
        result: Dict[str, str] = {}
        for row in rows:
            name, comment = row[0], row[1] or ""
            if name and comment:
                result[str(name)] = str(comment)
        return result

    # --- Sample values ---
    @staticmethod
    def _is_sampleable(sqlglot_type) -> bool:
        type_str = str(sqlglot_type).upper().lstrip("_")
        for prefix in UNSAMPLEABLE_TYPE_PREFIXES:
            if type_str.startswith(prefix.upper()):
                return False
        return True

    def _fetch_samples(
        self, adapter, dialect: str, schema: str, table: str,
        column_name: str, sqlglot_type, limit: int,
    ) -> List[str]:
        if not self._is_sampleable(sqlglot_type):
            return []

        # trunk-ignore(import-error)
        from sqlglot import exp as sqlglot_exp  # trunk-ignore: TID253
        try:
            col = sqlglot_exp.to_identifier(column_name)
            rendered_col = col.sql(dialect=dialect)
            tbl = sqlglot_exp.table_(table, db=schema)
            rendered_tbl = tbl.sql(dialect=dialect)
        except Exception:
            rendered_col = f"`{column_name}`"
            rendered_tbl = f"`{schema}`.`{table}`"

        sql = (
            f"SELECT DISTINCT {rendered_col} FROM {rendered_tbl} "
            f"WHERE {rendered_col} IS NOT NULL LIMIT {int(limit)}"
        )
        try:
            rows = adapter.fetchall(sql)
        except Exception:
            return []

        samples: List[str] = []
        for row in rows:
            val = row[0]
            if val is None:
                continue
            s = str(val)
            if len(s) > MAX_SAMPLE_VALUE_LENGTH:
                s = s[:MAX_SAMPLE_VALUE_LENGTH] + "\u2026"
            samples.append(s)
        return samples

    # --- Column descriptions ---
    @staticmethod
    def _sanitize(value: str) -> str:
        sanitized = value.replace("\n", " ").replace("\r", " ").replace("\t", " ")
        while "  " in sanitized:
            sanitized = sanitized.replace("  ", " ")
        return sanitized.strip().strip("\"'").strip()

    def _build_descriptions(
        self, columns: Dict[str, Any], comments: Dict[str, str],
        samples_map: Dict[str, List[str]],
    ) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for col_name in columns:
            parts: List[str] = []
            orig = (comments.get(col_name) or "").strip()
            if orig:
                parts.append(self._sanitize(orig))
            vals = samples_map.get(col_name) or []
            if vals:
                sample_text = "e.g. " + ", ".join(self._sanitize(s) for s in vals)
                parts.append(sample_text)
            if parts:
                result[col_name] = ". ".join(parts) if len(parts) > 1 else parts[0]
        return result

    # --- YAML assembly ---
    @staticmethod
    def _build_yaml(
        schema: str, table: str, gateway: str,
        columns: Dict[str, Any], dialect: str,
        column_descriptions: Optional[Dict[str, str]] = None,
    ) -> str:
        # trunk-ignore(import-error)
        from sqlmesh.utils.yaml import dump as yaml_dump  # trunk-ignore: TID253
        rendered: Dict[str, str] = {}
        for col_name, col_type in columns.items():
            try:
                rendered[col_name] = col_type.sql(dialect=dialect)
            except Exception:
                rendered[col_name] = str(col_type)
        model: Dict[str, Any] = {
            "name": f"{schema}.{table}",
            "columns": rendered,
        }
        if gateway:
            model["gateway"] = gateway
        if column_descriptions:
            model["column_descriptions"] = column_descriptions
        return yaml_dump([model])

    # --- Single-table processing ---
    def _process_table(
        self, adapter, schema: str, table: str, gateway: str,
        dialect: str, samples: int,
    ) -> Optional[Dict[str, Any]]:
        # trunk-ignore(import-error)
        from sqlglot import exp as sqlglot_exp  # trunk-ignore: TID253

        try:
            table_ref = sqlglot_exp.table_(table, db=schema)
            columns: Dict[str, Any] = adapter.columns(table_ref)
        except Exception:
            logger.warning("table_introspect_failed", table=f"{schema}.{table}")
            return None

        if not columns:
            return None

        comments = self._fetch_comments(adapter, dialect, schema, table)

        samples_map: Dict[str, List[str]] = {}
        for col_name, col_type in columns.items():
            vals = self._fetch_samples(adapter, dialect, schema, table, col_name, col_type, samples)
            if vals:
                samples_map[col_name] = vals

        descriptions = self._build_descriptions(columns, comments, samples_map)

        return {
            "columns": columns,
            "column_descriptions": descriptions if descriptions else None,
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_external_models_service: Optional[ExternalModelsService] = None


def get_external_models_service() -> ExternalModelsService:
    global _external_models_service
    if _external_models_service is None:
        _external_models_service = ExternalModelsService()
    return _external_models_service
