"""Structured error definitions for the workspace-runtime service.

Provides typed exceptions with error codes for consistent API error responses.
The base class and the response shape come from ``datacode_common.errors``:
they are a frontend contract, and a per-service copy of a contract drifts.
Codes that the platform contract froze (design 08 §7) are aliased from
:datacode_common.errors` — the shared module is the single source of their
literals, because most of them cross a service boundary (the gateway
re-routes on LEASE_NOT_HELD; task-runner classifies SANDBOX_*; the frontend
retries on WORKSPACE_RELOCATING) and a renamed copy on one side silently
disables the other side's handling.
"""

import errno
import os
from enum import Enum
from typing import TYPE_CHECKING, Optional

from datacode_common.errors import PlatformErrorCode, ServiceError
from datacode_common.git_reasons import GitRemoteReason
from datacode_common.git_reasons import message_for as git_reason_message

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
    ENVIRONMENT_NOT_FOUND = "ENVIRONMENT_NOT_FOUND"
    ENVIRONMENT_STATE_INVALID = "ENVIRONMENT_STATE_INVALID"
    ENVIRONMENT_BUSY = "ENVIRONMENT_BUSY"
    STATE_SCHEMA_MISMATCH = "STATE_SCHEMA_MISMATCH"
    GIT_REPO_NOT_BOUND = "GIT_REPO_NOT_BOUND"
    GIT_REPO_URL_INVALID = "GIT_REPO_URL_INVALID"
    GIT_OPERATION_FAILED = "GIT_OPERATION_FAILED"
    GIT_REMOTE_ACCESS_DENIED = "GIT_REMOTE_ACCESS_DENIED"
    GIT_MERGE_CONFLICT = "GIT_MERGE_CONFLICT"
    INVALID_REQUEST = "INVALID_REQUEST"
    TIMEOUT = "TIMEOUT"
    CONFLICT = "CONFLICT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    # A dependency or subsystem this request needs is not running. Distinguished
    # from INTERNAL_ERROR so callers (gateway) can degrade rather than retry.
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    # Tenant-level resource quota exhausted (P8): 429 + Retry-After.
    QUOTA_EXCEEDED = PlatformErrorCode.QUOTA_EXCEEDED.value
    # ── Platform contract codes (design 08 §7) ──────────────────────────
    # Literals are aliased from the shared contract module rather than
    # restated, so a rename here is impossible without renaming the contract.
    # The request reached an instance that does not hold this working copy's
    # lease; the gateway must invalidate its cached route and re-resolve.
    LEASE_NOT_HELD = PlatformErrorCode.LEASE_NOT_HELD.value
    # No lease capacity in the cell; 429 + Retry-After.
    LEASE_UNAVAILABLE = PlatformErrorCode.LEASE_UNAVAILABLE.value
    # Two runtimes claim one working copy; the file lock arbitrates. Distinct
    # from the reconciler's lowercase `lease_split_brain` DRIFT kind, which
    # reports the same condition asynchronously on the control plane.
    LEASE_SPLIT_BRAIN = PlatformErrorCode.LEASE_SPLIT_BRAIN.value
    # The working copy is being materialized; valid request, must wait (202).
    WORKSPACE_MATERIALIZING = PlatformErrorCode.WORKSPACE_MATERIALIZING.value
    # This instance is draining and the working copy is moving (503).
    WORKSPACE_RELOCATING = PlatformErrorCode.WORKSPACE_RELOCATING.value
    # Sandbox could not be started at all (image pull, node capacity, fork).
    SANDBOX_UNAVAILABLE = PlatformErrorCode.SANDBOX_UNAVAILABLE.value
    # Task killed by its own deadline (container timeout or RLIMIT_CPU).
    SANDBOX_TIMEOUT = PlatformErrorCode.SANDBOX_TIMEOUT.value
    # Task killed by the memory limit (OOMKill or RLIMIT_AS).
    SANDBOX_OOM = PlatformErrorCode.SANDBOX_OOM.value
    # Outbound connection refused by the egress policy.
    EGRESS_DENIED = PlatformErrorCode.EGRESS_DENIED.value
    # The cell is under maintenance or tenant migration.
    CELL_MAINTENANCE = PlatformErrorCode.CELL_MAINTENANCE.value
    # The working copy's disk is full against its quota.
    INSUFFICIENT_STORAGE = PlatformErrorCode.INSUFFICIENT_STORAGE.value
    # Database-layer errors. These are raised by DB drivers (psycopg2,
    # sqlite3, etc.) and are typically USER/DBA-fixable (e.g. a Postgres
    # table missing REPLICA IDENTITY, a publication misconfiguration, a
    # connection refused). We surface them as actionable 4xx/5xx rather
    # than opaque 500s so the frontend can show the real reason.
    DB_ENGINE_ERROR = "DB_ENGINE_ERROR"
    DB_CONNECTION_ERROR = "DB_CONNECTION_ERROR"
    DB_TRANSACTION_ERROR = "DB_TRANSACTION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


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
    identifies the workspace and environment (never the on-disk path, which
    exposes the platform's private layout); the underlying validation reason is
    passed as ``detail`` (SQLMesh config errors report offending field names, not
    secret values) and the full error is logged server-side.
    """

    def __init__(
        self,
        workspace_path: str,
        detail: Optional[str] = None,
        *,
        workspace_id: str = "",
        environment: str = "",
    ):
        if workspace_id:
            target = f"'{workspace_id}' (environment '{environment or 'unknown'}')"
        else:
            # Fallback for callers that only hold a path.
            name = os.path.basename(os.path.normpath(workspace_path)) or workspace_path
            target = f"'{name}'"
        super().__init__(
            code=ErrorCode.CONTEXT_CONFIG_INVALID,
            message=(
                f"Invalid SQLMesh configuration for workspace {target}. "
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


class EnvironmentNotFoundError(ServiceError):
    """Raised when an environment is absent from the platform registry.

    The registry is the source of truth: an environment that only exists as a
    Git branch or as a SQLMesh state entry is NOT an environment yet, it is
    drift, and it must be adopted through the reconciler first.
    """

    def __init__(self, environment: str):
        super().__init__(
            code=ErrorCode.ENVIRONMENT_NOT_FOUND,
            message=f"Environment not found: {environment}",
            status_code=404,
        )


class EnvironmentStateError(ServiceError):
    """Raised when an environment's lifecycle state forbids the operation.

    Examples: opening an environment that is still being created, destroying
    production, branching from an archived environment.
    """

    def __init__(self, message: str):
        super().__init__(
            code=ErrorCode.ENVIRONMENT_STATE_INVALID,
            message=message,
            status_code=409,
        )


class EnvironmentBusyError(ServiceError):
    """Raised when another state-mutating operation holds the environment lock.

    Every environment of a workspace shares one SQLMesh state schema, so two
    concurrent applies would race on the same state rows. Rejecting immediately
    with an explicit "busy" answer is far more actionable than letting the two
    operations collide half-way through a backfill.
    """

    def __init__(self, environment: str, operation: str = ""):
        held_by = f" (held by: {operation})" if operation else ""
        super().__init__(
            code=ErrorCode.ENVIRONMENT_BUSY,
            message=(
                f"Environment '{environment}' is busy: another operation is "
                f"already modifying its state{held_by}. Retry once it finishes."
            ),
            status_code=409,
        )


class StateSchemaMismatchError(ServiceError):
    """Raised when a workspace config declares a non-managed ``state_schema``.

    The SQLMesh state (snapshots, environments, external models) is a
    WORKSPACE-level shared asset: every environment and every member must read
    and write the same state schema, otherwise promoting between environments
    loses snapshot lineage and the next plan rebuilds everything from scratch.

    The platform therefore GENERATES ``state_schema`` into ``config.yaml`` (at
    workspace creation, and for any gateway added later) and from then on the
    FILE is the truth: the value is never silently rewritten at runtime. A
    deviating value is rejected here instead, so what the config says and what
    actually happens can never diverge.

    The structured ``detail`` is inlined at the top level of the response body
    (see ``ServiceError.to_response_dict``) so the frontend can render the
    mismatch and offer the one-click repair endpoint without string parsing.
    """

    def __init__(self, *, gateway: str, expected: str, actual: str, config_path: str):
        super().__init__(
            code=ErrorCode.STATE_SCHEMA_MISMATCH,
            message=(
                f"config.yaml declares state_schema '{actual}' for gateway "
                f"'{gateway}', but this workspace's SQLMesh state lives in "
                f"'{expected}'. Set the gateway's state_schema to '{expected}' "
                f"in config.yaml (or remove the key and let the platform "
                f"regenerate it), then retry."
            ),
            detail={
                "gateway": gateway,
                "expected": expected,
                "actual": actual,
                "config_file": "config.yaml",
                "repair_endpoint": "/api/v1/sqlmesh/config/state-schema/repair",
            },
            status_code=409,
        )


class GitRepoNotBoundError(ServiceError):
    """Raised when a workspace cannot be opened because no Git repo is bound.

    Git is the only durable carrier of workspace code, so a workspace without a
    remote cannot be materialized on another member's machine. The owner must
    bind a repository and push first.
    """

    def __init__(self, workspace_id: str):
        super().__init__(
            code=ErrorCode.GIT_REPO_NOT_BOUND,
            message=(
                f"Workspace {workspace_id} has no Git repository bound. "
                f"Ask the workspace owner to push the code to Git first."
            ),
            status_code=409,
        )


class GitRepoUrlInvalidError(ServiceError):
    """Raised when a supplied Git remote URL is rejected by validation."""

    def __init__(self, reason: str):
        super().__init__(
            code=ErrorCode.GIT_REPO_URL_INVALID,
            message=f"Invalid Git repository URL: {reason}",
            status_code=400,
        )


class GitOperationError(ServiceError):
    """Raised when a Git command required to fulfil the request failed.

    ``detail`` is STRUCTURED (and therefore inlined at the top level of the
    response by :meth:`ServiceError.to_response_dict`) so this 502 and the 422
    of :class:`GitRemoteAccessError` reach the frontend in the same shape: one
    parsing path, and the redacted git output always available to display.
    Before that, the detail was a bare string and the actual reason — already
    known here — never reached the user.
    """

    def __init__(
        self,
        operation: str,
        detail: str = "",
        *,
        reason: str = GitRemoteReason.UNKNOWN,
        host: str = "",
    ):
        super().__init__(
            code=ErrorCode.GIT_OPERATION_FAILED,
            message=f"Git operation failed: {operation}",
            detail={
                "git_error_reason": reason,
                "git_operation": operation,
                "git_remote_host": host,
                # Redacted git output; useful for support, never a secret.
                "git_detail": detail,
            },
            status_code=502,
        )


class GitRemoteAccessError(ServiceError):
    """A remote Git operation was rejected by the provider.

    Carries a stable ``git_error_reason`` (see
    :class:`datacode_common.git_reasons.GitRemoteReason`) so the frontend
    renders a localized, actionable sentence instead of raw git stderr. Mapped
    to 422 rather than 403 on purpose: 403 is reserved for platform
    authorization failures, which the frontend localizes through a different
    path.
    """

    def __init__(
        self,
        operation: str,
        reason: str,
        host: str = "",
        detail: str = "",
    ):
        super().__init__(
            code=ErrorCode.GIT_REMOTE_ACCESS_DENIED,
            message=git_reason_message(reason, host=host, operation=operation),
            detail={
                "git_error_reason": reason,
                "git_operation": operation,
                "git_remote_host": host,
                # Redacted git stderr; useful for support, never a secret.
                "git_detail": detail,
            },
            status_code=422,
        )


class GitMergeConflictError(ServiceError):
    """A promotional merge cannot be created because it conflicts.

    Refusing is the whole point: a conflict is an intermediate state that only a
    human with a working tree can resolve, so the platform never writes a half
    merge. The structured ``detail`` is inlined at the top level of the response
    (see :meth:`ServiceError.to_response_dict`) so the frontend lists the
    conflicted files and points the user at the resolution path — sync the base
    branch INTO the source environment, resolve there, commit, then retry.
    """

    def __init__(
        self,
        *,
        source_branch: str,
        target_branch: str,
        conflicts: list,
    ):
        super().__init__(
            code=ErrorCode.GIT_MERGE_CONFLICT,
            message=(
                f"Merging '{source_branch}' into '{target_branch}' conflicts in "
                f"{len(conflicts)} file(s). Sync '{target_branch}' into "
                f"'{source_branch}' first, resolve the conflicts there, commit, "
                f"then merge again."
            ),
            detail={
                "conflicts": conflicts,
                "source_branch": source_branch,
                "target_branch": target_branch,
            },
            status_code=409,
        )


class ConflictError(ServiceError):
    def __init__(self, message: str):
        super().__init__(
            code=ErrorCode.CONFLICT,
            message=message,
            status_code=409,
        )


class InvalidRequestError(ServiceError):
    """Raised when request data fails business validation (HTTP 400)."""

    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(
            code=ErrorCode.INVALID_REQUEST,
            message=message,
            detail=detail,
            status_code=400,
        )


class PermissionDeniedError(ServiceError):
    """The caller is not allowed to perform the requested operation.

    ``permission`` names the capability the gateway evaluated, so the message is
    actionable ("ask an administrator for X") instead of an opaque 403.
    """

    def __init__(self, message: str, permission: Optional[str] = None):
        if permission:
            message = f'{message} (required permission: "{permission}")'
        super().__init__(
            code=ErrorCode.PERMISSION_DENIED,
            message=message,
            detail=permission,
            status_code=403,
        )


class TimeoutError(ServiceError):
    def __init__(self, operation: str, timeout_seconds: int):
        super().__init__(
            code=ErrorCode.TIMEOUT,
            message=f"Operation '{operation}' timed out after {timeout_seconds}s",
            status_code=504,
        )


class QuotaExceededError(ServiceError):
    """A tenant-level resource quota was reached (design 06 P8).

    Raised on the lease-acquire path when the tenant holds its ceiling of
    concurrent leases (or occupies its ceiling of runtime replicas). 429 rather
    than 503 on purpose: the limit is a rate the tenant has exhausted, and the
    gateway/client should back off and honour ``Retry-After`` rather than
    re-place onto another instance.
    """

    def __init__(
        self,
        resource: str,
        *,
        tenant_id: str,
        limit: int,
        current: int,
        retry_after_seconds: int = 60,
    ):
        super().__init__(
            code=ErrorCode.QUOTA_EXCEEDED,
            message=(
                f"Tenant {tenant_id} reached its {resource} quota "
                f"({current}/{limit}). Release a working copy or contact an "
                "administrator to raise the limit."
            ),
            detail={
                "quota_resource": resource,
                "quota_limit": limit,
                "quota_current": current,
                "tenant_id": tenant_id,
            },
            status_code=429,
            headers={"Retry-After": str(max(0, int(retry_after_seconds)))},
        )


def translate_oserror(exc: OSError) -> ServiceError:
    """Translate an OSError into its contract answer (design 08 §7).

    ENOSPC becomes ``INSUFFICIENT_STORAGE`` (507): a full disk surfaces through
    whatever write happens to hit it first — a file save, a draft, a git object
    — so the translation lives in one place (the global exception handler)
    rather than scattered per endpoint. "No space" is the one OSError an editor
    user can act on (delete files, ask for quota), and it must not share a body
    with genuine server faults.

    Every other OSError keeps the generic 500 shape: ``FileNotFoundError`` and
    friends are either handled by their own endpoints already or are genuine
    server faults, and reclassifying them here would silently change dozens of
    responses at once.
    """
    if exc.errno == errno.ENOSPC:
        return ServiceError(
            code=ErrorCode.INSUFFICIENT_STORAGE,
            message=(
                "The storage backing this working copy is full; remove "
                "unreferenced files or contact an operator"
            ),
            detail=str(exc),
            status_code=507,
        )
    return ServiceError(
        code=ErrorCode.INTERNAL_ERROR,
        message="An unexpected error occurred",
        detail=str(exc).strip() or exc.__class__.__name__,
        status_code=500,
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
