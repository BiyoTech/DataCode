"""Safe HTTP error responses for ontology API."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import HTTPException

from src.models import OntologySchemaError

logger = structlog.get_logger(__name__)


def safe_http_exception(
    status_code: int,
    code: str,
    public_message: str,
    exc: Exception | None = None,
    extra: dict[str, Any] | None = None,
) -> HTTPException:
    if exc is not None:
        logger.error("ontology_api_error", code=code, error=str(exc), exc_info=True)
    detail: dict[str, Any] = {"error": public_message, "code": code}
    if extra:
        detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def raise_from_exception(exc: Exception) -> None:
    """Map exceptions to safe client-facing HTTP errors."""
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, FileNotFoundError):
        raise safe_http_exception(
            404,
            "ONTOLOGY_NOT_FOUND",
            "Ontology resource not found",
            exc,
        )
    if isinstance(exc, KeyError):
        raise safe_http_exception(
            404,
            "ONTOLOGY_NOT_FOUND",
            "Ontology resource not found",
            exc,
        )
    # Schema validation failures carry actionable field-level details, so
    # surface them to the client (checked before the generic ValueError
    # branch since OntologySchemaError subclasses ValueError).
    if isinstance(exc, OntologySchemaError):
        raise safe_http_exception(
            400,
            "ONTOLOGY_SCHEMA_INVALID",
            str(exc),
            exc,
            extra={"ontology_name": exc.ontology_name, "errors": exc.errors},
        )
    if isinstance(exc, ValueError):
        raise safe_http_exception(
            400,
            "ONTOLOGY_INVALID",
            "Invalid ontology request",
            exc,
        )
    raise safe_http_exception(
        500,
        "ONTOLOGY_INTERNAL",
        "Internal server error",
        exc,
    )
