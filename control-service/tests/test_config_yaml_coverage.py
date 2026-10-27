"""Every key in ``conf/control-service.yaml`` must reach ``Settings``.

Same guard as workspace-runtime's (see that test's docstring for the shipped
regression this class of test exists to prevent): a YAML key with no mapping
entry, or a mapping value that is not a Settings field, is silently dropped.
Control-service has no nested blocks outside the scalar mapping.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from datacode_common.config import dropped_yaml_keys, unknown_field_names

from src.config import _YAML_FIELD_MAPPING, Settings

_SERVICE_YAML = Path(__file__).resolve().parents[2] / "conf" / "control-service.yaml"


def _shipped_yaml() -> dict:
    with open(_SERVICE_YAML) as handle:
        return yaml.safe_load(handle)


def test_no_shipped_yaml_key_is_silently_ignored():
    dropped = dropped_yaml_keys(_shipped_yaml(), _YAML_FIELD_MAPPING)
    assert dropped == [], (
        "conf/control-service.yaml declares keys with no mapping to a Settings "
        "field; they are silently ignored — add them to _YAML_FIELD_MAPPING"
    )


def test_every_mapping_value_is_a_real_settings_field():
    unknown = unknown_field_names(_YAML_FIELD_MAPPING, Settings.model_fields)
    assert unknown == [], (
        "_YAML_FIELD_MAPPING routes YAML values into Settings(**kwargs); these "
        f"field names do not exist on the model: {unknown}"
    )


def test_the_shipped_values_actually_land_on_settings():
    from src.config import _flatten_yaml

    flat = _flatten_yaml(_shipped_yaml())
    data = _shipped_yaml()
    assert flat["reconcile_sweep_repair"] == data["reconcile"]["sweep_repair"]
    assert (
        flat["operation_resume_grace_seconds"]
        == data["reconcile"]["operation_resume_grace_seconds"]
    )
