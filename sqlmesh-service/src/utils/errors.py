"""Structured error definitions for SQLMesh Service.

Provides typed exceptions with error codes for consistent API error responses.
"""

import os
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.utils.error_archive import ErrorArchiveContext

# ``SQLMeshError`` is the common base for every error SQLMesh raises for a
# USER-FACING, actionable problem (e.g. ``PlanError``, ``ConfigError``,
# ``MacroEvalError``). We import it once at module load so error translation
# can distinguish "the user's project/plan is wrong" (surface the raw message,
# like the native CLI does) from "the server itself blew up" (hide behind a
# generic message, keep the detail for diagnostics). The guard keeps us working
# on SQLMesh builds that might rename/relocate the class.
try:
    from sqlmesh.utils.errors import SQLMeshError as _SQLMeshErrorCls
except ImportError:  # pragma: no cover - defensive against SQLMesh changes
    _SQLMeshErrorCls = None  # type: ignore[assignment]


class ErrorCode(str, Enum):
    """Enumerated error codes for structured API responses."""
    CONTEXT_LOAD_FAILED = "CONTEXT_LOAD_FAILED"
    CONTEXT_CONFIG_INVALID = "CONTEXT_CONFIG_INVALID"
    CONTEXT_REFRESH_FAILED = "CONTEXT_REFRESH_FAILED"
    PLAN_FAILED = "PLAN_FAILED"
    APPLY_FAILED = "APPLY_FAILED"
    QUERY_FAILED = "QUERY_FAILED"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    WORKSPACE_PATH_INVALID = "WORKSPACE_PATH_INVALID"
    INVALID_REQUEST = "INVALID_REQUEST"
    TIMEOUT = "TIMEOUT"
    CONFLICT = "CONFLICT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    # Database-layer errors. These are raised by DB drivers (psycopg2,
    # sqlite3, etc.) and are typically USER/DBA-fixable (e.g. a Postgres
    # table missing REPLICA IDENTITY, a publication misconfiguration, a
    # connection refused). We surface them as actionable 4xx/5xx rather
    # than opaque 500s so the frontend can show the real reason.
    DB_ENGINE_ERROR = "DB_ENGINE_ERROR"
    DB_CONNECTION_ERROR = "DB_CONNECTION_ERROR"
    DB_TRANSACTION_ERROR = "DB_TRANSACTION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ServiceError(Exception):
    """Base exception for all service errors."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        detail: Optional[str] = None,
        status_code: int = 500,
    ):
        self.code = code
        self.message = message
        self.detail = detail
        self.status_code = status_code
        # Populated by ``service_error_from_exception`` (via ErrorArchive)
        # when the error is persisted to ``{workspace}/errors/``. The value
        # is the JSON file's ``error_id``, which the frontend / agent can use
        # to fetch the full error context. Remains ``None`` when archiving is
        # disabled or the write failed.
        self.error_log_id: Optional[str] = None
        super().__init__(message)

    def to_dict(self) -> dict:
        result = {"error_code": self.code.value, "message": self.message}
        if self.detail:
            result["detail"] = self.detail
        return result

    def to_response_dict(self) -> dict:
        """Public response shape with top-level error fields + structured detail.

        Mirrors FastAPI's ``HTTPException`` body so the frontend can read
        ``error_code``/``message``/``detail`` uniformly. When ``detail`` is a
        dict (e.g. ``PlanLintError`` carrying ``lint_results``), it is
        inlined as-is; otherwise it is wrapped under ``detail`` as a string.
        """
        out: dict = {
            "error_code": self.code.value,
            "message": self.message,
        }
        if self.error_log_id:
            out["error_log_id"] = self.error_log_id
        if isinstance(self.detail, dict):
            # Inline the structured payload so the frontend does not have to
            # reach into a nested ``detail`` object. Preserve the inlined
            # summary under ``detail_message`` when it differs, but keep the
            # authoritative top-level ``message`` intact (do not let a nested
            # ``message`` key clobber it).
            inlined = dict(self.detail)
            nested_message = inlined.pop("message", None)
            out.update(inlined)
            out["message"] = self.message
            if nested_message and nested_message != self.message:
                out["detail_message"] = nested_message
        elif self.detail is not None:
            out["detail"] = self.detail
        return out


class ContextLoadError(ServiceError):
    def __init__(self, workspace_path: str, detail: Optional[str] = None):
        super().__init__(
            code=ErrorCode.CONTEXT_LOAD_FAILED,
            message=f"Failed to load context for workspace: {workspace_path}",
            detail=detail,
            status_code=500,
        )


class ContextRefreshError(ServiceError):
    """Raised when refreshing a cached SQLMesh context fails.

    Previously this path swallowed the exception and only marked the entry
    as invalid, which caused subsequent operations to run against an
    aborted transaction (e.g. ``InFailedSqlTransaction``). Surfacing the
    failure immediately lets the caller return a clean, actionable error
    instead of a confusing downstream failure.
    """

    def __init__(self, workspace_path: str, detail: Optional[str] = None):
        super().__init__(
            code=ErrorCode.CONTEXT_REFRESH_FAILED,
            message=f"Failed to refresh context for workspace: {workspace_path}",
            detail=detail,
            status_code=500,
        )


class ContextConfigError(ServiceError):
    """Raised when a workspace's SQLMesh config.yaml cannot be loaded.

    Typically a client-fixable problem (e.g. a gateway with missing connection
    credentials), so it maps to 422 rather than a raw 500. The public message
    references only the workspace name; the underlying validation reason is
    passed as ``detail`` (SQLMesh config errors report offending field names,
    not secret values) and the full error is logged server-side.
    """

    def __init__(self, workspace_path: str, detail: Optional[str] = None):
        name = os.path.basename(os.path.normpath(workspace_path)) or workspace_path
        super().__init__(
            code=ErrorCode.CONTEXT_CONFIG_INVALID,
            message=(
                f"Invalid SQLMesh configuration for workspace '{name}'. "
                f"Check its config.yaml connection settings."
            ),
            detail=detail,
            status_code=422,
        )


class ModelNotFoundError(ServiceError):
    def __init__(self, model_name: str):
        super().__init__(
            code=ErrorCode.MODEL_NOT_FOUND,
            message=f"Model not found: {model_name}",
            status_code=404,
        )


class WorkspaceNotFoundError(ServiceError):
    def __init__(self, workspace_id: str):
        super().__init__(
            code=ErrorCode.WORKSPACE_NOT_FOUND,
            message=f"Workspace not found: {workspace_id}",
            status_code=404,
        )


class WorkspacePathInvalidError(ServiceError):
    def __init__(self, path: str, reason: str = ""):
        super().__init__(
            code=ErrorCode.WORKSPACE_PATH_INVALID,
            message=f"Invalid workspace path: {path}",
            detail=reason,
            status_code=400,
        )


class ConflictError(ServiceError):
    def __init__(self, message: str):
        super().__init__(
            code=ErrorCode.CONFLICT,
            message=message,
            status_code=409,
        )


class PermissionDeniedError(ServiceError):
    def __init__(self, message: str):
        super().__init__(
            code=ErrorCode.PERMISSION_DENIED,
            message=message,
            status_code=403,
        )


class TimeoutError(ServiceError):
    def __init__(self, operation: str, timeout_seconds: int):
        super().__init__(
            code=ErrorCode.TIMEOUT,
            message=f"Operation '{operation}' timed out after {timeout_seconds}s",
            status_code=504,
        )


def extract_root_cause(exc: Exception, max_depth: int = 5) -> Optional[Exception]:
    """Walk the ``__cause__`` / ``__context__`` chain to find the root cause.

    SQLMesh / SQLAlchemy / psycopg2 frequently wrap a low-level error inside
    a higher-level one (e.g. ``migrate`` fails, ``rollback`` raises
    ``InFailedSqlTransaction`` whose ``__cause__`` is the original
    ``ObjectNotInPrerequisiteState``). Returning only the top-level
    exception hides the actionable root cause from the user. We traverse up
    to ``max_depth`` levels and return the deepest exception that itself
    has no cause, which is the one the user needs to see.

    Public (no leading underscore) so :mod:`src.utils.error_archive` can
    reuse the same root-cause extraction logic for archived error files.
    """
    seen: set[int] = set()
    current: Optional[Exception] = exc
    root: Optional[Exception] = exc
    depth = 0
    while current is not None and depth < max_depth:
        # Guard against cycles (shouldn't happen, but be defensive).
        if id(current) in seen:
            break
        seen.add(id(current))
        root = current
        nxt = current.__cause__ or current.__context__
        current = nxt if nxt is not current else None
        depth += 1
    return root


def _classify_db_error(exc: Exception) -> Optional[tuple[ErrorCode, int]]:
    """Classify a database-driver exception into (ErrorCode, status_code).

    Returns ``None`` when ``exc`` is not a recognized DB-driver error.
    Detection is done by class name string matching rather than direct
    imports, because:
    - The driver (psycopg2 / psycopg3 / asyncpg / sqlite3) is not known at
      module load time and may not be installed in every environment.
    - DBAPI-2 errors follow a stable naming convention (``Error``,
      ``OperationalError``, ``InterfaceError``, ``DatabaseError``,
      ``IntegrityError``, ``InternalError``, ``ProgrammingError``,
      ``NotSupportedError``, ``TransactionRollbackError``).

    Mapping rationale:
    - Connection-class (``OperationalError`` mentioning "connect", or
      ``InterfaceError``): HTTP 503 — the DB is unreachable, retry later.
    - Transaction-state (``InFailedSqlTransaction``,
      ``TransactionRollbackError``, or text containing "transaction is
      aborted" / "current transaction"): HTTP 409 — a previous statement
      aborted the transaction; the caller must rollback / retry.
    - Everything else DB-related (syntax errors, missing tables,
      ``ObjectNotInPrerequisiteState``, ``UndefinedColumn`` ...): HTTP 422
      — the user/DBA can fix the schema/SQL/config and retry.
    """
    cls_name = exc.__class__.__name__
    # Only consider names that look like DB-driver exceptions. This avoids
    # accidentally classifying random exceptions whose class happens to be
    # named ``Error``.
    db_driver_prefixes = (
        "psycopg2.", "psycopg.", "asyncpg.", "sqlite3.",
        "mysql.", "pymysql.", "MySQLdb.",
    )
    is_db_driver = (
        any(exc.__class__.__module__.startswith(p) for p in db_driver_prefixes)
        or cls_name in {
            # DBAPI-2 base / sub errors
            "DatabaseError", "OperationalError", "InterfaceError",
            "IntegrityError", "InternalError", "ProgrammingError",
            "NotSupportedError", "DataError",
            # psycopg2 specific
            "InFailedSqlTransaction", "ObjectNotInPrerequisiteState",
            "UndefinedColumn", "UndefinedTable",
            "DuplicateTable", "DuplicateColumn",
            "InvalidCatalogName", "InvalidSchemaName",
            # asyncpg / others
            "PostgresError", "InterfaceError",
        }
    )
    if not is_db_driver:
        return None

    text = str(exc)
    lower = text.lower()

    # 1. Transaction-state errors.
    if (
        cls_name == "InFailedSqlTransaction"
        or "transaction is aborted" in lower
        or "current transaction" in lower
        or cls_name == "TransactionRollbackError"
    ):
        return ErrorCode.DB_TRANSACTION_ERROR, 409

    # 2. Connection errors.
    if (
        cls_name == "InterfaceError"
        or (cls_name == "OperationalError" and (
            "could not connect" in lower
            or "connection" in lower and "refused" in lower
            or "server closed the connection" in lower
            or "timeout" in lower
        ))
    ):
        return ErrorCode.DB_CONNECTION_ERROR, 503

    # 3. Default: engine/SQL error — user/DBA fixable.
    return ErrorCode.DB_ENGINE_ERROR, 422


def service_error_from_exception(
    exc: Exception,
    *,
    code: ErrorCode,
    fallback_message: str,
    archive: Optional["ErrorArchiveContext"] = None,
) -> "ServiceError":
    """Translate a caught exception into a ServiceError that never hides detail.

    Mirrors the native SQLMesh CLI, which prints ``str(e)`` directly for plan /
    config / macro failures. Three cases:

    * SQLMesh business error (``SQLMeshError`` subclass): the user's project or
      plan is wrong and fixable. The raw error text becomes the top-level
      ``message`` (so CLI/frontend always show the actionable reason) and it
      maps to HTTP 422 rather than a 500-class server incident.
    * Database-driver error (psycopg2 / sqlite3 / asyncpg ...): classified by
      ``_classify_db_error`` into ``DB_ENGINE_ERROR`` (422),
      ``DB_CONNECTION_ERROR`` (503), or ``DB_TRANSACTION_ERROR`` (409). The
      raw error text becomes the top-level ``message`` so the user/DBA sees
      the actionable reason (e.g. "cannot delete from table _versions because
      it does not have a replica identity"). When a deeper root cause exists
      in the ``__cause__`` chain, both layers are preserved under ``detail``.
    * Any other exception: treated as an unexpected server fault. The generic
      ``fallback_message`` stays as ``message`` for a stable UX, while the raw
      text is preserved under ``detail`` for diagnostics, and it maps to 500.

    The already-typed ``ServiceError`` (and its subclasses) is passed through
    unchanged so callers can ``raise service_error_from_exception(...)`` inside a
    broad ``except`` without accidentally re-wrapping a structured error.

    When ``archive`` is provided, the translated error is also persisted as a
    JSON file under ``{workspace}/errors/`` (best-effort) so the agent-service
    repair agent can read the full context (root cause, SQL, traceback) and
    attempt a fix. The persisted ``error_id`` is stored on the returned
    ``ServiceError.error_log_id`` and included in the API response.
    """
    if isinstance(exc, ServiceError):
        # Pass-through: still archive if requested, but do not re-wrap.
        if archive is not None:
            _archive_error(exc, exc=exc, archive=archive)
        return exc

    text = str(exc).strip() or exc.__class__.__name__

    # 1. SQLMesh business error — actionable, user-fixable.
    is_business_error = _SQLMeshErrorCls is not None and isinstance(exc, _SQLMeshErrorCls)
    if is_business_error:
        service_error = ServiceError(code=code, message=text, status_code=422)
        if archive is not None:
            _archive_error(service_error, exc=exc, archive=archive)
        return service_error

    # 2. Database-driver error — classify and surface raw reason.
    db_classification = _classify_db_error(exc)
    if db_classification is not None:
        db_code, db_status = db_classification
        # Build a detail string that preserves the full cause chain so that
        # when rollback / retry produces a secondary exception, the original
        # root cause is not lost. Example:
        #   "Root cause: ObjectNotInPrerequisiteState: cannot delete from
        #    table _versions ... | At: InFailedSqlTransaction: current
        #    transaction is aborted ..."
        root = extract_root_cause(exc)
        if root is not None and root is not exc:
            root_text = str(root).strip() or root.__class__.__name__
            detail = f"Root cause: {root_text} | At: {text}"
        else:
            detail = None
        service_error = ServiceError(
            code=db_code,
            message=text,
            detail=detail,
            status_code=db_status,
        )
        if archive is not None:
            _archive_error(service_error, exc=exc, archive=archive)
        return service_error

    # 3. Unexpected internal fault — stable public message, retain detail.
    service_error = ServiceError(code=code, message=fallback_message, detail=text)
    if archive is not None:
        _archive_error(service_error, exc=exc, archive=archive)
    return service_error


def _archive_error(
    service_error: "ServiceError",
    *,
    exc: Exception,
    archive: "ErrorArchiveContext",
) -> None:
    """Persist ``service_error`` to ``{workspace}/errors/`` and set ``error_log_id``.

    Thin wrapper around :meth:`ErrorArchive.record` that backfills the
    returned ``error_id`` onto ``service_error.error_log_id`` so the API
    response can include it. Import is lazy to avoid a circular dependency
    (``error_archive`` imports ``extract_root_cause`` from this module).
    """
    try:
        from src.utils.error_archive import ErrorArchive

        error_id = ErrorArchive.record(
            exc=exc,
            service_error=service_error,
            archive_context=archive,
        )
        service_error.error_log_id = error_id
    except Exception:
        # Archiving is best-effort; never let it break error translation.
        import logging

        logging.getLogger(__name__).warning(
            "error_archive_invocation_failed", exc_info=True
        )
