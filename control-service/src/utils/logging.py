"""Structured logging configuration using structlog.

The control plane owns no working copy, so there is no per-workspace NDJSON
mirror here: that sink belongs to workspace-runtime, which is where the
workspace-attributable traffic lands.
"""

from datacode_common.logging.setup import get_logger as _get_logger
from datacode_common.logging.setup import setup_logging as _setup_logging

from src.config import settings

SERVICE_NAME = "control-service"


def setup_logging() -> None:
    """Configure stdout logging."""
    _setup_logging(settings.log_level.upper(), basic_format="%(message)s")


def get_logger(name: str):
    return _get_logger(name)
