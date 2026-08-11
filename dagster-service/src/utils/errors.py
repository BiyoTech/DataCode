"""Structured error definitions for Dagster Service.

Provides typed exceptions with error codes for consistent API error responses,
mirroring the shape used by sqlmesh-service (``{error_code, message, detail?}``).
"""

from enum import Enum
from typing import Optional


class ErrorCode(str, Enum):
    """Enumerated error codes for structured API responses."""

    # Generic.
    INVALID_REQUEST = "INVALID_REQUEST"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    TIMEOUT = "TIMEOUT"
    CONFLICT = "CONFLICT"
    PERMISSION_DENIED = "PERMISSION_DENIED"

    # Workspace.
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    WORKSPACE_PATH_INVALID = "WORKSPACE_PATH_INVALID"

    # Dagster process / instance.
    PROCESS_START_FAILED = "PROCESS_START_FAILED"
    CODE_LOCATION_LOAD_FAILED = "CODE_LOCATION_LOAD_FAILED"
    DAGSTER_UNAVAILABLE = "DAGSTER_UNAVAILABLE"

    # Dagster domain objects.
    GRAPHQL_ERROR = "GRAPHQL_ERROR"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    ASSET_NOT_FOUND = "ASSET_NOT_FOUND"
    PIPELINE_NOT_FOUND = "PIPELINE_NOT_FOUND"
    SCHEDULE_NOT_FOUND = "SCHEDULE_NOT_FOUND"

    # Bridge.
    SQLMESH_BRIDGE_FAILED = "SQLMESH_BRIDGE_FAILED"


class ServiceError(Exception):
    """Base exception for all dagster-service errors."""

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
        super().__init__(message)

    def to_response_dict(self) -> dict:
        """Public response shape with top-level error fields."""
        out: dict = {"error_code": self.code.value, "message": self.message}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


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


class ProcessStartError(ServiceError):
    def __init__(self, process_name: str, detail: Optional[str] = None):
        super().__init__(
            code=ErrorCode.PROCESS_START_FAILED,
            message=f"Failed to start Dagster process: {process_name}",
            detail=detail,
            status_code=500,
        )


class CodeLocationLoadError(ServiceError):
    def __init__(self, workspace_path: str, detail: Optional[str] = None):
        super().__init__(
            code=ErrorCode.CODE_LOCATION_LOAD_FAILED,
            message=f"Failed to load code location for workspace: {workspace_path}",
            detail=detail,
            status_code=500,
        )


class DagsterUnavailableError(ServiceError):
    """Raised when the (local or remote) Dagster webserver cannot be reached."""

    def __init__(self, endpoint: str, detail: Optional[str] = None):
        super().__init__(
            code=ErrorCode.DAGSTER_UNAVAILABLE,
            message=f"Dagster instance is unavailable at: {endpoint}",
            detail=detail,
            status_code=503,
        )


class GraphQLError(ServiceError):
    """Raised when a Dagster GraphQL query/mutation returns errors."""

    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(
            code=ErrorCode.GRAPHQL_ERROR,
            message=message,
            detail=detail,
            status_code=502,
        )


class RunNotFoundError(ServiceError):
    def __init__(self, run_id: str):
        super().__init__(
            code=ErrorCode.RUN_NOT_FOUND,
            message=f"Run not found: {run_id}",
            status_code=404,
        )


class SQLMeshBridgeError(ServiceError):
    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(
            code=ErrorCode.SQLMESH_BRIDGE_FAILED,
            message=message,
            detail=detail,
            status_code=502,
        )


def service_error_from_exception(
    exc: Exception,
    *,
    code: ErrorCode,
    fallback_message: str,
) -> "ServiceError":
    """Translate a caught exception into a ServiceError.

    Already-typed ``ServiceError`` instances are passed through unchanged so a
    broad ``except`` can safely ``raise service_error_from_exception(...)``
    without re-wrapping a structured error. Any other exception becomes an
    internal fault with a stable public ``message`` and the raw text preserved
    under ``detail`` for diagnostics.
    """
    if isinstance(exc, ServiceError):
        return exc
    text = str(exc).strip() or exc.__class__.__name__
    return ServiceError(code=code, message=fallback_message, detail=text)
