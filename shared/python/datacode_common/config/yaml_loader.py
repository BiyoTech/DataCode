"""YAML configuration file loading utilities."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml


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
