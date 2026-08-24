"""Workspace-scoped log sink — append NDJSON lines to ``{workspace}/logs/``.

The platform treats a workspace as a self-describing directory: models,
ontology, agent memory and the error archive all live there as files. Logs
follow the same rule, so the agent can read them with its ordinary file tools
and the UI can show them without a separate log store.

Layout::

    {workspace}/logs/
    ├── service/  {service}.{YYYY-MM-DD}.ndjson   # service timeline
    ├── runs/     {name}.ndjson                   # task-scoped streams
    └── errors/   *.json                          # rich error archive

The level is a FIELD on every line, never a directory: splitting by level
would scatter one request's causal chain across four files, which is exactly
what makes an incident hard to follow.

A workspace path is a per-user, per-environment working copy
(``{base}/{tenant}/{workspace}/{user}/envs/{env}``), so writing here inherits
tenant, workspace, user and environment isolation from the filesystem layout.

Design constraints:

* **Never raises** — a logging failure must not break the request that
  produced it. Every public entry point swallows exceptions.
* **Single-syscall append** — lines are written with one ``os.write`` to an
  ``O_APPEND`` descriptor so concurrent writers never interleave a line.
* **Bounded everywhere** — open descriptors (LRU), line length, write rate
  per workspace, and retention in days are all capped, because the target
  directory is the user's own disk.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from datacode_common.logging.redact import redact_value

logger = logging.getLogger(__name__)

# Channel sub-directories under {workspace}/logs/.
CHANNEL_SERVICE = "service"
CHANNEL_RUNS = "runs"

# structlog emits lowercase level names; "warn" is accepted as an alias so a
# configuration value written either way behaves the same.
_LEVEL_ORDER: Dict[str, int] = {
    "debug": 10,
    "info": 20,
    "warn": 30,
    "warning": 30,
    "error": 40,
    "critical": 50,
    "exception": 40,
}

# Keys promoted to the top level of a log line. Everything else a call site
# passes lands under "fields", which keeps the line shape predictable for the
# reader and the UI regardless of what a given call site logs.
_PROMOTED_KEYS = (
    "trace_id",
    "tenant_id",
    "workspace_id",
    "user_id",
    "environment",
    "operation",
    "error_code",
    "error_log_id",
    "run_id",
    "duration_ms",
)

# Keys consumed by the record builder itself and never copied into "fields".
_CONSUMED_KEYS = frozenset(
    {"event", "level", "timestamp", "logger", "workspace_path", *_PROMOTED_KEYS}
)

# Per-string cap applied before falling back to dropping "fields" entirely.
_FIELD_STRING_CAP = 1024


@dataclass
class _Budget:
    """Token bucket guarding one workspace's write rate."""

    tokens: float
    updated_at: float
    dropped: int = 0


@dataclass
class WorkspaceSinkConfig:
    """Tunables for :class:`WorkspaceLogSink`.

    Defaults are deliberately conservative: this writes into the user's own
    working copy, so a runaway loop must not be able to fill their disk.
    """

    enabled: bool = True
    min_level: str = "info"
    max_line_bytes: int = 8192
    max_open_files: int = 64
    max_lines_per_sec: int = 200
    retention_days: int = 7

    def level_threshold(self) -> int:
        return _LEVEL_ORDER.get((self.min_level or "info").lower(), 20)


class WorkspaceLogSink:
    """Append structured records to a workspace's ``logs/`` directory.

    Usable two ways:

    * as a structlog processor (``__call__``) that mirrors any record already
      carrying a ``workspace_path`` into ``logs/service/``;
    * as a direct writer (:meth:`write`) for task-scoped streams such as a
      pipeline run, by passing ``channel`` and ``name``.
    """

    def __init__(self, service: str, config: Optional[WorkspaceSinkConfig] = None):
        self.service = service
        self.config = config or WorkspaceSinkConfig()
        self._lock = threading.Lock()
        self._handles: "OrderedDict[str, int]" = OrderedDict()
        self._budgets: Dict[str, _Budget] = {}
        self._threshold = self.config.level_threshold()

    # ── structlog processor ────────────────────────────────────────────

    def __call__(self, _logger: Any, _method_name: str, event_dict: Dict[str, Any]):
        """Mirror ``event_dict`` into its workspace, then pass it through.

        Records without a ``workspace_path`` (service startup, tenant-level
        auth, background reapers) belong to no workspace and are left to
        stdout only.
        """
        try:
            workspace_path = event_dict.get("workspace_path")
            if workspace_path and self._level_enabled(event_dict.get("level")):
                self.write(str(workspace_path), self._build_record(event_dict))
        except Exception:  # pragma: no cover - defensive
            pass
        return event_dict

    # ── direct writer ──────────────────────────────────────────────────

    def write(
        self,
        workspace_path: str,
        record: Dict[str, Any],
        *,
        channel: str = CHANNEL_SERVICE,
        name: Optional[str] = None,
    ) -> None:
        """Append one NDJSON line. Best-effort: failures are swallowed.

        Args:
            workspace_path: Working-copy root; the target is
                ``{workspace_path}/logs/{channel}/{name}.ndjson``.
            record: Already-shaped log record (see :meth:`_build_record`).
            channel: Sub-directory, e.g. ``service`` or ``runs``.
            name: File stem. Defaults to ``{service}.{UTC date}`` so the
                service timeline rotates daily.
        """
        if not self.config.enabled:
            return
        try:
            stem = name or f"{self.service}.{datetime.now(timezone.utc):%Y-%m-%d}"
            target = Path(workspace_path) / "logs" / channel / f"{stem}.ndjson"

            with self._lock:
                dropped = self._consume_token(workspace_path)
                if dropped is None:
                    return
                if dropped > 0:
                    self._append(target, self._drop_notice(dropped))
                self._append(target, record)
        except Exception:
            # Logging must never break the operation that produced it.
            logger.warning("workspace_log_write_failed", exc_info=True)

    def close(self) -> None:
        """Close every cached descriptor. Safe to call repeatedly."""
        with self._lock:
            for fd in self._handles.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._handles.clear()

    # ── internals ──────────────────────────────────────────────────────

    def _level_enabled(self, level: Optional[str]) -> bool:
        return _LEVEL_ORDER.get((level or "info").lower(), 20) >= self._threshold

    def _build_record(self, event_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Reshape a structlog event dict into the canonical log line."""
        record: Dict[str, Any] = {
            "ts": event_dict.get("timestamp")
            or datetime.now(timezone.utc).isoformat(),
            "level": (event_dict.get("level") or "info").lower(),
            "service": self.service,
            "event": event_dict.get("event"),
        }
        if event_dict.get("logger"):
            record["logger"] = event_dict["logger"]
        for key in _PROMOTED_KEYS:
            if event_dict.get(key) is not None:
                record[key] = event_dict[key]

        fields = {
            key: value
            for key, value in event_dict.items()
            if key not in _CONSUMED_KEYS
        }
        if fields:
            record["fields"] = fields
        return record

    def _append(self, target: Path, record: Dict[str, Any]) -> None:
        """Serialize, redact and append one line. Caller holds the lock."""
        line = self._render(record)
        fd = self._descriptor(target)
        try:
            os.write(fd, line)
        except OSError:
            # The file may have been removed underneath us (workspace reset,
            # manual cleanup). Drop the stale descriptor and retry once.
            self._discard(str(target))
            os.write(self._descriptor(target), line)

    def _render(self, record: Dict[str, Any]) -> bytes:
        """Return a redacted, length-bounded NDJSON line as bytes."""
        safe = redact_value(record)
        line = json.dumps(safe, ensure_ascii=False, default=str)
        if len(line.encode("utf-8")) <= self.config.max_line_bytes:
            return (line + "\n").encode("utf-8")

        # Oversized: shrink the free-form part first, keeping the promoted
        # keys intact because they are what makes a line searchable.
        fields = safe.get("fields")
        if isinstance(fields, dict):
            safe["fields"] = {
                key: (
                    value[:_FIELD_STRING_CAP] + "...[truncated]"
                    if isinstance(value, str) and len(value) > _FIELD_STRING_CAP
                    else value
                )
                for key, value in fields.items()
            }
            line = json.dumps(safe, ensure_ascii=False, default=str)
            if len(line.encode("utf-8")) <= self.config.max_line_bytes:
                return (line + "\n").encode("utf-8")

        safe["fields"] = {"_truncated": True}
        line = json.dumps(safe, ensure_ascii=False, default=str)
        return (line + "\n").encode("utf-8")

    def _descriptor(self, target: Path) -> int:
        """Return an ``O_APPEND`` descriptor for ``target``, opening on miss."""
        key = str(target)
        fd = self._handles.get(key)
        if fd is not None:
            self._handles.move_to_end(key)
            return fd

        target.parent.mkdir(parents=True, exist_ok=True)
        # A cache miss means either the first write of the process or a date
        # rollover, both of which are exactly when pruning should happen.
        self._prune(target.parent)

        fd = os.open(key, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        self._handles[key] = fd
        while len(self._handles) > self.config.max_open_files:
            _, evicted = self._handles.popitem(last=False)
            try:
                os.close(evicted)
            except OSError:
                pass
        return fd

    def _discard(self, key: str) -> None:
        fd = self._handles.pop(key, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _prune(self, directory: Path) -> None:
        """Delete ``.ndjson`` files older than the retention window."""
        if self.config.retention_days <= 0:
            return
        cutoff = time.time() - self.config.retention_days * 86400
        try:
            for entry in directory.iterdir():
                if entry.suffix != ".ndjson":
                    continue
                if entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
        except OSError:
            pass

    def _consume_token(self, workspace_path: str) -> Optional[int]:
        """Take one token; return dropped-since-last-write, or ``None``.

        ``None`` means the caller must skip this line. A returned count is the
        number of lines dropped while the bucket was empty, so the sink can
        state the gap explicitly instead of silently losing data.
        """
        rate = float(self.config.max_lines_per_sec)
        if rate <= 0:
            return 0

        now = time.monotonic()
        budget = self._budgets.get(workspace_path)
        if budget is None:
            budget = _Budget(tokens=rate, updated_at=now)
            self._budgets[workspace_path] = budget
        else:
            elapsed = max(0.0, now - budget.updated_at)
            budget.tokens = min(rate, budget.tokens + elapsed * rate)
            budget.updated_at = now

        if budget.tokens < 1.0:
            budget.dropped += 1
            return None

        budget.tokens -= 1.0
        dropped, budget.dropped = budget.dropped, 0
        return dropped

    def _drop_notice(self, dropped: int) -> Dict[str, Any]:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": "warning",
            "service": self.service,
            "event": "workspace_logs_dropped",
            "fields": {
                "dropped": dropped,
                "reason": "rate_limit",
                "max_lines_per_sec": self.config.max_lines_per_sec,
            },
        }


def build_workspace_sink(service: str) -> Optional[WorkspaceLogSink]:
    """Build a sink for *service* from ``platform.yaml``, or ``None``.

    Returns ``None`` when workspace file logging is disabled so callers can
    pass the result straight to ``setup_logging(workspace_sink=...)``.
    """
    try:
        from datacode_common.config.platform import platform_config

        cfg = platform_config().logging.workspace_file
        if not cfg.enabled:
            return None
        return WorkspaceLogSink(service, WorkspaceSinkConfig(**cfg.model_dump()))
    except Exception:
        logger.warning("workspace_log_sink_disabled", exc_info=True)
        return None
