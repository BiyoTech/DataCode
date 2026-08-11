"""Workspace config.yaml maintenance helpers.

Currently provides SQLMesh state isolation: each workspace must use its own
``state_schema`` so that multiple workspaces sharing a single engine/database
(e.g. the same MySQL) do not collide on SQLMesh state — snapshots, environments
and external models. Without this, workspace B can load workspace A's external
models straight out of the shared default ``sqlmesh`` state schema.
"""

from __future__ import annotations

import re
from pathlib import Path

from ruamel.yaml import YAML

from src.utils.logging import get_logger

logger = get_logger(__name__)

_STATE_SCHEMA_PREFIX = "sqlmesh__"


def _sanitize_identifier(name: str) -> str:
    """Reduce an arbitrary name to a safe SQL schema identifier fragment."""
    safe = re.sub(r"[^0-9a-zA-Z_]", "_", name).strip("_").lower()
    return safe or "workspace"


def workspace_state_schema(workspace_path: str, project: str | None = None) -> str:
    """Return the workspace-scoped SQLMesh state schema name."""
    name = project or Path(workspace_path).name
    return f"{_STATE_SCHEMA_PREFIX}{_sanitize_identifier(str(name))}"


def ensure_state_isolation(workspace_path: str) -> bool:
    """Ensure every gateway in the workspace config uses a workspace-scoped
    ``state_schema``.

    Idempotent and comment-preserving. A gateway that already declares a
    ``state_schema`` is left untouched (respects user overrides). Returns True
    only when the config file was actually modified.
    """
    config_path = Path(workspace_path) / "config.yaml"
    if not config_path.exists():
        return False

    yaml = YAML()
    yaml.preserve_quotes = True
    try:
        data = yaml.load(config_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - never let config parsing break loading
        logger.warning("state_isolation_parse_failed", path=str(config_path), error=str(e))
        return False

    if not isinstance(data, dict):
        return False
    gateways = data.get("gateways")
    if not isinstance(gateways, dict) or not gateways:
        return False

    state_schema = workspace_state_schema(workspace_path, data.get("project"))

    changed = False
    for gw in gateways.values():
        if not isinstance(gw, dict):
            continue
        if not gw.get("state_schema"):
            gw["state_schema"] = state_schema
            changed = True

    if changed:
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f)
            logger.info(
                "state_isolation_applied",
                path=str(config_path),
                state_schema=state_schema,
            )
        except OSError as e:
            logger.warning("state_isolation_write_failed", path=str(config_path), error=str(e))
            return False

    return changed
