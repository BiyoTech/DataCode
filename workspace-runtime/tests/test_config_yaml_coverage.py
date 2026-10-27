"""Every key in ``conf/workspace-runtime.yaml`` must reach ``Settings``.

The regression this guards against is silent and has shipped before:
``task.backend`` sat in the YAML while nothing mapped it, so every deployment
ran with the built-in default (``disabled``) and refused every sandboxed task —
including ``agent.shell``. A key present in the shipped YAML but absent from
``_YAML_FIELD_MAPPING`` is dropped on the floor, and a mapping VALUE that is
not a Settings field is dropped just as quietly by pydantic.

Two assertions, both structural against the real shipped file:

* no YAML key is dropped (every leaf is mapped — runtime has no nested
  blocks outside the scalar mapping);
* no mapping value names a field that does not exist.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from datacode_common.config import dropped_yaml_keys, unknown_field_names

from src.config import _YAML_FIELD_MAPPING, Settings

_SERVICE_YAML = Path(__file__).resolve().parents[2] / "conf" / "workspace-runtime.yaml"


def _shipped_yaml() -> dict:
    with open(_SERVICE_YAML) as handle:
        return yaml.safe_load(handle)


def test_no_shipped_yaml_key_is_silently_ignored():
    dropped = dropped_yaml_keys(_shipped_yaml(), _YAML_FIELD_MAPPING)
    assert dropped == [], (
        "conf/workspace-runtime.yaml declares keys with no mapping to a Settings "
        "field; they are silently ignored — add them to _YAML_FIELD_MAPPING (or "
        "extend the mapping guard if they are a nested block)"
    )


def test_every_mapping_value_is_a_real_settings_field():
    unknown = unknown_field_names(_YAML_FIELD_MAPPING, Settings.model_fields)
    assert unknown == [], (
        "_YAML_FIELD_MAPPING routes YAML values into Settings(**kwargs); these "
        "field names do not exist on the model, so pydantic drops the value "
        f"silently: {unknown}"
    )


def test_the_shipped_values_actually_land_on_settings():
    """Behavioural spot-check on a few load-bearing keys."""
    from src.config import _flatten_yaml

    flat = _flatten_yaml(_shipped_yaml())
    assert flat["task_backend"] == _shipped_yaml()["task"]["backend"]
    assert flat["dagster_mode"] == _shipped_yaml()["dagster"]["mode"]
    assert flat["env_lock_backend"] == _shipped_yaml()["concurrency"]["env_lock_backend"]
