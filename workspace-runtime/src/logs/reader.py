"""Read the workspace log directory.

The write side keeps files small and append-only, which lets the read side stay
equally simple: no index, no daemon, no cached state. A query reads the tail of
each candidate file, filters lines in memory and merges them newest-first.

Three kinds of file are surfaced, all under one uniform entry shape so the UI
does not care where a line came from:

* ``logs/service/*.ndjson`` — the platform's own timeline (canonical schema);
* ``logs/runs/*.ndjson``    — task-scoped streams (same schema);
* ``sql-warehouse/logs/*``  — SQLMesh's own text logs, parsed leniently. The
  platform does not control that format, so unparseable lines are surfaced
  verbatim rather than dropped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.files.layout import (
    SQL_WAREHOUSE_DIR,
    errors_dir,
    legacy_errors_dir,
    run_logs_dir,
    service_logs_dir,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

# How much of each file's tail one query may read. A log line is ~300 bytes, so
# this is a few thousand lines per file — far more than a screen, and bounded
# regardless of how large the file has grown.
DEFAULT_TAIL_BYTES = 512 * 1024

MAX_LIMIT = 1000
DEFAULT_LIMIT = 200

# Source name for SQLMesh's own text logs, which the platform only reads.
SQLMESH_CLI_SOURCE = "sqlmesh-cli"

# "2026-08-21 10:00:00,123 - INFO - sqlmesh.core.context - message". The logger
# segment is optional and must be a dotted identifier, so a message that merely
# contains " - " is not mistaken for one.
_TEXT_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]?\d*)\s*[-|]\s*"
    r"(?P<level>[A-Z]+)\s*[-|]\s*"
    r"(?:(?P<logger>[\w.]+)\s*[-|]\s*)?(?P<rest>.*)$"
)

_LEVEL_ALIASES = {"warn": "warning", "warning": "warning", "fatal": "critical"}


@dataclass(frozen=True)
class LogQuery:
    """A bounded, newest-first query over one workspace's logs."""

    workspace_path: str
    sources: Tuple[str, ...] = ()
    levels: Tuple[str, ...] = ()
    search: str = ""
    since: str = ""
    limit: int = DEFAULT_LIMIT
    cursor: str = ""

    def normalized_limit(self) -> int:
        return max(1, min(self.limit or DEFAULT_LIMIT, MAX_LIMIT))


@dataclass
class LogPage:
    """One page of entries plus the cursor that continues it."""

    entries: List[Dict[str, Any]] = field(default_factory=list)
    next_cursor: Optional[str] = None
    sources: List[str] = field(default_factory=list)


# ── Discovery ──────────────────────────────────────────────────────────


def _log_files(workspace_path: str) -> List[Tuple[str, Path, bool]]:
    """Return ``(source, path, is_ndjson)`` for every readable log file."""
    found: List[Tuple[str, Path, bool]] = []

    for directory in (service_logs_dir(workspace_path), run_logs_dir(workspace_path)):
        for path in _iter_files(Path(directory), ".ndjson"):
            # "workspace-runtime.2026-08-21.ndjson" -> "workspace-runtime"
            found.append((path.name.split(".")[0], path, True))

    warehouse_logs = Path(workspace_path) / SQL_WAREHOUSE_DIR / "logs"
    for path in _iter_files(warehouse_logs, ""):
        found.append((SQLMESH_CLI_SOURCE, path, False))

    return found


def _iter_files(directory: Path, suffix: str) -> Iterable[Path]:
    try:
        if not directory.is_dir():
            return []
        return sorted(
            entry
            for entry in directory.iterdir()
            if entry.is_file() and (not suffix or entry.suffix == suffix)
        )
    except OSError:
        return []


def list_sources(workspace_path: str) -> List[str]:
    """Return the distinct source names currently present on disk."""
    return sorted({source for source, _, _ in _log_files(workspace_path)})


# ── Querying ───────────────────────────────────────────────────────────


def query_logs(query: LogQuery) -> LogPage:
    """Return the newest matching entries, plus a cursor for older ones."""
    limit = query.normalized_limit()
    wanted_sources = {s for s in query.sources if s}
    wanted_levels = {_normalize_level(level) for level in query.levels if level}
    search = query.search.lower()
    cursor = _parse_cursor(query.cursor)

    available: List[str] = []
    matched: List[Dict[str, Any]] = []

    for source, path, is_ndjson in _log_files(query.workspace_path):
        available.append(source)
        if wanted_sources and source not in wanted_sources:
            continue
        for entry in _read_tail(source, path, is_ndjson):
            if wanted_levels and entry["level"] not in wanted_levels:
                continue
            if query.since and entry["ts"] < query.since:
                continue
            if search and search not in entry["_haystack"]:
                continue
            if cursor and _sort_key(entry) >= cursor:
                continue
            matched.append(entry)

    matched.sort(key=_sort_key, reverse=True)
    page = matched[:limit]
    for entry in page:
        entry.pop("_haystack", None)

    next_cursor = None
    if len(matched) > limit:
        last = page[-1]
        next_cursor = f"{last['ts']}|{last['id']}"

    return LogPage(entries=page, next_cursor=next_cursor, sources=sorted(set(available)))


def _sort_key(entry: Dict[str, Any]) -> Tuple[str, str]:
    return (entry["ts"], entry["id"])


def _parse_cursor(cursor: str) -> Optional[Tuple[str, str]]:
    if not cursor or "|" not in cursor:
        return None
    ts, _, entry_id = cursor.partition("|")
    return (ts, entry_id)


def _normalize_level(level: str) -> str:
    lowered = (level or "info").lower()
    return _LEVEL_ALIASES.get(lowered, lowered)


def _read_tail(source: str, path: Path, is_ndjson: bool) -> List[Dict[str, Any]]:
    """Parse the tail of one file into entries. Never raises."""
    try:
        raw = _tail_bytes(path, DEFAULT_TAIL_BYTES)
    except OSError:
        return []

    entries: List[Dict[str, Any]] = []
    for index, line in enumerate(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        entry = (
            _parse_ndjson_line(source, line)
            if is_ndjson
            else _parse_text_line(source, line)
        )
        if entry is None:
            continue
        entry["id"] = f"{source}:{path.name}:{index}"
        entry["_haystack"] = line.lower()
        entries.append(entry)
    return entries


def _tail_bytes(path: Path, max_bytes: int) -> str:
    """Read at most the last ``max_bytes`` of a file, dropping a partial line."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()  # discard the partial first line
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def _parse_ndjson_line(source: str, line: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    entry = {
        "ts": str(payload.get("ts") or ""),
        "level": _normalize_level(str(payload.get("level") or "info")),
        "source": source,
        "service": payload.get("service") or source,
        "event": payload.get("event") or "",
        "fields": payload.get("fields") or {},
    }
    for key in (
        "trace_id",
        "workspace_id",
        "environment",
        "user_id",
        "operation",
        "error_code",
        "error_log_id",
        "run_id",
        "logger",
    ):
        if payload.get(key) is not None:
            entry[key] = payload[key]
    return entry


def _parse_text_line(source: str, line: str) -> Dict[str, Any]:
    """Parse a SQLMesh text log line, falling back to the raw text."""
    match = _TEXT_LINE_RE.match(line)
    if not match:
        return {
            "ts": "",
            "level": "info",
            "source": source,
            "service": source,
            "event": line,
            "fields": {},
        }
    entry = {
        "ts": match.group("ts").replace(" ", "T").replace(",", "."),
        "level": _normalize_level(match.group("level")),
        "source": source,
        "service": source,
        "event": match.group("rest"),
        "fields": {},
    }
    if match.group("logger"):
        entry["logger"] = match.group("logger")
    return entry


# ── Tailing (SSE) ──────────────────────────────────────────────────────


def tail_offsets(workspace_path: str) -> Dict[str, int]:
    """Snapshot current file sizes so a tail starts at "now"."""
    offsets: Dict[str, int] = {}
    for _, path, _ in _log_files(workspace_path):
        try:
            offsets[str(path)] = path.stat().st_size
        except OSError:
            continue
    return offsets


def read_appended(
    workspace_path: str, offsets: Dict[str, int]
) -> List[Dict[str, Any]]:
    """Return entries appended since the last call, advancing ``offsets``.

    A file that shrank was rotated or removed, so its offset resets to zero and
    the new content is picked up from the start.
    """
    fresh: List[Dict[str, Any]] = []
    for source, path, is_ndjson in _log_files(workspace_path):
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue

        previous = offsets.get(key)
        if previous is None:
            offsets[key] = size
            continue
        if size == previous:
            continue
        if size < previous:
            previous = 0

        try:
            with path.open("rb") as handle:
                handle.seek(previous)
                data = handle.read(DEFAULT_TAIL_BYTES)
        except OSError:
            continue
        offsets[key] = previous + len(data)

        for index, line in enumerate(data.decode("utf-8", errors="replace").splitlines()):
            line = line.strip()
            if not line:
                continue
            entry = (
                _parse_ndjson_line(source, line)
                if is_ndjson
                else _parse_text_line(source, line)
            )
            if entry is None:
                continue
            entry["id"] = f"{source}:{path.name}:{previous}:{index}"
            fresh.append(entry)

    fresh.sort(key=lambda item: (item["ts"], item["id"]))
    return fresh


# ── Error archive ──────────────────────────────────────────────────────


def _archive_dirs(workspace_path: str) -> List[Path]:
    """Current archive directory first, then the pre-``logs/`` location."""
    return [Path(errors_dir(workspace_path)), Path(legacy_errors_dir(workspace_path))]


def list_errors(workspace_path: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Return newest-first summaries of archived errors."""
    files: List[Path] = []
    for directory in _archive_dirs(workspace_path):
        files.extend(_iter_files(directory, ".json"))

    # Filenames start with a UTC timestamp, so lexical order is chronological.
    files.sort(key=lambda path: path.name, reverse=True)

    summaries: List[Dict[str, Any]] = []
    for path in files[: max(1, limit)]:
        payload = _load_json(path)
        if payload is None:
            continue
        summaries.append(
            {
                "error_id": payload.get("error_id", ""),
                "error_code": payload.get("error_code", ""),
                "message": payload.get("message", ""),
                "operation": payload.get("operation", ""),
                "occurred_at_utc": payload.get("occurred_at_utc", ""),
                "service": payload.get("service", ""),
            }
        )
    return summaries


def read_error(workspace_path: str, error_id: str) -> Optional[Dict[str, Any]]:
    """Return one archived error payload, or ``None``.

    The filename embeds the short id prefix, so the right file is usually found
    without opening any other.
    """
    short_id = error_id.split("-", 1)[0]
    for directory in _archive_dirs(workspace_path):
        for path in _iter_files(directory, ".json"):
            if f"_{short_id}_" not in path.name:
                continue
            payload = _load_json(path)
            if payload and payload.get("error_id") == error_id:
                return payload
    return None


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def utc_now_iso() -> str:
    """Timestamp helper shared by the API layer."""
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "LogPage",
    "LogQuery",
    "list_errors",
    "list_sources",
    "query_logs",
    "read_appended",
    "read_error",
    "tail_offsets",
    "utc_now_iso",
]
