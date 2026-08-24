"""Tests for the workspace log sink.

The sink writes into the user's own working copy from a process that serves
many workspaces concurrently, so the properties worth locking are the ones that
protect that directory: lines never interleave, secrets never land, the file
never grows without bound, and a failure here never breaks the request that
produced the log line.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agenticdata_common.logging.workspace_sink import (
    CHANNEL_RUNS,
    WorkspaceLogSink,
    WorkspaceSinkConfig,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws_test"
    ws.mkdir()
    return ws


def _sink(**overrides: Any) -> WorkspaceLogSink:
    return WorkspaceLogSink("sqlmesh-service", WorkspaceSinkConfig(**overrides))


def _service_dir(workspace: Path) -> Path:
    return workspace / "logs" / "service"


def _read_lines(workspace: Path) -> List[Dict[str, Any]]:
    files = sorted(_service_dir(workspace).glob("*.ndjson"))
    lines: List[Dict[str, Any]] = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                lines.append(json.loads(line))
    return lines


def _event(**fields: Any) -> Dict[str, Any]:
    return {"event": "unit_test_event", "level": "info", **fields}


# ---------------------------------------------------------------------------
# Processor behaviour
# ---------------------------------------------------------------------------


def test_processor_writes_record_and_passes_event_through(workspace: Path) -> None:
    sink = _sink()
    event_dict = _event(workspace_path=str(workspace), trace_id="t-1", host="github.com")

    returned = sink(None, "info", dict(event_dict))
    sink.close()

    # stdout rendering must be untouched.
    assert returned["event"] == "unit_test_event"

    lines = _read_lines(workspace)
    assert len(lines) == 1
    assert lines[0]["service"] == "sqlmesh-service"
    assert lines[0]["trace_id"] == "t-1"
    # Unknown keys land under "fields" so the line shape stays predictable.
    assert lines[0]["fields"] == {"host": "github.com"}


def test_processor_skips_records_without_a_workspace(workspace: Path) -> None:
    sink = _sink()
    sink(None, "info", _event())
    sink.close()

    assert not _service_dir(workspace).exists()


def test_processor_honours_the_level_threshold(workspace: Path) -> None:
    sink = _sink(min_level="warning")
    sink(None, "info", _event(workspace_path=str(workspace)))
    sink(None, "error", _event(workspace_path=str(workspace), level="error"))
    sink.close()

    lines = _read_lines(workspace)
    assert [line["level"] for line in lines] == ["error"]


def test_disabled_sink_writes_nothing(workspace: Path) -> None:
    sink = _sink(enabled=False)
    sink(None, "error", _event(workspace_path=str(workspace), level="error"))
    sink.close()

    assert not _service_dir(workspace).exists()


# ---------------------------------------------------------------------------
# Content safety
# ---------------------------------------------------------------------------


def test_credentials_are_redacted(workspace: Path) -> None:
    sink = _sink()
    sink(
        None,
        "error",
        _event(
            workspace_path=str(workspace),
            level="error",
            stderr="remote: token=ghp_supersecret rejected",
            dsn="postgresql://user:hunter2@db:5432/app",
        ),
    )
    sink.close()

    fields = _read_lines(workspace)[0]["fields"]
    assert "ghp_supersecret" not in fields["stderr"]
    assert "***REDACTED***" in fields["stderr"]
    assert "hunter2" not in fields["dsn"]


def test_long_lines_are_truncated_but_stay_parseable(workspace: Path) -> None:
    sink = _sink(max_line_bytes=512)
    sink(
        None,
        "info",
        _event(workspace_path=str(workspace), payload="x" * 50_000, trace_id="t-9"),
    )
    sink.close()

    raw = next(_service_dir(workspace).glob("*.ndjson")).read_text(encoding="utf-8")
    assert len(raw.encode("utf-8")) <= 512 + 1  # + the newline terminator

    line = json.loads(raw)
    # The searchable identity survives; only the free-form payload is dropped.
    assert line["trace_id"] == "t-9"
    assert line["fields"] == {"_truncated": True}


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_rate_limit_drops_and_reports_the_gap(workspace: Path) -> None:
    sink = _sink(max_lines_per_sec=5)
    for _ in range(20):
        sink(None, "info", _event(workspace_path=str(workspace)))

    lines = _read_lines(workspace)
    assert len(lines) == 5

    # Once the bucket refills, the next line is preceded by an explicit notice
    # rather than the gap passing unmentioned.
    time.sleep(1.1)
    sink(None, "info", _event(workspace_path=str(workspace)))
    sink.close()

    events = [line["event"] for line in _read_lines(workspace)]
    assert "workspace_logs_dropped" in events
    notice = next(
        line for line in _read_lines(workspace) if line["event"] == "workspace_logs_dropped"
    )
    assert notice["fields"]["dropped"] == 15


def test_descriptor_cache_evicts_beyond_the_limit(tmp_path: Path) -> None:
    sink = _sink(max_open_files=2)
    for index in range(5):
        ws = tmp_path / f"ws{index}"
        ws.mkdir()
        sink(None, "info", _event(workspace_path=str(ws)))

    assert len(sink._handles) == 2
    sink.close()

    # Eviction must not lose data: every workspace still has its line.
    for index in range(5):
        assert _read_lines(tmp_path / f"ws{index}")


def test_expired_files_are_pruned_on_open(workspace: Path) -> None:
    stale = _service_dir(workspace)
    stale.mkdir(parents=True)
    old_file = stale / "sqlmesh-service.2020-01-01.ndjson"
    old_file.write_text('{"event": "ancient"}\n', encoding="utf-8")
    ancient = time.time() - 30 * 86400
    os.utime(old_file, (ancient, ancient))

    sink = _sink(retention_days=7)
    sink(None, "info", _event(workspace_path=str(workspace)))
    sink.close()

    assert not old_file.exists()
    assert len(_read_lines(workspace)) == 1


def test_retention_disabled_keeps_old_files(workspace: Path) -> None:
    stale = _service_dir(workspace)
    stale.mkdir(parents=True)
    old_file = stale / "sqlmesh-service.2020-01-01.ndjson"
    old_file.write_text('{"event": "ancient"}\n', encoding="utf-8")
    ancient = time.time() - 30 * 86400
    os.utime(old_file, (ancient, ancient))

    sink = _sink(retention_days=0)
    sink(None, "info", _event(workspace_path=str(workspace)))
    sink.close()

    assert old_file.exists()


# ---------------------------------------------------------------------------
# Concurrency and failure tolerance
# ---------------------------------------------------------------------------


def test_concurrent_writes_never_interleave(workspace: Path) -> None:
    """Every line must remain individually parseable under contention."""
    sink = _sink(max_lines_per_sec=0)  # unlimited: this test is about atomicity
    writers = 8
    per_writer = 25
    barrier = threading.Barrier(writers)

    def write(worker: int) -> None:
        barrier.wait()
        for index in range(per_writer):
            sink(
                None,
                "info",
                _event(
                    workspace_path=str(workspace),
                    worker=worker,
                    index=index,
                    filler="y" * 200,
                ),
            )

    threads = [threading.Thread(target=write, args=(worker,)) for worker in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    sink.close()

    lines = _read_lines(workspace)  # json.loads would raise on a torn line
    assert len(lines) == writers * per_writer
    assert {(line["fields"]["worker"], line["fields"]["index"]) for line in lines} == {
        (worker, index) for worker in range(writers) for index in range(per_writer)
    }


def test_write_failure_never_raises(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _sink()

    def explode(*_args: Any, **_kwargs: Any) -> int:
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "open", explode)

    # A logging failure must not propagate into the operation being logged.
    sink(None, "error", _event(workspace_path=str(workspace), level="error"))


def test_unwritable_workspace_never_raises(tmp_path: Path) -> None:
    sink = _sink()
    sink(None, "info", _event(workspace_path=str(tmp_path / "does" / "not" / "exist")))
    sink.close()


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def test_direct_write_targets_the_requested_channel(workspace: Path) -> None:
    sink = _sink()
    sink.write(
        str(workspace),
        {"ts": "2026-08-21T00:00:00Z", "level": "info", "event": "step_started"},
        channel=CHANNEL_RUNS,
        name="dagster-run-abc123",
    )
    sink.close()

    target = workspace / "logs" / CHANNEL_RUNS / "dagster-run-abc123.ndjson"
    assert json.loads(target.read_text(encoding="utf-8"))["event"] == "step_started"
