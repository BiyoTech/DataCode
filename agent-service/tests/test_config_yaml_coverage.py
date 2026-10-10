"""Every key in ``conf/agent-service.yaml`` must reach ``Settings``.

Same guard as workspace-runtime's (see that test's docstring for the shipped
regression this class of test exists to prevent), with one documented
exception: the ``llm.`` and ``agent.memory.`` blocks are flattened
procedurally in ``_flatten_yaml`` (multi-model list, nested dataclass), so
their keys are exempt from the scalar mapping — and covered instead by a
behavioural assertion that the nested blocks really do land on Settings.

``agent.default_execution_style`` was the reason this test exists here: the
YAML shipped it for months while nothing read it, so the documented knob
("default autonomy for new sessions") was a lie until it was wired to
``Settings.default_execution_style``.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from datacode_common.config import dropped_yaml_keys, unknown_field_names

from src.config import _YAML_FIELD_MAPPING, Settings, _flatten_yaml

_SERVICE_YAML = Path(__file__).resolve().parents[2] / "conf" / "agent-service.yaml"

# Flattened procedurally in _flatten_yaml (see module docstring).
_NESTED_BLOCKS = ("llm.", "agent.memory.")


def _shipped_yaml() -> dict:
    with open(_SERVICE_YAML) as handle:
        return yaml.safe_load(handle)


def test_no_shipped_yaml_key_is_silently_ignored():
    dropped = dropped_yaml_keys(
        _shipped_yaml(), _YAML_FIELD_MAPPING, handled_prefixes=_NESTED_BLOCKS
    )
    assert dropped == [], (
        "conf/agent-service.yaml declares keys with no path into Settings; they "
        "are silently ignored — add them to _YAML_FIELD_MAPPING, or flatten the "
        "block in _flatten_yaml and extend _NESTED_BLOCKS here"
    )


def test_every_mapping_value_is_a_real_settings_field():
    unknown = unknown_field_names(_YAML_FIELD_MAPPING, Settings.model_fields)
    assert unknown == [], (
        f"_YAML_FIELD_MAPPING names fields that Settings does not have: {unknown}"
    )


def test_the_nested_blocks_land_on_settings():
    flat = _flatten_yaml(_shipped_yaml())
    data = _shipped_yaml()

    assert flat["llm"].default_model == data["llm"]["default_model"]
    assert [m.id for m in flat["llm"].models] == [m["id"] for m in data["llm"]["models"]]
    assert (
        flat["memory_summarization"].model
        == data["agent"]["memory"]["summarization"]["model"]
    )


def test_default_execution_style_is_wired():
    """The documented knob must be read somewhere, not just declared."""
    flat = _flatten_yaml(_shipped_yaml())
    assert flat["default_execution_style"] == _shipped_yaml()["agent"][
        "default_execution_style"
    ]
