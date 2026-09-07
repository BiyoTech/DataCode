"""Structured error definitions for control-service.

Provides typed exceptions with error codes for consistent API error responses.
The base class and the response shape are shared (``datacode_common.errors``):
they are a frontend contract, and a per-service copy of a contract drifts.
What stays here is the control plane's own ErrorCode enum and the
registry / workspace / Git-orchestration error families it owns.
"""

from enum import Enum
from typing import Optional

from datacode_common.errors import ServiceError
from datacode_common.git_reasons import GitRemoteReason
from datacode_common.git_reasons import message_for as git_reason_message

__all__ = ["ServiceError", "ErrorCode"]


class ErrorCode(str, Enum):
    """Enumerated error codes for structured API responses."""

    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    WORKSPACE_PATH_INVALID = "WORKSPACE_PATH_INVALID"
    ENVIRONMENT_NOT_FOUND = "ENVIRONMENT_NOT_FOUND"
    ENVIRONMENT_STATE_INVALID = "ENVIRONMENT_STATE_INVALID"
    ENVIRONMENT_BUSY = "ENVIRONMENT_BUSY"
    GIT_REPO_NOT_BOUND = "GIT_REPO_NOT_BOUND"
    GIT_REPO_URL_INVALID = "GIT_REPO_URL_INVALID"
    GIT_OPERATION_FAILED = "GIT_OPERATION_FAILED"
    GIT_REMOTE_ACCESS_DENIED = "GIT_REMOTE_ACCESS_DENIED"
    INVALID_REQUEST = "INVALID_REQUEST"
    TIMEOUT = "TIMEOUT"
    CONFLICT = "CONFLICT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


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
    """Raised when a Git operation required to fulfil the request failed.

    ``detail`` is STRUCTURED (and therefore inlined at the top level of the
    response by :meth:`ServiceError.to_response_dict`) so this 502 and the 422
    of :class:`GitRemoteAccessError` reach the frontend in the same shape: one
    parsing path, and the redacted git output always available to display.
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
    authorization failures.

    The control plane never CLASSIFIES a Git failure — the runtime does, and
    this only relays the verdict. Re-classifying here would mean two
    classifiers whose disagreement is invisible.
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


class RuntimeUnavailableError(ServiceError):
    """workspace-runtime could not be reached through the env-control channel.

    The control plane is degraded, not broken: registry rows keep their state
    and the operation can be retried once the runtime answers again.
    """

    def __init__(self, operation: str, detail: str = ""):
        super().__init__(
            code=ErrorCode.RUNTIME_UNAVAILABLE,
            message=(
                f"workspace-runtime could not execute '{operation}'. "
                "Retry shortly; the registry state is unchanged."
            ),
            detail=detail,
            status_code=503,
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
    """The caller is not allowed to perform the requested operation."""

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

    429 rather than 409 on purpose: the limit is not a conflict with the
    current state of one object — it is a rate the tenant has exhausted, and
    the standard client behaviour (back off, honour Retry-After, retry) is
    exactly right for it. The structured payload names the resource, the
    ceiling and the live count so the UI can render an actionable sentence
    without a second round-trip.
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
                f"({current}/{limit}). Free up resources or contact an "
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
