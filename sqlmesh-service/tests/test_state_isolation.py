"""Tests for the platform-managed SQLMesh state schema (config_ops).

The contract under test is the ownership model: the platform GENERATES the value
into ``config.yaml`` when a gateway has none, and REJECTS a gateway that declares
a different one. It never silently overrides, so the file a user reads is always
the value that takes effect. ``repair_state_schema`` is the explicit escape
hatch: once the user has SEEN the 409 and confirmed, it writes the managed value
back so the file returns to being the single truth.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from src.utils.errors import ServiceError, StateSchemaMismatchError
from src.workspace.config_ops import (
    reconcile_state_schema,
    repair_state_schema,
    workspace_state_schema,
)

_WORKSPACE_ID = "ws-t1-my-ws"
_EXPECTED = "sqlmesh__ws_t1_my_ws"

_CONFIG = """\
# Gateway Connection
project: my_ws
gateways:
  mysql:
    connection:
      type: mysql
      host: localhost
      user: root
default_gateway: mysql
model_defaults:
  dialect: mysql
"""


def _write(tmp_path: Path, text: str) -> Path:
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def _load(tmp_path: Path) -> dict:
    return YAML().load((tmp_path / "config.yaml").read_text(encoding="utf-8"))


def test_generates_state_schema_when_missing(tmp_path):
    _write(tmp_path, _CONFIG)

    result = reconcile_state_schema(str(tmp_path), _WORKSPACE_ID)

    assert result.changed is True
    assert result.expected == _EXPECTED
    assert result.generated_for == ("mysql",)

    data = _load(tmp_path)
    assert data["gateways"]["mysql"]["state_schema"] == _EXPECTED
    # Existing keys and comments survive the round-trip.
    assert data["gateways"]["mysql"]["connection"]["host"] == "localhost"
    assert "# Gateway Connection" in (tmp_path / "config.yaml").read_text(
        encoding="utf-8"
    )


def test_is_idempotent(tmp_path):
    _write(tmp_path, _CONFIG)

    assert reconcile_state_schema(str(tmp_path), _WORKSPACE_ID).changed is True
    # The value is now declared in the file: nothing left to write.
    assert reconcile_state_schema(str(tmp_path), _WORKSPACE_ID).changed is False


def test_rejects_deviating_state_schema(tmp_path):
    _write(
        tmp_path,
        _CONFIG.replace(
            "      user: root\n",
            "      user: root\n    state_schema: custom_state\n",
        ),
    )

    with pytest.raises(StateSchemaMismatchError) as excinfo:
        reconcile_state_schema(str(tmp_path), _WORKSPACE_ID)

    # The error must name both values so the user can fix the file.
    assert "custom_state" in excinfo.value.message
    assert _EXPECTED in excinfo.value.message
    # The structured detail lets the frontend render the mismatch and offer
    # the repair endpoint without parsing the message.
    assert excinfo.value.detail == {
        "gateway": "mysql",
        "expected": _EXPECTED,
        "actual": "custom_state",
        "config_file": "config.yaml",
        "repair_endpoint": "/api/v1/sqlmesh/config/state-schema/repair",
    }
    # The offending file is never rewritten.
    assert _load(tmp_path)["gateways"]["mysql"]["state_schema"] == "custom_state"


# --- Explicit repair (the escape hatch behind the 409) ---


def test_repairs_deviating_state_schema(tmp_path):
    _write(
        tmp_path,
        _CONFIG.replace(
            "      user: root\n",
            "      user: root\n    state_schema: custom_state\n",
        ),
    )

    result = repair_state_schema(str(tmp_path), _WORKSPACE_ID)

    assert result.changed is True
    assert result.generated_for == ("mysql",)
    assert result.expected == _EXPECTED
    assert _load(tmp_path)["gateways"]["mysql"]["state_schema"] == _EXPECTED

    # The repair restores the invariant: the next reconcile accepts the file.
    assert reconcile_state_schema(str(tmp_path), _WORKSPACE_ID).changed is False


def test_repair_fills_missing_values_too(tmp_path):
    _write(tmp_path, _CONFIG)

    result = repair_state_schema(str(tmp_path), _WORKSPACE_ID)

    assert result.generated_for == ("mysql",)
    assert _load(tmp_path)["gateways"]["mysql"]["state_schema"] == _EXPECTED


def test_repair_is_noop_when_already_correct(tmp_path):
    _write(tmp_path, _CONFIG)
    reconcile_state_schema(str(tmp_path), _WORKSPACE_ID)

    result = repair_state_schema(str(tmp_path), _WORKSPACE_ID)

    assert result.changed is False
    assert result.generated_for == ()


def test_repair_without_config_is_an_error(tmp_path):
    # Unlike reconcile (silent no-op), an explicit user action must not fail
    # silently: the caller would retry the plan and hit the same problem.
    with pytest.raises(ServiceError):
        repair_state_schema(str(tmp_path), _WORKSPACE_ID)


def test_generates_for_a_newly_added_gateway_only(tmp_path):
    _write(tmp_path, _CONFIG)
    reconcile_state_schema(str(tmp_path), _WORKSPACE_ID)

    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    _write(
        tmp_path,
        text.replace(
            "default_gateway: mysql",
            "  duckdb:\n    connection:\n      type: duckdb\ndefault_gateway: mysql",
        ),
    )

    result = reconcile_state_schema(str(tmp_path), _WORKSPACE_ID)

    assert result.generated_for == ("duckdb",)
    assert _load(tmp_path)["gateways"]["duckdb"]["state_schema"] == _EXPECTED


def test_schema_name_derives_from_workspace_id():
    assert workspace_state_schema("ws-t1-brazil-sales") == "sqlmesh__ws_t1_brazil_sales"
    # Deterministic and engine-safe: identifiers stay within 63 characters.
    long_id = "ws-tenant-" + ("a" * 120)
    schema = workspace_state_schema(long_id)
    assert len(schema) <= 63
    assert schema == workspace_state_schema(long_id)


def test_missing_config_is_noop(tmp_path):
    assert reconcile_state_schema(str(tmp_path), _WORKSPACE_ID).changed is False
