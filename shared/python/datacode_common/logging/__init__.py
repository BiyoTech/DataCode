"""Structured logging utilities."""

from datacode_common.logging.context import (
    bind_request_context,
    bind_workspace_context,
    clear_context,
)
from datacode_common.logging.redact import (
    redact_dict_in_place,
    redact_string,
    redact_value,
)
from datacode_common.logging.setup import get_logger, setup_logging
from datacode_common.logging.workspace_sink import (
    CHANNEL_RUNS,
    CHANNEL_SERVICE,
    WorkspaceLogSink,
    WorkspaceSinkConfig,
    build_workspace_sink,
)

__all__ = [
    "CHANNEL_RUNS",
    "CHANNEL_SERVICE",
    "WorkspaceLogSink",
    "WorkspaceSinkConfig",
    "bind_request_context",
    "bind_workspace_context",
    "build_workspace_sink",
    "clear_context",
    "get_logger",
    "redact_dict_in_place",
    "redact_string",
    "redact_value",
    "setup_logging",
]
