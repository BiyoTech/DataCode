"""RuntimeClient tests (design 06 P5).

The client is the control plane's ONLY way to touch working copies, so its
error-translation contract is what keeps the registry's typed errors honest
across the service boundary: a busy environment must surface as 409, an
engine refusal as a boolean drift signal, and a dead runtime as a retryable
503 — never as an opaque transport error.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.runtime.client import RuntimeClient  # noqa: E402
from src.utils.errors import (  # noqa: E402
    EnvironmentBusyError,
    GitRemoteAccessError,
    RuntimeUnavailableError,
    ServiceError,
    WorkspaceNotFoundError,
)


def _client_with(handler) -> RuntimeClient:
    transport = httpx.MockTransport(handler)
    return RuntimeClient(base_url="http://runtime:8002", transport=transport)


def _error_response(status: int, code: str, message: str, detail=None) -> httpx.Response:
    body: dict = {"error_code": code, "message": message}
    if detail is not None:
        body["detail"] = detail
    return httpx.Response(status, json=body)


async def test_translates_busy_environment():
    def handler(request: httpx.Request) -> httpx.Response:
        return _error_response(409, "ENVIRONMENT_BUSY", "Environment 'dev' is busy")

    client = _client_with(handler)
    with pytest.raises(EnvironmentBusyError) as excinfo:
        await client.post("/internal/env-control/git/ensure-worktree", {})
    assert excinfo.value.status_code == 409


async def test_translates_workspace_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return _error_response(404, "WORKSPACE_NOT_FOUND", "Workspace not found: x")

    client = _client_with(handler)
    with pytest.raises(WorkspaceNotFoundError):
        await client.get("/internal/env-control/sqlmesh/state-environments")


async def test_translates_git_remote_access_denied():
    def handler(request: httpx.Request) -> httpx.Response:
        return _error_response(
            422,
            "GIT_REMOTE_ACCESS_DENIED",
            "push rejected",
            detail={
                "git_operation": "push",
                "git_error_reason": "credential_missing",
                "git_remote_host": "github.com",
                "git_detail": "stderr",
            },
        )

    client = _client_with(handler)
    with pytest.raises(GitRemoteAccessError) as excinfo:
        await client.post("/internal/env-control/git/push-branch", {})
    detail = excinfo.value.detail
    assert detail["git_error_reason"] == "credential_missing"
    assert excinfo.value.status_code == 422


async def test_reads_git_fields_inlined_at_the_top_level():
    """The shape the runtime actually sends.

    ``ServiceError.to_response_dict`` INLINES a structured detail, so a real
    runtime answer has no ``detail`` object at all. Reading the fields from
    ``detail`` (as an earlier version did) always found nothing, and every
    relayed Git failure silently became reason ``unknown`` — the user lost the
    cause even though the runtime had reported it correctly.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error_code": "GIT_REMOTE_ACCESS_DENIED",
                "message": "The repository was not found on github.com",
                "git_error_reason": "repo_not_found",
                "git_operation": "fetch",
                "git_remote_host": "github.com",
                "git_detail": "remote: Repository not found.",
            },
        )

    client = _client_with(handler)
    with pytest.raises(GitRemoteAccessError) as excinfo:
        await client.post("/internal/env-control/git/ensure-repository", {})

    detail = excinfo.value.detail
    assert detail["git_error_reason"] == "repo_not_found"
    assert detail["git_operation"] == "fetch"
    assert detail["git_remote_host"] == "github.com"
    assert "Repository not found" in detail["git_detail"]


async def test_git_operation_failed_is_not_prefixed_twice():
    """The "Git operation failed: Git operation failed: ..." users reported.

    The upstream message already carries the prefix; rebuilding the error from
    it as if it were an operation name produced the doubled sentence. The
    message must therefore be relayed verbatim.
    """
    upstream = "Git operation failed: add worktree for environment prod"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={
                "error_code": "GIT_OPERATION_FAILED",
                "message": upstream,
                "git_error_reason": "branch_not_found",
                "git_operation": "add worktree for environment prod",
                "git_remote_host": "github.com",
                "git_detail": "branch 'main' exists neither locally nor on origin",
            },
        )

    client = _client_with(handler)
    with pytest.raises(ServiceError) as excinfo:
        await client.post("/internal/env-control/git/ensure-worktree", {})

    error = excinfo.value
    assert error.message == upstream
    assert error.message.count("Git operation failed:") == 1
    assert error.status_code == 502
    # The structured payload survives the hop, so the frontend's single Git
    # parsing path applies to this 502 exactly as it does to a 422.
    body = error.to_response_dict()
    assert body["git_error_reason"] == "branch_not_found"
    assert "exists neither locally" in body["git_detail"]


async def test_git_error_without_fields_still_yields_a_parseable_reason():
    """A degraded upstream must not produce a body missing the reason key."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _error_response(502, "GIT_OPERATION_FAILED", "Git operation failed: init")

    client = _client_with(handler)
    with pytest.raises(ServiceError) as excinfo:
        await client.post("/internal/env-control/git/ensure-repository", {})
    assert excinfo.value.to_response_dict()["git_error_reason"] == "unknown"


async def test_translates_unknown_code_to_generic_service_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return _error_response(418, "SOMETHING_ELSE", "teapot")

    client = _client_with(handler)
    with pytest.raises(ServiceError) as excinfo:
        await client.post("/internal/env-control/workspace/hard-purge", {})
    assert not isinstance(excinfo.value, EnvironmentBusyError)
    assert excinfo.value.status_code == 418


async def test_transport_failure_is_retryable_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client_with(handler)
    with pytest.raises(RuntimeUnavailableError) as excinfo:
        await client.get("/internal/env-control/git/members")
    assert excinfo.value.status_code == 503


async def test_malformed_body_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    client = _client_with(handler)
    with pytest.raises(RuntimeUnavailableError):
        await client.get("/internal/env-control/git/members")


async def test_posts_internal_token_header():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("X-Internal-Token")
        return httpx.Response(200, json={"ok": True})

    client = _client_with(handler)
    # The token comes from settings; patch it deterministically for the test.
    from src.config import settings

    previous = settings.runtime_internal_token
    settings.runtime_internal_token = "secret-token"
    try:
        body = await client.post("/internal/env-control/git/ensure-repository", {})
    finally:
        settings.runtime_internal_token = previous
    assert body == {"ok": True}
    assert seen["token"] == "secret-token"


async def test_missing_base_url_is_unavailable():
    client = RuntimeClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text="{}")
    ))
    # Simulate a deployment without services.runtime registered: the platform
    # topology yields no URL and every dispatch must fail closed (503).
    client._base_url = ""
    with pytest.raises(RuntimeUnavailableError):
        await client.get("/internal/env-control/git/members")
