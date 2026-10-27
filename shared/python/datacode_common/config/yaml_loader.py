"""YAML configuration file loading utilities."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


def find_config_file(filename: str, *, package_file: Path) -> Optional[Path]:
    """Resolve YAML config file path relative to repo conf/ directory.

    Search order:
    1. ``$CONFIG_PATH/<filename>`` (when the env var is set)
    2. Walk up from ``package_file``'s directory until a ``conf/`` sibling is found
    3. ``./conf/<filename>`` relative to the current working directory
    """
    if env_path := os.getenv("CONFIG_PATH"):
        p = Path(env_path) / filename
        if p.exists():
            return p

    # Walk up from the package file's directory to find a ``conf/`` directory.
    # This handles packages at different depths (e.g. shared/python/pkg/mod.py
    # needs 5 levels up, while service/src/config.py needs 3).
    start = package_file.resolve().parent
    for ancestor in [start, *start.parents]:
        candidate = ancestor / "conf" / filename
        if candidate.exists():
            return candidate

    # Fallback: relative to CWD.
    p = Path("conf") / filename
    if p.exists():
        return p
    return None


def load_yaml_config(filename: str, *, package_file: Path) -> dict[str, Any]:
    config_path = find_config_file(filename, package_file=package_file)
    if config_path and config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def flatten_yaml_mapping(data: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    """Flatten nested YAML keys using dot-path to field name mapping."""
    result: dict[str, Any] = {}
    for yaml_key, field_name in mapping.items():
        parts = yaml_key.split(".")
        value: Any = data
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break
        if value is not None:
            result[field_name] = value
    return result


def drop_env_provided_keys(
    yaml_flat: dict[str, Any], *, env_prefix: str
) -> dict[str, Any]:
    """Remove keys the environment already provides, so env wins over YAML.

    pydantic-settings ranks its INIT source ABOVE its ENV source. Every service
    here loads its YAML file and passes the result as ``__init__`` kwargs, which
    therefore SHADOWS every ``<PREFIX>_<FIELD>`` environment variable — silently,
    because both layers produce a valid value and nothing reports which one won.

    That failure mode is not theoretical: a container setting
    ``WORKSPACE_RUNTIME_LEASE_ENABLED=true`` kept booting with leases disabled
    because ``conf/workspace-runtime.yaml`` also declares ``lease.enabled``.

    Dropping the env-provided keys here lets pydantic's env source fill them,
    restoring the documented precedence: env > service YAML > platform > defaults.

    Note ``os.environ`` membership, not truthiness: an intentionally empty value
    (``FOO=``) is still a decision by the deployment and must not fall back to
    the file.

    Args:
        yaml_flat: Flattened YAML values, keyed by pydantic field name.
        env_prefix: The service's ``env_prefix`` (e.g. ``WORKSPACE_RUNTIME_``).

    Returns:
        A new mapping without the keys the environment supplies.
    """
    kept: dict[str, Any] = {}
    shadowed: list[str] = []
    for key, value in yaml_flat.items():
        if f"{env_prefix}{key.upper()}" in os.environ:
            shadowed.append(key)
            continue
        kept[key] = value
    if shadowed:
        # Deliberately logged: "my env var had no effect" is otherwise an
        # invisible failure, and this line is the only place that knows.
        logger.debug(
            "config_env_override_applied prefix=%s fields=%s",
            env_prefix,
            ",".join(sorted(shadowed)),
        )
    return kept
