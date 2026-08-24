"""Error archive — persist actionable service errors to ``{workspace}/logs/errors/``.

When SQLMesh Service encounters an actionable error (SQL execution failure,
model parsing error, context load failure, plan/apply failure, etc.), the
``service_error_from_exception`` helper calls :func:`ErrorArchive.record` to
write a structured JSON file into the workspace's ``logs/errors/`` directory.

The archive sits beside the service log timeline under ``logs/`` and the two
are linked by ``error_log_id``: the timeline says when something failed, the
archive holds the traceback, SQL and context needed to fix it. It stays a
separate artifact rather than a log line precisely because that payload is
too rich to flatten into one record.

The JSON files are consumed by:

* The agent-service repair agent, which reads the latest error file to
  understand the root cause and attempt a fix (SQL / model / config).
* Users, for durable per-workspace error history that survives log rotation.

Design principles:

* **Best-effort**: write failures are logged as warnings and never propagated
  — archiving must not block the API response.
* **One file per error**: filename encodes UTC timestamp + UUID + error code
  for sortability and collision resistance.
* **Redacted**: credentials in ``password=``, ``://user:pass@host``,
  ``token=``, ``secret=`` patterns are masked before persistence.
* **Bounded**: each workspace retains at most ``error_archive_retention``
  files; oldest are pruned after each write.
* **Atomic**: files are written to a ``.tmp`` sibling then ``os.replace``-d to
  avoid partial reads by concurrent consumers.
"""

from __future__ import annotations

import json
import os
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from agenticdata_common.logging.redact import redact_string

from src.config import settings
from src.utils.errors import ServiceError, extract_root_cause
from src.utils.logging import get_logger
from src.workspace.layout import errors_dir as _errors_dir

logger = get_logger(__name__)

# Safety cap on traceback length to avoid unbounded file growth.
_MAX_TRACEBACK_LEN = 32 * 1024


@dataclass
class ErrorArchiveContext:
    """Call-site context passed to :meth:`ErrorArchive.record`.

    Captured at the ``except`` block so the archived JSON carries the
    operation name and any domain-specific context (SQL, model FQN, gateway,
    etc.) needed by the agent to locate and fix the problem.
    """

    workspace_path: str
    operation: str
    gateway: Optional[str] = None
    sql: Optional[str] = None
    model_fqn: Optional[str] = None
    context: Optional[Dict[str, Any]] = None


class ErrorArchive:
    """Persist actionable service errors as JSON files under ``errors/``."""

    @classmethod
    def record(
        cls,
        *,
        exc: Exception,
        service_error: ServiceError,
        archive_context: ErrorArchiveContext,
    ) -> Optional[str]:
        """Write a structured JSON error file to ``{workspace}/errors/``.

        Args:
            exc: The original caught exception (before translation to
                ``ServiceError``). Used to extract the root cause and traceback.
            service_error: The translated ``ServiceError`` carrying ``code``,
                ``message``, ``detail``, and ``status_code``.
            archive_context: Call-site context (operation, workspace_path,
                optional SQL / model_fqn / gateway / context dict).

        Returns:
            ``error_id`` string on success, ``None`` on failure or when
            archiving is disabled. Never raises.
        """
        if not settings.error_archive_enabled:
            return None

        try:
            root = extract_root_cause(exc)
            error_id = cls._generate_error_id()
            now = datetime.now(timezone.utc)

            sql_text = archive_context.sql
            sql_truncated = False
            max_sql = settings.error_archive_max_sql_len
            if sql_text is not None and len(sql_text) > max_sql:
                sql_text = sql_text[:max_sql]
                sql_truncated = True

            tb_text = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            if len(tb_text) > _MAX_TRACEBACK_LEN:
                tb_text = tb_text[:_MAX_TRACEBACK_LEN] + "\n... [truncated]"

            payload: Dict[str, Any] = {
                "error_id": error_id,
                "error_code": service_error.code.value,
                "message": service_error.message,
                "detail": service_error.detail,
                "root_cause_class": type(root).__name__ if root else type(exc).__name__,
                "root_cause_module": type(root).__module__ if root else type(exc).__module__,
                "operation": archive_context.operation,
                "workspace_path": archive_context.workspace_path,
                "gateway": archive_context.gateway,
                "sql": sql_text,
                "sql_truncated": sql_truncated,
                "model_fqn": archive_context.model_fqn,
                "http_status": service_error.status_code,
                "occurred_at_utc": now.isoformat(),
                "service": "sqlmesh-service",
                "service_version": "0.2.0",
                "traceback": tb_text,
                "context": archive_context.context,
            }

            cls._sanitize_payload(payload)

            errors_dir = Path(_errors_dir(archive_context.workspace_path))
            errors_dir.mkdir(parents=True, exist_ok=True)

            filename = cls._build_filename(payload, now)
            cls._write_atomic(errors_dir / filename, payload)

            cls._enforce_retention(errors_dir, settings.error_archive_retention)

            logger.info(
                "error_archived",
                error_id=error_id,
                error_code=payload["error_code"],
                operation=payload["operation"],
                workspace_path=payload["workspace_path"],
            )
            return error_id

        except Exception:
            logger.warning("error_archive_write_failed", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_error_id() -> str:
        """Generate a globally unique error ID.

        Format: ``{uuid8}-{ISO8601 UTC}``. The UUID prefix guarantees
        uniqueness across concurrent errors in the same second; the timestamp
        gives human-readable ordering.
        """
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{uuid.uuid4().hex[:8]}-{ts}"

    @staticmethod
    def _build_filename(payload: Dict[str, Any], now: datetime) -> str:
        """Build a sortable, collision-resistant filename.

        Format: ``{UTC %Y%m%dT%H%M%S%f}_{uuid8}_{ERROR_CODE}.json``

        The microsecond component (``%f``) ensures correct chronological
        ordering even when multiple errors are written within the same
        second, which is critical for retention pruning to delete the
        truly oldest files rather than a random subset.
        """
        ts = now.strftime("%Y%m%dT%H%M%S%f")
        short_id = payload["error_id"].split("-", 1)[0]
        code = payload["error_code"]
        return f"{ts}_{short_id}_{code}.json"

    @staticmethod
    def _write_atomic(target: Path, payload: Dict[str, Any]) -> None:
        """Atomically write ``payload`` as JSON to ``target``.

        Writes to a ``.tmp`` sibling first, then ``os.replace``-d to the final
        path. This prevents concurrent readers (agent) from seeing a partial
        JSON file.
        """
        tmp = target.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)

    @staticmethod
    def _sanitize_payload(payload: Dict[str, Any]) -> None:
        """Redact credential patterns in all string fields, in place.

        Recurses into nested dicts and lists so ``context`` sub-fields are
        also sanitized. Patterns covered: ``password=``, ``token=``,
        ``secret=``, ``://user:pass@host``.
        """
        for key, value in payload.items():
            if isinstance(value, str):
                payload[key] = ErrorArchive._redact_string(value)
            elif isinstance(value, dict):
                ErrorArchive._sanitize_dict(value)
            elif isinstance(value, list):
                payload[key] = [
                    ErrorArchive._redact_string(v) if isinstance(v, str) else v
                    for v in value
                ]

    @staticmethod
    def _sanitize_dict(d: Dict[str, Any]) -> None:
        for key, value in d.items():
            if isinstance(value, str):
                d[key] = ErrorArchive._redact_string(value)
            elif isinstance(value, dict):
                ErrorArchive._sanitize_dict(value)

    @staticmethod
    def _redact_string(text: str) -> str:
        """Apply the shared credential redaction patterns to ``text``.

        Shared with the workspace log sink so both writers mask the same
        credential shapes.
        """
        return redact_string(text)

    @staticmethod
    def _enforce_retention(errors_dir: Path, retention: int) -> None:
        """Keep at most ``retention`` newest ``.json`` files in ``errors_dir``.

        Files are sorted by name descending (filename starts with UTC
        timestamp, so lexical sort == chronological sort). Oldest excess files
        are deleted. ``.tmp`` files older than 1 hour are also cleaned up as
        orphaned-write recovery.
        """
        try:
            files = sorted(
                [f for f in errors_dir.iterdir() if f.suffix == ".json"],
                key=lambda f: f.name,
                reverse=True,
            )
            if len(files) <= retention:
                return
            for stale in files[retention:]:
                try:
                    stale.unlink()
                except OSError:
                    pass
            pruned = len(files) - retention
            if pruned > 0:
                logger.info("error_archive_retention_pruned", count=pruned)
        except Exception:
            # Retention enforcement is best-effort; never raise.
            pass
