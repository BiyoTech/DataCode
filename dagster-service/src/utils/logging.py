"""Structured logging configuration using structlog.

Reuses the shared agenticdata_common logging setup so every service emits
logs in the same format. All log messages MUST be in English and timestamps
are UTC (enforced by the shared setup).
"""

from agenticdata_common.logging.setup import get_logger as _get_logger
from agenticdata_common.logging.setup import setup_logging as _setup_logging

from src.config import settings


def setup_logging() -> None:
    _setup_logging(settings.log_level.upper(), basic_format="%(message)s")


def get_logger(name: str):
    return _get_logger(name)
