"""Tests for the error archive module.

Verifies that actionable service errors are persisted as structured JSON
files under ``{workspace}/logs/errors/`` with correct schema, credential
redaction, SQL truncation, retention enforcement, and failure tolerance.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from src.utils.error_archive import ErrorArchive, ErrorArchiveContext
from src.utils.errors import ErrorCode, ServiceError, service_error_from_exception


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A fake workspace root; the archive creates ``logs/errors/`` under it."""
    ws = tmp_path / "ws_test"
    ws.mkdir()
    return ws


def _make_service_error(
    message: str = "cannot delete from table _versions",
    detail: str | None = "Root cause: ...",
    code: ErrorCode = ErrorCode.DB_ENGINE_ERROR,
    status_code: int = 422,
) -> ServiceError:
    return ServiceError(
        code=code,
        message=message,
        detail=detail,
        status_code=status_code,
    )


def _make_exception(message: str = "boom") -> Exception:
    try:
        raise ValueError(message)
    except ValueError as e:
        return e


# ---------------------------------------------------------------------------
# record() — happy path
# ---------------------------------------------------------------------------


def test_record_writes_json_file(workspace: Path) -> None:
    """A single error produces one JSON file with the full schema."""
    exc = _make_exception("query failed")
    svc_err = _make_service_error()

    error_id = ErrorArchive.record(
        exc=exc,
        service_error=svc_err,
        archive_context=ErrorArchiveContext(
            workspace_path=str(workspace),
            operation="query.execute",
            gateway="pg",
            sql="SELECT 1",
            context={"row_limit": 1000},
        ),
    )

    assert error_id is not None
    errors_dir = workspace / "logs" / "errors"
    assert errors_dir.is_dir()

    files = list(errors_dir.glob("*.json"))
    assert len(files) == 1

    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["error_id"] == error_id
    assert payload["error_code"] == "DB_ENGINE_ERROR"
    assert payload["message"] == "cannot delete from table _versions"
    assert payload["operation"] == "query.execute"
    assert payload["workspace_path"] == str(workspace)
    assert payload["gateway"] == "pg"
    assert payload["sql"] == "SELECT 1"
    assert payload["sql_truncated"] is False
    assert payload["http_status"] == 422
    assert payload["service"] == "sqlmesh-service"
    assert payload["occurred_at_utc"].endswith("+00:00") or "T" in payload["occurred_at_utc"]
    assert "Traceback" in payload["traceback"]
    assert payload["root_cause_class"] == "ValueError"
    assert payload["context"] == {"row_limit": 1000}


def test_record_returns_error_id(workspace: Path) -> None:
    """The returned error_id matches the one in the JSON file."""
    exc = _make_exception()
    svc_err = _make_service_error()
    error_id = ErrorArchive.record(
        exc=exc,
        service_error=svc_err,
        archive_context=ErrorArchiveContext(
            workspace_path=str(workspace),
            operation="test.op",
        ),
    )
    assert error_id is not None
    files = list((workspace / "logs" / "errors").glob("*.json"))
    payload = json.loads(files[0].read_text())
    assert payload["error_id"] == error_id


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_record_redacts_password_in_message(workspace: Path) -> None:
    """``password=secret`` in the message is replaced with ``***REDACTED***``."""
    exc = _make_exception("connect failed")
    svc_err = ServiceError(
        code=ErrorCode.DB_CONNECTION_ERROR,
        message="connection refused: host=db password=mysecret123",
        status_code=503,
    )
    ErrorArchive.record(
        exc=exc,
        service_error=svc_err,
        archive_context=ErrorArchiveContext(
            workspace_path=str(workspace),
            operation="context.load",
        ),
    )
    files = list((workspace / "logs" / "errors").glob("*.json"))
    payload = json.loads(files[0].read_text())
    assert "mysecret123" not in payload["message"]
    assert "***REDACTED***" in payload["message"]


def test_record_redacts_connection_string_in_sql(workspace: Path) -> None:
    """``://user:pass@host`` in the SQL field is masked."""
    exc = _make_exception()
    svc_err = _make_service_error()
    ErrorArchive.record(
        exc=exc,
        service_error=svc_err,
        archive_context=ErrorArchiveContext(
            workspace_path=str(workspace),
            operation="query.execute",
            sql="SELECT * FROM pg_catalog WHERE url = 'postgresql://admin:s3cr3t@dbhost:5432/prod'",
        ),
    )
    files = list((workspace / "logs" / "errors").glob("*.json"))
    payload = json.loads(files[0].read_text())
    assert "s3cr3t" not in payload["sql"]
    assert "***" in payload["sql"]


def test_record_redacts_token_in_context(workspace: Path) -> None:
    """``token=...`` inside the context dict is also redacted."""
    exc = _make_exception()
    svc_err = _make_service_error()
    ErrorArchive.record(
        exc=exc,
        service_error=svc_err,
        archive_context=ErrorArchiveContext(
            workspace_path=str(workspace),
            operation="plan.apply",
            context={"auth_token": "token=abc123xyz", "env": "prod"},
        ),
    )
    files = list((workspace / "logs" / "errors").glob("*.json"))
    payload = json.loads(files[0].read_text())
    ctx = payload["context"]
    assert "abc123xyz" not in ctx["auth_token"]
    assert "***REDACTED***" in ctx["auth_token"]


# ---------------------------------------------------------------------------
# SQL truncation
# ---------------------------------------------------------------------------


def test_record_truncates_long_sql(workspace: Path) -> None:
    """SQL exceeding ``error_archive_max_sql_len`` is truncated with a flag."""
    exc = _make_exception()
    svc_err = _make_service_error()
    long_sql = "SELECT " + "x" * 10_000
    ErrorArchive.record(
        exc=exc,
        service_error=svc_err,
        archive_context=ErrorArchiveContext(
            workspace_path=str(workspace),
            operation="query.execute",
            sql=long_sql,
        ),
    )
    files = list((workspace / "logs" / "errors").glob("*.json"))
    payload = json.loads(files[0].read_text())
    assert payload["sql_truncated"] is True
    assert len(payload["sql"]) <= 4096


def test_record_does_not_truncate_short_sql(workspace: Path) -> None:
    """Short SQL is preserved as-is with ``sql_truncated=False``."""
    exc = _make_exception()
    svc_err = _make_service_error()
    short_sql = "SELECT 1"
    ErrorArchive.record(
        exc=exc,
        service_error=svc_err,
        archive_context=ErrorArchiveContext(
            workspace_path=str(workspace),
            operation="query.execute",
            sql=short_sql,
        ),
    )
    files = list((workspace / "logs" / "errors").glob("*.json"))
    payload = json.loads(files[0].read_text())
    assert payload["sql_truncated"] is False
    assert payload["sql"] == short_sql


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_record_enforces_retention(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When files exceed ``error_archive_retention``, oldest are pruned."""
    # Lower retention to 3 for this test.
    monkeypatch.setattr("src.config.settings.error_archive_retention", 3)

    for i in range(5):
        exc = _make_exception(f"error {i}")
        svc_err = _make_service_error(message=f"error {i}")
        ErrorArchive.record(
            exc=exc,
            service_error=svc_err,
            archive_context=ErrorArchiveContext(
                workspace_path=str(workspace),
                operation="query.execute",
            ),
        )

    files = sorted((workspace / "logs" / "errors").glob("*.json"))
    assert len(files) == 3
    # The newest 3 messages should be present; oldest 2 pruned.
    messages = [json.loads(f.read_text())["message"] for f in files]
    assert "error 0" not in messages
    assert "error 1" not in messages
    assert "error 2" in messages
    assert "error 3" in messages
    assert "error 4" in messages


# ---------------------------------------------------------------------------
# Disabled
# ---------------------------------------------------------------------------


def test_record_disabled_returns_none(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``error_archive_enabled`` is False, no file is written."""
    monkeypatch.setattr("src.config.settings.error_archive_enabled", False)
    exc = _make_exception()
    svc_err = _make_service_error()
    error_id = ErrorArchive.record(
        exc=exc,
        service_error=svc_err,
        archive_context=ErrorArchiveContext(
            workspace_path=str(workspace),
            operation="query.execute",
        ),
    )
    assert error_id is None
    assert not (workspace / "logs" / "errors").exists()


# ---------------------------------------------------------------------------
# Failure tolerance
# ---------------------------------------------------------------------------


def test_record_failure_never_raises(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the filesystem write fails, record() returns None instead of raising."""
    exc = _make_exception()
    svc_err = _make_service_error()

    def _mock_mkdir(*args, **kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr(Path, "mkdir", _mock_mkdir)

    error_id = ErrorArchive.record(
        exc=exc,
        service_error=svc_err,
        archive_context=ErrorArchiveContext(
            workspace_path=str(workspace),
            operation="query.execute",
        ),
    )
    assert error_id is None


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_record_concurrent_no_collision(workspace: Path) -> None:
    """Multiple concurrent writes produce distinct files (no collision)."""
    results: list[str | None] = [None] * 10
    barrier = threading.Barrier(10)

    def _worker(idx: int) -> None:
        barrier.wait()  # release all threads simultaneously
        exc = _make_exception(f"concurrent {idx}")
        svc_err = _make_service_error(message=f"concurrent {idx}")
        results[idx] = ErrorArchive.record(
            exc=exc,
            service_error=svc_err,
            archive_context=ErrorArchiveContext(
                workspace_path=str(workspace),
                operation="query.execute",
            ),
        )

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All 10 should have returned non-None error_ids.
    assert all(r is not None for r in results)
    # All error_ids should be distinct.
    assert len(set(results)) == 10

    files = list((workspace / "logs" / "errors").glob("*.json"))
    assert len(files) == 10
    error_ids_in_files = {json.loads(f.read_text())["error_id"] for f in files}
    assert error_ids_in_files == set(results)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# service_error_from_exception integration
# ---------------------------------------------------------------------------


def test_service_error_from_exception_sets_error_log_id(workspace: Path) -> None:
    """``service_error_from_exception`` with ``archive=`` populates ``error_log_id``."""
    exc = _make_exception("DB error")
    svc_err = service_error_from_exception(
        exc,
        code=ErrorCode.QUERY_FAILED,
        fallback_message="Query failed",
        archive=ErrorArchiveContext(
            workspace_path=str(workspace),
            operation="query.execute",
            sql="SELECT 1",
        ),
    )
    assert svc_err.error_log_id is not None
    files = list((workspace / "logs" / "errors").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["error_id"] == svc_err.error_log_id


def test_service_error_from_exception_without_archive(workspace: Path) -> None:
    """Without ``archive=``, no file is written and ``error_log_id`` stays None."""
    exc = _make_exception("DB error")
    svc_err = service_error_from_exception(
        exc,
        code=ErrorCode.QUERY_FAILED,
        fallback_message="Query failed",
    )
    assert svc_err.error_log_id is None
    assert not (workspace / "logs" / "errors").exists()


def test_to_response_dict_includes_error_log_id(workspace: Path) -> None:
    """``to_response_dict`` surfaces ``error_log_id`` when set."""
    exc = _make_exception()
    svc_err = service_error_from_exception(
        exc,
        code=ErrorCode.QUERY_FAILED,
        fallback_message="Query failed",
        archive=ErrorArchiveContext(
            workspace_path=str(workspace),
            operation="query.execute",
        ),
    )
    body = svc_err.to_response_dict()
    assert "error_log_id" in body
    assert body["error_log_id"] == svc_err.error_log_id


def test_to_response_dict_omits_error_log_id_when_none() -> None:
    """``to_response_dict`` does not include ``error_log_id`` when it is None."""
    svc_err = ServiceError(
        code=ErrorCode.QUERY_FAILED,
        message="fail",
        status_code=500,
    )
    body = svc_err.to_response_dict()
    assert "error_log_id" not in body


# ---------------------------------------------------------------------------
# Atomic write (no partial files)
# ---------------------------------------------------------------------------


def test_record_no_tmp_files_left(workspace: Path) -> None:
    """After a successful write, no ``.tmp`` files remain."""
    exc = _make_exception()
    svc_err = _make_service_error()
    ErrorArchive.record(
        exc=exc,
        service_error=svc_err,
        archive_context=ErrorArchiveContext(
            workspace_path=str(workspace),
            operation="query.execute",
        ),
    )
    tmp_files = list((workspace / "logs" / "errors").glob("*.tmp"))
    assert len(tmp_files) == 0
