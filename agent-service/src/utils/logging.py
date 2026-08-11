"""Structured logging configuration using structlog."""

from agenticdata_common.logging.setup import get_logger as _get_logger
from agenticdata_common.logging.setup import setup_logging as _setup_logging

from src.config import settings


def setup_logging() -> None:
    _setup_logging(settings.log_level.upper())


def get_logger(name: str):
    return _get_logger(name)
