"""Structured logging setup."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import structlog


def setup_logging(
    level: str = "INFO",
    *,
    basic_format: str | None = None,
    workspace_sink: Optional[Callable[..., Any]] = None,
) -> None:
    """Configure structlog for JSON-based structured logging.

    Every log line this process emits carries ``cell_id`` bound once here
    (design 03, P1): multi-cell operations must be able to filter by cell
    from day one, even while only cell-0 exists.

    Args:
        level: Root log level name.
        basic_format: Format string for the stdlib handler behind structlog.
        workspace_sink: Optional processor mirroring records that carry a
            ``workspace_path`` into that workspace's ``logs/`` directory (see
            :class:`datacode_common.logging.workspace_sink.WorkspaceLogSink`).
            It runs just before rendering, so it sees the fully enriched event
            dict while stdout output stays byte-for-byte unchanged.
    """
    from datacode_common.cell import cell_id

    # Bind once per process: the cell does not change during a lifetime, and
    # contextvars-bound values would need clearing per task for no benefit.
    structlog.contextvars.bind_contextvars(cell_id=cell_id())

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]
    if workspace_sink is not None:
        processors.append(workspace_sink)
    processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    fmt = basic_format or "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        format=fmt,
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        level=getattr(logging, level.upper(), logging.INFO),
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
