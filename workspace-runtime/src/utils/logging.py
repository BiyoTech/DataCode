"""Structured logging configuration using structlog."""

from datacode_common.logging.setup import get_logger as _get_logger
from datacode_common.logging.setup import setup_logging as _setup_logging
from datacode_common.logging.workspace_sink import build_workspace_sink

from src.config import settings

SERVICE_NAME = "workspace-runtime"


def setup_logging() -> None:
    """Configure stdout logging plus the workspace NDJSON mirror."""
    _setup_logging(
        settings.log_level.upper(),
        basic_format="%(message)s",
        workspace_sink=build_workspace_sink(SERVICE_NAME),
    )


def get_logger(name: str):
    return _get_logger(name)
