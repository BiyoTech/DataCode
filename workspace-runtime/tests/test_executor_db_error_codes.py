"""The sandbox classifies warehouse failures before they cross the wire.

Why this file exists: the runtime only ever receives a STRING. Once the
exception has been flattened into ``{"code": "op_failed", "message": "..."}``
there is no way to tell "the warehouse is unreachable" from "the platform
broke" except by matching text — and not matching it is exactly how

    connection to server at "10.0.0.1", port 5432 failed: timeout expired

reached the API as ``INTERNAL_ERROR`` / 500, with the only copy of the reason
in a file under ``logs/errors/`` that nobody had opened.

So the classification belongs where the exception still has a class: in
``_dispatch``, inside the sandbox. These tests pin that, plus the contract that
an unrecognised failure still collapses to the generic ``op_failed`` the
runtime has always handled.
"""

from __future__ import annotations

import builtins

import pytest

from src.executor import server

_OP = "metadata.models"


class _OperationalError(Exception):
    """Stands in for psycopg2.OperationalError.

    ``classify_db_error`` matches on the class NAME (the driver is not
    importable in every image), so a local class carrying that name is the
    honest fixture — it exercises the real rule rather than a patched one.
    """


_OperationalError.__name__ = "OperationalError"
_OperationalError.__module__ = "psycopg2"


def _failing(monkeypatch, raise_this: BaseException) -> None:
    """Point one op at an exception, so ``_dispatch`` classifies a real raise."""

    def _boom(_params):
        raise raise_this

    monkeypatch.setitem(server._OPS, _OP, _boom)


def _dispatch() -> dict:
    return server._dispatch({"v": 1, "id": 7, "op": _OP, "params": {}})


def test_an_unreachable_warehouse_keeps_its_class_across_the_wire(monkeypatch):
    """The case that produced the 500: the host resolves, nothing answers."""
    _failing(
        monkeypatch,
        _OperationalError(
            'connection to server at "10.0.0.1", port 5432 failed: timeout expired'
        ),
    )

    response = _dispatch()

    assert response["ok"] is False
    assert response["error"]["code"] == "db_connection_error"
    # The driver's text survives: it names the host the user has to fix.
    assert "10.0.0.1" in response["error"]["message"]


def test_a_sql_error_is_an_engine_error_not_a_connection_error(monkeypatch):
    """The warehouse ANSWERED and rejected the statement: 422, not 503."""
    _failing(monkeypatch, _OperationalError('relation "missing_table" does not exist'))

    response = _dispatch()

    # A 503 here would tell the user to retry something that will never work.
    assert response["error"]["code"] == "db_engine_error"


def test_an_unrecognised_failure_stays_generic(monkeypatch):
    """Fail safe: classification is an improvement, never a requirement."""
    _failing(monkeypatch, RuntimeError("something the platform did wrong"))

    assert _dispatch()["error"] == {
        "code": "op_failed",
        "message": "something the platform did wrong",
    }


def test_classification_never_masks_an_explicit_op_error(monkeypatch):
    """``lint_failed`` and friends carry structure and are classified first."""
    from src.executor.op_error import OpError

    _failing(
        monkeypatch,
        OpError("lint_failed", "plan blocked by linter", lint_results=[{"a": 1}]),
    )

    response = _dispatch()

    assert response["error"]["code"] == "lint_failed"
    assert response["error"]["lint_results"] == [{"a": 1}]


def test_a_broken_classifier_degrades_to_the_generic_code(monkeypatch):
    """The sandbox must answer even when the errors module cannot be imported."""
    real_import = builtins.__import__

    def _no_errors(name, *args, **kwargs):
        if name.startswith("src.utils.errors"):
            raise ImportError("injected")
        return real_import(name, *args, **kwargs)

    _failing(monkeypatch, _OperationalError("connection refused"))
    monkeypatch.setattr(builtins, "__import__", _no_errors)

    assert _dispatch()["error"]["code"] == "op_failed"


@pytest.mark.parametrize(
    "code", ["db_connection_error", "db_engine_error", "db_transaction_error"]
)
def test_every_db_code_the_runtime_maps_is_one_the_protocol_defines(code):
    """A code the sandbox emits that the runtime does not map is a silent
    downgrade back to a 500 — so the two lists must agree."""
    from datacode_common import executor_protocol as proto

    from src.sqlmesh.metadata_context import _DB_ERRORS

    assert code in set(proto.DB_ERROR_CODES.values())
    assert code in _DB_ERRORS


def test_the_classifier_is_reachable_without_a_workspace():
    """It runs in a child whose env was cleared; nothing here may depend on it."""
    assert isinstance(server._db_error_code(RuntimeError("x")), str)  # type: ignore[attr-defined]
