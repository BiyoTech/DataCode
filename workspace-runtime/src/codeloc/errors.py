"""Structured error definitions for the Dagster (codeloc) surface.

Provides typed exceptions with error codes for consistent API error responses.
The base class and the response shape come from ``datacode_common.errors`` —
this copy used to re-implement them and had already drifted (no ``headers``,
so it could not even carry a Retry-After). What stays here is the
Dagster-specific ErrorCode enum and error families.
"""

from enum import Enum
from typing import Optional

from datacode_common.errors import ServiceError


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

    # Deployment (one environment's code location + run namespace).
    DEPLOYMENT_NOT_FOUND = "DEPLOYMENT_NOT_FOUND"
    DEPLOYMENT_INVALID = "DEPLOYMENT_INVALID"

    # Dagster domain objects.
    GRAPHQL_ERROR = "GRAPHQL_ERROR"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    ASSET_NOT_FOUND = "ASSET_NOT_FOUND"
    PIPELINE_NOT_FOUND = "PIPELINE_NOT_FOUND"
    SCHEDULE_NOT_FOUND = "SCHEDULE_NOT_FOUND"

    # Bridge.
    SQLMESH_BRIDGE_FAILED = "SQLMESH_BRIDGE_FAILED"


class PermissionDeniedError(ServiceError):
    """Raised when a caller is not authorized to reach an endpoint.

    Used by the internal (service-to-service) API, whose only credential is a
    shared secret. 403 rather than 401: there is no interactive challenge to
    issue — either the peer holds the platform's internal token or it is not a
    peer.
    """

    def __init__(self, message: str = "Permission denied", detail: Optional[str] = None):
        super().__init__(
            code=ErrorCode.PERMISSION_DENIED,
            message=message,
            detail=detail,
            status_code=403,
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


class DeploymentNotFoundError(ServiceError):
    """Raised when a deployment name is not known to this service.

    "Not known" means no code location has been ensured for it in this process —
    the registry may well have a row for it. Callers that want it running must
    ensure it rather than treat this as a permanent absence.
    """

    def __init__(self, deployment_name: str):
        super().__init__(
            code=ErrorCode.DEPLOYMENT_NOT_FOUND,
            message=f"Deployment not found: {deployment_name}",
            status_code=404,
        )


class DeploymentInvalidError(ServiceError):
    """Raised when a deployment request contradicts the platform's own naming.

    Typically an ``environment`` or ``env_type`` that does not match the working
    copy the caller pointed at: honoring it would load one environment's code
    under another environment's identity, which is the exact failure mode the
    deployment abstraction exists to prevent.
    """

    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(
            code=ErrorCode.DEPLOYMENT_INVALID,
            message=message,
            detail=detail,
            status_code=400,
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
