"""Contract tests for the lease-capacity and storage error codes (08 §7).

LEASE_UNAVAILABLE (429 + Retry-After) is the acquire endpoint's capacity
verdict: no runtime instance could take the working copy. The gateway keys its
no-degradation rule on the typed error it builds from this response, so the
status, code and header must all match the contract exactly.

INSUFFICIENT_STORAGE (507) is the ENOSPC translation: a full disk surfaces
through whichever write hits it first, and "no space" is the one OSError an
editor user can act on — it must not arrive as an opaque 500.
"""

from __future__ import annotations

import errno
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.rest import leases as leases_api
from src.lease.server import LeaseAcquireError
from src.utils.errors import ErrorCode, ServiceError, translate_oserror


class _FailingServer:
    """A lease server whose placement always exhausts the fleet."""

    def __init__(self, cause: Exception | None = None) -> None:
        # The endpoint discriminates LEASE_SPLIT_BRAIN from
        # LEASE_UNAVAILABLE by the exception the server chain carries.
        self._cause = cause

    async def acquire(self, lease_key: str) -> dict:
        error = LeaseAcquireError(
            f"no instance accepted the lease for {lease_key!r}: "
            "all candidates are at capacity"
        )
        if self._cause is not None:
            raise error from self._cause
        raise error


@pytest.fixture()
def client(monkeypatch):
    """An app exposing only the lease endpoints, over the failing server."""
    import src.lease.wiring as wiring
    from src.config import settings

    monkeypatch.setattr(wiring, "get_lease_server", lambda: _FailingServer())
    # The internal endpoints are shared-secret guarded; enable the guard with
    # a known token so the request reaches the handler under test.
    monkeypatch.setattr(settings, "internal_token", "test-internal-token")

    app = _build_app()
    return TestClient(app)


@pytest.fixture()
def split_brain_client(monkeypatch):
    """Same app, but the server's failure chain carries a split brain."""
    import src.lease.wiring as wiring
    from src.config import settings
    from src.lease.holder import LeaseSplitBrain

    monkeypatch.setattr(
        wiring,
        "get_lease_server",
        lambda: _FailingServer(cause=LeaseSplitBrain("arbitration failed")),
    )
    monkeypatch.setattr(settings, "internal_token", "test-internal-token")
    return TestClient(_build_app())


def _build_app(router=None) -> FastAPI:
    """A minimal app exposing one router with the production error handlers.

    The default is the lease router; other contract tests pass their own.
    """
    app = FastAPI()
    app.include_router(router if router is not None else leases_api.router)

    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.exception_handler(ServiceError)
    async def _handler(_request: Request, exc: ServiceError) -> JSONResponse:
        # Mirrors production's single handler. The codeloc hierarchy shares
        # this base now, so a second codeloc-specific registration would
        # SHADOW this one (same class) and drop the Retry-After headers.
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_response_dict(),
            headers=exc.headers,
        )

    return app


def test_acquire_capacity_failure_answers_429_with_retry_after(client):
    """The contract: LEASE_UNAVAILABLE, 429, and a Retry-After window.

    The window matters more than the status: the gateway propagates this
    verdict to the client instead of degrading, and the client honours the
    window — without it, a 429 invites an immediate retry against a verdict
    that cannot have changed.
    """
    response = client.post(
        "/internal/leases/acquire",
        json={"lease_key": "t1/ws-t1-shop/u1/dev_alice"},
        headers={"X-Internal-Token": "test-internal-token"},
    )

    assert response.status_code == 429
    body = response.json()
    assert body["error_code"] == "LEASE_UNAVAILABLE"
    assert "capacity" in response.headers.get("Retry-After", "") or response.headers[
        "Retry-After"
    ].isdigit()
    # The fleet-level reason travels as detail, not as the user message.
    assert "no instance accepted" in body.get("detail", "")


def test_split_brain_answers_409_without_a_retry_after(split_brain_client):
    """The contract's discrimination: arbitration failure ≠ capacity.

    A split brain resolves within one retry, so the answer is 409 with NO
    Retry-After (an immediate retry is safe and expected to succeed) —
    advertising a window here would make callers wait for a condition that
    is already gone.
    """
    response = split_brain_client.post(
        "/internal/leases/acquire",
        json={"lease_key": "t1/ws-t1-shop/u1/dev_alice"},
        headers={"X-Internal-Token": "test-internal-token"},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "LEASE_SPLIT_BRAIN"
    assert "Retry-After" not in response.headers


class TestAgentShellSubmissionFailure:
    """The shell tool surfaces a failed SUBMISSION as the contract's 503.

    A 200-with-an-error-dict would tell the agent the command ran and failed,
    inviting a code fix for what is an infrastructure problem. agent-service's
    client already maps 5xx to a failed-command result, so the agent still
    sees one result per submitted command.
    """

    @pytest.fixture()
    def shell_client(self, monkeypatch):
        from src.api.rest import tools as tools_api
        from src.tasks.submitter import TaskSubmissionError

        # The endpoint's pre-submission checks (path validation, identity
        # extraction) are not under test here; only the submission verdict is.
        monkeypatch.setattr(tools_api, "validate_workspace_path", lambda p: p)
        monkeypatch.setattr(
            tools_api, "_identity", lambda p: ("t1", "ws-1", "u1", "dev")
        )

        class _BrokenSubmitter:
            async def submit(self, *args, **kwargs):
                raise TaskSubmissionError("no nodes available")

        monkeypatch.setattr(
            "src.tasks.submitter.get_task_submitter", lambda: _BrokenSubmitter()
        )
        return TestClient(_build_app(tools_api.router))

    def test_submission_failure_answers_503_sandbox_unavailable(self, shell_client):
        response = shell_client.post(
            "/rt/v1/tools/shell",
            json={"workspace_path": "/w/copy", "command": "ls", "timeout_s": 5},
        )

        assert response.status_code == 503
        body = response.json()
        assert body["error_code"] == "SANDBOX_UNAVAILABLE"
        assert "no nodes available" in body.get("detail", "") + body.get("message", "")
        assert response.headers.get("Retry-After") == "5"


class TestTranslateOSError:
    def test_enospc_becomes_insufficient_storage(self):
        exc = OSError(errno.ENOSPC, "No space left on device")

        translated = translate_oserror(exc)

        assert translated.code == ErrorCode.INSUFFICIENT_STORAGE
        assert translated.status_code == 507
        assert translated.to_response_dict()["error_code"] == "INSUFFICIENT_STORAGE"

    def test_other_oserrors_keep_the_generic_500_shape(self):
        """Reclassifying every OSError would silently change dozens of
        responses; only ENOSPC is a user-actionable contract verdict."""
        for exc in (
            FileNotFoundError("gone"),
            PermissionError(errno.EACCES, "denied"),
            IsADirectoryError(errno.EISDIR, "is a directory"),
        ):
            translated = translate_oserror(exc)
            assert translated.code == ErrorCode.INTERNAL_ERROR, type(exc).__name__
            assert translated.status_code == 500

    def test_a_real_enospc_write_is_translated(self):
        """End-to-end against the OS: an actual write to a full device (a tiny
        tmpfs on macOS/Linux both support this) produces the same errno the
        translation keys on."""
        try:
            # /dev/full answers any write with ENOSPC on Linux; skip elsewhere.
            if not os.path.exists("/dev/full"):
                pytest.skip("no /dev/full on this platform")
            with open("/dev/full", "w") as full:
                full.write("x" * 1024)
        except OSError as exc:
            translated = translate_oserror(exc)
            assert translated.code == ErrorCode.INSUFFICIENT_STORAGE
            assert translated.status_code == 507
        else:  # pragma: no cover — /dev/full never accepts writes
            pytest.fail("write to /dev/full unexpectedly succeeded")
