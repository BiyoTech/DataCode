"""Tests for safe ontology API error responses."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.errors import raise_from_exception, safe_http_exception
from src.models import OntologySchemaError


def test_safe_http_exception_500_hides_internal_details() -> None:
    exc = RuntimeError("secret database password leaked")
    http_exc = safe_http_exception(
        500,
        "ONTOLOGY_INTERNAL",
        "Internal server error",
        exc,
    )
    assert http_exc.status_code == 500
    assert http_exc.detail == {
        "error": "Internal server error",
        "code": "ONTOLOGY_INTERNAL",
    }
    assert "password" not in str(http_exc.detail)


def test_raise_from_exception_maps_file_not_found() -> None:
    with pytest.raises(HTTPException) as raised:
        raise_from_exception(FileNotFoundError("models/foo.yaml"))
    assert raised.value.status_code == 404
    assert raised.value.detail["code"] == "ONTOLOGY_NOT_FOUND"
    assert "foo.yaml" not in str(raised.value.detail)


def test_raise_from_exception_maps_internal_error() -> None:
    with pytest.raises(HTTPException) as raised:
        raise_from_exception(RuntimeError("traceback details"))
    assert raised.value.status_code == 500
    assert raised.value.detail["code"] == "ONTOLOGY_INTERNAL"
    assert "traceback" not in str(raised.value.detail)


def test_raise_from_exception_maps_generic_value_error() -> None:
    with pytest.raises(HTTPException) as raised:
        raise_from_exception(ValueError("bad input"))
    assert raised.value.status_code == 400
    assert raised.value.detail["code"] == "ONTOLOGY_INVALID"
    # Generic ValueErrors stay opaque (no leaked raw message).
    assert "errors" not in raised.value.detail


def test_raise_from_exception_maps_schema_error_with_details() -> None:
    field_errors = [
        {
            "location": "entities.Order.attributes[3].type",
            "message": "Input should be 'categorical', 'time', 'numeric' or 'text'",
            "input": "timestamp",
            "type": "literal_error",
        }
    ]
    exc = OntologySchemaError("olist_benchmark_backup", field_errors)

    with pytest.raises(HTTPException) as raised:
        raise_from_exception(exc)

    detail = raised.value.detail
    assert raised.value.status_code == 400
    assert detail["code"] == "ONTOLOGY_SCHEMA_INVALID"
    assert detail["ontology_name"] == "olist_benchmark_backup"
    assert detail["errors"] == field_errors
    # The summary carries the failing ontology name and error count.
    assert "olist_benchmark_backup" in detail["error"]
    assert "1 error" in detail["error"]
