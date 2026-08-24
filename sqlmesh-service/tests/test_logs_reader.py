"""Tests for the workspace log reader.

The reader is the only thing between a user and files the platform appends to
continuously, so the properties under test are: reads stay bounded, filters and
merging behave across sources, pagination does not skip or repeat, malformed
input is tolerated, and the pre-``logs/`` error archive is still reachable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.logs import reader


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws_test"
    (ws / "logs" / "service").mkdir(parents=True)
    return ws


def _write_ndjson(workspace: Path, filename: str, records: List[Dict[str, Any]]) -> None:
    target = workspace / "logs" / "service" / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _record(ts: str, **overrides: Any) -> Dict[str, Any]:
    return {
        "ts": ts,
        "level": "info",
        "service": "sqlmesh-service",
        "event": "unit_event",
        **overrides,
    }


def _query(workspace: Path, **overrides: Any) -> reader.LogQuery:
    return reader.LogQuery(workspace_path=str(workspace), **overrides)


# ---------------------------------------------------------------------------
# Basic reading
# ---------------------------------------------------------------------------


def test_returns_newest_first(workspace: Path) -> None:
    _write_ndjson(
        workspace,
        "sqlmesh-service.2026-08-21.ndjson",
        [
            _record("2026-08-21T09:00:00Z", event="first"),
            _record("2026-08-21T09:00:01Z", event="second"),
            _record("2026-08-21T09:00:02Z", event="third"),
        ],
    )

    page = reader.query_logs(_query(workspace))

    assert [entry["event"] for entry in page.entries] == ["third", "second", "first"]
    assert page.next_cursor is None


def test_empty_workspace_yields_no_entries(tmp_path: Path) -> None:
    page = reader.query_logs(_query(tmp_path))

    assert page.entries == []
    assert page.sources == []


def test_malformed_lines_are_skipped(workspace: Path) -> None:
    target = workspace / "logs" / "service" / "sqlmesh-service.2026-08-21.ndjson"
    target.write_text(
        '{"ts": "2026-08-21T09:00:00Z", "level": "info", "event": "good"}\n'
        "not json at all\n"
        "[1, 2, 3]\n",
        encoding="utf-8",
    )

    page = reader.query_logs(_query(workspace))

    assert [entry["event"] for entry in page.entries] == ["good"]


def test_promoted_keys_and_fields_survive(workspace: Path) -> None:
    _write_ndjson(
        workspace,
        "sqlmesh-service.2026-08-21.ndjson",
        [
            _record(
                "2026-08-21T09:00:00Z",
                level="error",
                event="git_remote_failed",
                trace_id="t-1",
                error_code="GIT_REMOTE_ACCESS_DENIED",
                error_log_id="ab12-...",
                fields={"host": "github.com"},
            )
        ],
    )

    entry = reader.query_logs(_query(workspace)).entries[0]

    assert entry["trace_id"] == "t-1"
    assert entry["error_log_id"] == "ab12-..."
    assert entry["fields"] == {"host": "github.com"}


# ---------------------------------------------------------------------------
# Filtering and merging
# ---------------------------------------------------------------------------


def test_merges_sources_by_timestamp(workspace: Path) -> None:
    _write_ndjson(
        workspace,
        "sqlmesh-service.2026-08-21.ndjson",
        [_record("2026-08-21T09:00:00Z"), _record("2026-08-21T09:00:02Z")],
    )
    _write_ndjson(
        workspace,
        "dagster-service.2026-08-21.ndjson",
        [_record("2026-08-21T09:00:01Z", service="dagster-service")],
    )

    page = reader.query_logs(_query(workspace))

    assert [entry["source"] for entry in page.entries] == [
        "sqlmesh-service",
        "dagster-service",
        "sqlmesh-service",
    ]
    assert page.sources == ["dagster-service", "sqlmesh-service"]


def test_source_filter_excludes_other_files(workspace: Path) -> None:
    _write_ndjson(workspace, "sqlmesh-service.2026-08-21.ndjson", [_record("2026-08-21T09:00:00Z")])
    _write_ndjson(workspace, "dagster-service.2026-08-21.ndjson", [_record("2026-08-21T09:00:01Z")])

    page = reader.query_logs(_query(workspace, sources=("dagster-service",)))

    assert [entry["source"] for entry in page.entries] == ["dagster-service"]
    # Sources listing still reports everything available, so the UI can offer
    # the filter the user just narrowed away from.
    assert page.sources == ["dagster-service", "sqlmesh-service"]


def test_level_filter_treats_warn_as_warning(workspace: Path) -> None:
    _write_ndjson(
        workspace,
        "sqlmesh-service.2026-08-21.ndjson",
        [
            _record("2026-08-21T09:00:00Z", level="info"),
            _record("2026-08-21T09:00:01Z", level="warning", event="warned"),
            _record("2026-08-21T09:00:02Z", level="error", event="failed"),
        ],
    )

    page = reader.query_logs(_query(workspace, levels=("warn", "error")))

    assert [entry["event"] for entry in page.entries] == ["failed", "warned"]


def test_search_matches_anywhere_in_the_line(workspace: Path) -> None:
    _write_ndjson(
        workspace,
        "sqlmesh-service.2026-08-21.ndjson",
        [
            _record("2026-08-21T09:00:00Z", fields={"host": "github.com"}),
            _record("2026-08-21T09:00:01Z", event="other", fields={"host": "gitlab.com"}),
        ],
    )

    page = reader.query_logs(_query(workspace, search="GITLAB"))

    assert [entry["event"] for entry in page.entries] == ["other"]


def test_since_excludes_older_entries(workspace: Path) -> None:
    _write_ndjson(
        workspace,
        "sqlmesh-service.2026-08-21.ndjson",
        [
            _record("2026-08-21T08:00:00Z", event="old"),
            _record("2026-08-21T10:00:00Z", event="new"),
        ],
    )

    page = reader.query_logs(_query(workspace, since="2026-08-21T09:00:00Z"))

    assert [entry["event"] for entry in page.entries] == ["new"]


# ---------------------------------------------------------------------------
# Pagination and bounds
# ---------------------------------------------------------------------------


def test_cursor_pages_backwards_without_gaps_or_repeats(workspace: Path) -> None:
    _write_ndjson(
        workspace,
        "sqlmesh-service.2026-08-21.ndjson",
        [_record(f"2026-08-21T09:00:{index:02d}Z", event=f"e{index}") for index in range(10)],
    )

    first = reader.query_logs(_query(workspace, limit=4))
    assert [entry["event"] for entry in first.entries] == ["e9", "e8", "e7", "e6"]
    assert first.next_cursor is not None

    second = reader.query_logs(_query(workspace, limit=4, cursor=first.next_cursor))
    assert [entry["event"] for entry in second.entries] == ["e5", "e4", "e3", "e2"]

    third = reader.query_logs(_query(workspace, limit=4, cursor=second.next_cursor))
    assert [entry["event"] for entry in third.entries] == ["e1", "e0"]
    assert third.next_cursor is None


def test_limit_is_capped(workspace: Path) -> None:
    assert _query(workspace, limit=10_000).normalized_limit() == reader.MAX_LIMIT
    assert _query(workspace, limit=0).normalized_limit() == reader.DEFAULT_LIMIT


def test_only_the_file_tail_is_read(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A huge file must not be loaded whole, and no partial line may survive."""
    monkeypatch.setattr(reader, "DEFAULT_TAIL_BYTES", 2048)
    _write_ndjson(
        workspace,
        "sqlmesh-service.2026-08-21.ndjson",
        [
            _record(f"2026-08-21T09:{index // 60:02d}:{index % 60:02d}Z", filler="z" * 200)
            for index in range(200)
        ],
    )

    page = reader.query_logs(_query(workspace, limit=reader.MAX_LIMIT))

    # Every returned line parsed cleanly, and far fewer than 200 were read.
    assert 0 < len(page.entries) < 200


# ---------------------------------------------------------------------------
# External SQLMesh logs
# ---------------------------------------------------------------------------


def test_sqlmesh_text_logs_are_surfaced(workspace: Path) -> None:
    warehouse_logs = workspace / "sql-warehouse" / "logs"
    warehouse_logs.mkdir(parents=True)
    (warehouse_logs / "sqlmesh.log").write_text(
        "2026-08-21 09:00:00,123 - ERROR - sqlmesh.core.context - plan failed\n"
        "a line in no recognizable format\n",
        encoding="utf-8",
    )

    page = reader.query_logs(_query(workspace))

    assert page.sources == [reader.SQLMESH_CLI_SOURCE]
    parsed = next(entry for entry in page.entries if entry["level"] == "error")
    assert parsed["event"] == "plan failed"
    assert parsed["logger"] == "sqlmesh.core.context"
    # An unparseable line is shown verbatim rather than dropped: the platform
    # does not own this format and must not hide what it cannot read.
    assert any(
        entry["event"] == "a line in no recognizable format" for entry in page.entries
    )


# ---------------------------------------------------------------------------
# Tailing
# ---------------------------------------------------------------------------


def test_tail_returns_only_new_lines(workspace: Path) -> None:
    _write_ndjson(workspace, "sqlmesh-service.2026-08-21.ndjson", [_record("2026-08-21T09:00:00Z")])

    offsets = reader.tail_offsets(str(workspace))
    assert reader.read_appended(str(workspace), offsets) == []

    _write_ndjson(
        workspace, "sqlmesh-service.2026-08-21.ndjson", [_record("2026-08-21T09:00:01Z", event="new")]
    )
    fresh = reader.read_appended(str(workspace), offsets)

    assert [entry["event"] for entry in fresh] == ["new"]
    assert reader.read_appended(str(workspace), offsets) == []


def test_tail_recovers_from_rotation(workspace: Path) -> None:
    path = workspace / "logs" / "service" / "sqlmesh-service.2026-08-21.ndjson"
    _write_ndjson(workspace, path.name, [_record("2026-08-21T09:00:00Z") for _ in range(5)])

    offsets = reader.tail_offsets(str(workspace))

    # A shorter file means the old content is gone; start over rather than
    # seeking past the end and going silent forever.
    path.write_text(
        json.dumps(_record("2026-08-21T10:00:00Z", event="after_rotation")) + "\n",
        encoding="utf-8",
    )
    fresh = reader.read_appended(str(workspace), offsets)

    assert [entry["event"] for entry in fresh] == ["after_rotation"]


def test_tail_picks_up_a_newly_created_file(workspace: Path) -> None:
    offsets = reader.tail_offsets(str(workspace))

    _write_ndjson(workspace, "dagster-service.2026-08-21.ndjson", [_record("2026-08-21T09:00:00Z")])
    # A file discovered after the snapshot starts at its current end, so the
    # tail does not replay history the caller already paged through.
    assert reader.read_appended(str(workspace), offsets) == []

    _write_ndjson(
        workspace, "dagster-service.2026-08-21.ndjson", [_record("2026-08-21T09:00:01Z", event="live")]
    )
    assert [entry["event"] for entry in reader.read_appended(str(workspace), offsets)] == ["live"]


# ---------------------------------------------------------------------------
# Error archive
# ---------------------------------------------------------------------------


def _write_error(directory: Path, filename: str, payload: Dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(payload), encoding="utf-8")


def test_lists_errors_from_both_current_and_legacy_locations(workspace: Path) -> None:
    _write_error(
        workspace / "logs" / "errors",
        "20260821T093012123456_ab12_GIT.json",
        {"error_id": "ab12-x", "error_code": "GIT_REMOTE_ACCESS_DENIED", "message": "denied"},
    )
    _write_error(
        workspace / "errors",
        "20260101T000000000000_cd34_PLAN.json",
        {"error_id": "cd34-y", "error_code": "PLAN_FAILED", "message": "old"},
    )

    summaries = reader.list_errors(str(workspace))

    assert [item["error_id"] for item in summaries] == ["ab12-x", "cd34-y"]


def test_reads_a_single_error_including_the_legacy_location(workspace: Path) -> None:
    _write_error(
        workspace / "errors",
        "20260101T000000000000_cd34_PLAN.json",
        {"error_id": "cd34-y", "error_code": "PLAN_FAILED", "traceback": "..."},
    )

    payload = reader.read_error(str(workspace), "cd34-y")

    assert payload is not None
    assert payload["traceback"] == "..."
    assert reader.read_error(str(workspace), "missing-id") is None


def test_corrupt_archive_entries_are_skipped(workspace: Path) -> None:
    errors = workspace / "logs" / "errors"
    errors.mkdir(parents=True)
    (errors / "20260821T093012123456_ab12_GIT.json").write_text("{ broken", encoding="utf-8")

    assert reader.list_errors(str(workspace)) == []
