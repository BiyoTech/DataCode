"""Tests for per-workspace SQLMesh state isolation (config_ops)."""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

from src.workspace.config_ops import ensure_state_isolation, workspace_state_schema

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


def test_injects_state_schema_when_missing(tmp_path):
    _write(tmp_path, _CONFIG)

    changed = ensure_state_isolation(str(tmp_path))

    assert changed is True
    data = _load(tmp_path)
    assert data["gateways"]["mysql"]["state_schema"] == "sqlmesh__my_ws"
    # Existing keys are preserved.
    assert data["gateways"]["mysql"]["connection"]["host"] == "localhost"
    # Comment is preserved (ruamel round-trip).
    assert "# Gateway Connection" in (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_is_idempotent(tmp_path):
    _write(tmp_path, _CONFIG)
    assert ensure_state_isolation(str(tmp_path)) is True
    # Second run makes no change.
    assert ensure_state_isolation(str(tmp_path)) is False


def test_respects_existing_state_schema(tmp_path):
    text = _CONFIG.replace(
        "      user: root\n",
        "      user: root\n    state_schema: custom_state\n",
    )
    _write(tmp_path, text)

    changed = ensure_state_isolation(str(tmp_path))

    assert changed is False
    assert _load(tmp_path)["gateways"]["mysql"]["state_schema"] == "custom_state"


def test_schema_name_derives_from_project(tmp_path):
    assert workspace_state_schema(str(tmp_path), "My Project!") == "sqlmesh__my_project"
    # Falls back to directory name when no project given.
    assert workspace_state_schema("/a/b/Test-2") == "sqlmesh__test_2"


def test_missing_config_is_noop(tmp_path):
    assert ensure_state_isolation(str(tmp_path)) is False
