"""HTTP client for workspace-runtime's env-control channel (design 06 P5).

The control plane owns the registry and the state machine but must have NO
filesystem access; this client is the single hop through which every Git /
SQLMesh-engine / provisioning side effect is dispatched to a runtime replica.

Error translation contract
--------------------------
The runtime answers errors in the platform's structured shape
(``{error_code, message, ...}``). This client maps them back onto the
LOCAL ServiceError hierarchy so registry code keeps raising typed errors
instead of parsing HTTP bodies:

* ``ENVIRONMENT_BUSY``            -> EnvironmentBusyError (409)
* ``WORKSPACE_NOT_FOUND``         -> WorkspaceNotFoundError (404)
* ``WORKSPACE_PATH_INVALID``      -> WorkspacePathInvalidError (400)
* ``GIT_REMOTE_ACCESS_DENIED``    -> GitRemoteAccessError (422)
* ``GIT_OPERATION_FAILED``        -> GitOperationError (502)
* other ``GIT_*`` / unknown codes -> generic ServiceError preserving the code
* transport failures / 5xx        -> RuntimeUnavailableError (503, retryable)

Where the Git fields come from
------------------------------
``ServiceError.to_response_dict`` INLINES a structured detail at the top level
of the body, so the runtime's Git errors arrive as top-level
``git_error_reason`` / ``git_operation`` / ``git_remote_host`` / ``git_detail``
— NOT nested under ``detail``. Reading them from ``detail`` (as an earlier
version did) always found nothing, so every relayed Git failure degraded to
reason ``unknown`` and the user lost the actual cause. They are read from the
body itself here, with the nested form still accepted for robustness.

Callers that prefer a boolean outcome (binder repair paths) catch
``RuntimeUnavailableError`` themselves; callers that surface the error to the
API let it propagate.
"""

from __future__ import annotations

import typing as t

import httpx
from datacode_common.git_reasons import GitRemoteReason

from src.config import settings
from src.utils.errors import (
    EnvironmentBusyError,
    ErrorCode,
    GitRemoteAccessError,
    RuntimeUnavailableError,
    ServiceError,
    WorkspaceNotFoundError,
    WorkspacePathInvalidError,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

# The internal-token header the runtime expects on /internal/* routes.
INTERNAL_TOKEN_HEADER = "X-Internal-Token"

# Runtime error_code -> local exception factory. Codes not listed fall through
# to a generic ServiceError that preserves code, message and status.
#
# The Git entries take the WHOLE body rather than (message, detail): the Git
# fields are inlined at the top level (see the module docstring), and the
# structured detail must survive the hop untouched — the control plane relays a
# verdict it is not entitled to re-interpret.
_ERROR_MAP: dict = {
    "ENVIRONMENT_BUSY": lambda body: EnvironmentBusyError(_message_of(body)),
    "WORKSPACE_NOT_FOUND": lambda body: WorkspaceNotFoundError(_message_of(body)),
    "WORKSPACE_PATH_INVALID": lambda body: WorkspacePathInvalidError(
        _message_of(body), _detail_string_of(body)
    ),
    "GIT_REMOTE_ACCESS_DENIED": lambda body: GitRemoteAccessError(
        *_git_fields_of(body)
    ),
    "GIT_OPERATION_FAILED": lambda body: _relay_git_operation_error(body),
}


def _message_of(body: dict) -> str:
    """The body's message, or a stable placeholder when absent."""
    return str(body.get("message") or "workspace-runtime reported an error")


def _detail_string_of(body: dict) -> str:
    """The body's nested ``detail`` as a string (empty when structured/absent)."""
    detail = body.get("detail")
    return detail if isinstance(detail, str) else ""


def _git_fields_of(body: dict) -> tuple[str, str, str, str]:
    """Extract ``(operation, reason, host, detail)`` from a relayed Git error.

    Top-level first (that is where ``to_response_dict`` inlines them), falling
    back to a nested ``detail`` dict so a caller that nests still works.
    """
    nested = body.get("detail")
    nested = nested if isinstance(nested, dict) else {}

    def field(name: str, default: str = "") -> str:
        value = body.get(name)
        if value in (None, ""):
            value = nested.get(name)
        return str(value) if value not in (None, "") else default

    return (
        field("git_operation", "git operation"),
        field("git_error_reason", GitRemoteReason.UNKNOWN),
        field("git_remote_host"),
        field("git_detail"),
    )


def _relay_git_operation_error(body: dict) -> ServiceError:
    """Relay a runtime ``GIT_OPERATION_FAILED`` without re-wrapping its message.

    The upstream message already reads "Git operation failed: <operation>".
    Constructing :class:`GitOperationError` from it would prepend the prefix a
    second time — the "Git operation failed: Git operation failed: ..." the
    user saw. The message is therefore forwarded verbatim while the structured
    Git fields are rebuilt so the frontend's single Git parsing path applies.
    """
    operation, reason, host, detail = _git_fields_of(body)
    return ServiceError(
        code=ErrorCode.GIT_OPERATION_FAILED,
        message=_message_of(body),
        detail={
            "git_error_reason": reason,
            "git_operation": operation,
            "git_remote_host": host,
            "git_detail": detail,
        },
        status_code=502,
    )


class RuntimeClient:
    """Thin, typed wrapper over the env-control REST surface."""

    def __init__(
        self,
        *,
        base_url: str = "",
        timeout: float | None = None,
        transport: t.Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._base_url = (base_url or settings.runtime_rest_url).rstrip("/")
        self._timeout = timeout if timeout is not None else settings.runtime_request_timeout
        # Injected by tests; ``None`` means real network.
        self._transport = transport

    @property
    def base_url(self) -> str:
        return self._base_url

    async def post(self, path: str, payload: dict) -> dict:
        """POST ``payload`` and return the decoded JSON body (raises on error)."""
        return await self._request("POST", path, json=payload)

    async def get(self, path: str, params: dict | None = None) -> dict:
        """GET with query params and return the decoded JSON body."""
        return await self._request("GET", path, params=params)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if not self._base_url:
            raise RuntimeUnavailableError(
                f"{method} {path}",
                "services.runtime is not configured in platform.yaml",
            )

        url = f"{self._base_url}{path}"
        headers = {INTERNAL_TOKEN_HEADER: settings.runtime_internal_token}
        try:
            async with self._client() as client:
                resp = await client.request(
                    method, url, json=json, params=params, headers=headers
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "runtime_request_unreachable",
                method=method,
                path=path,
                error=str(exc),
            )
            raise RuntimeUnavailableError(f"{method} {path}", str(exc)) from exc

        if resp.status_code >= 400:
            raise self._translate_error(method, path, resp)
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeUnavailableError(
                f"{method} {path}", f"malformed response body: {exc}"
            ) from exc

    def _translate_error(
        self, method: str, path: str, resp: httpx.Response
    ) -> ServiceError:
        """Map the runtime's structured error onto the local hierarchy."""
        body: dict = {}
        try:
            decoded = resp.json()
            if isinstance(decoded, dict):
                body = decoded
        except ValueError:
            pass

        code = str(body.get("error_code", ""))
        if not body.get("message"):
            body = dict(body, message=f"runtime {resp.status_code} on {path}")

        factory = _ERROR_MAP.get(code)
        if factory is not None:
            return factory(body)

        detail = body.get("detail")
        return ServiceError(
            ErrorCode(code) if code in ErrorCode._value2member_map_ else ErrorCode.INTERNAL_ERROR,
            str(body["message"]),
            detail=detail if isinstance(detail, dict) else (
                str(detail) if detail is not None else None
            ),
            status_code=502 if resp.status_code >= 500 else resp.status_code,
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)


_client: RuntimeClient | None = None


def get_runtime_client() -> RuntimeClient:
    """Get or create the global :class:`RuntimeClient`."""
    global _client
    if _client is None:
        _client = RuntimeClient()
    return _client


def set_runtime_client(client: RuntimeClient | None) -> None:
    """Replace the global client (tests wire a fake here)."""
    global _client
    _client = client
