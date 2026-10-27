"""Config-only access to a working copy's SQLMesh configuration.

These helpers answer questions that need the PARSED ``config.yaml`` — gateway
names, dialects, connection settings — without ever constructing a
``sqlmesh.Context``. Context construction execs the user's Python models and
macros (threat T2) and is only allowed inside the executor sandbox; anything
the platform process needs must therefore come from the config file alone.

Known boundary, honestly recorded: SQLMesh also accepts a ``config.py``,
which IS user code and would execute here on parse. Most projects use the
YAML form; moving the config parse itself into the sandbox is a recorded
follow-up, and until then this module is the narrowed remainder of the
in-process surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from sqlmesh.core.config import Config

from src.utils.logging import get_logger

logger = get_logger(__name__)

# All config file names SQLMesh recognizes for a project root.
_CONFIG_FILENAMES = ("config.yaml", "config.yml", "config.py")


def load_workspace_config(workspace_path: str) -> Config:
    """Parse a workspace's SQLMesh ``config.yaml`` WITHOUT creating a Context.

    Returns the fully-parsed :class:`~sqlmesh.core.config.Config`, which
    exposes ``gateways`` and ``default_gateway_name``. Crucially this does NOT
    open any database connection (unlike ``Context(load=True)`` which eagerly
    reads the state DB), so it is safe to call even when the target warehouse
    is unreachable.

    Raises ``ConfigError`` on malformed config.
    """
    from sqlmesh.core.config.loader import load_config_from_paths

    from src.files.layout import sqlmesh_root

    project_root = Path(sqlmesh_root(workspace_path))
    project_paths = [project_root / name for name in _CONFIG_FILENAMES]
    return load_config_from_paths(
        Config,
        project_paths=project_paths,
        config_name="config",
    )


def gateway_dialect(config: Config, gateway: Optional[str] = None) -> str:
    """The SQL dialect of a gateway ("" when it cannot be determined).

    Used by format-on-save and other cheap path-aware behaviors that must not
    pay for a Context load.
    """
    gw_name = gateway or config.default_gateway_name
    gw_config = config.gateways.get(gw_name) if gw_name else None
    if gw_config is None and config.gateways:
        gw_config = next(iter(config.gateways.values()))
    if gw_config is None or gw_config.connection is None:
        return ""
    # ``DIALECT`` is a ClassVar on every connection config class.
    return (getattr(type(gw_config.connection), "DIALECT", "") or "").lower()


def engine_adapter_for(workspace_path: str, gateway: Optional[str] = None):
    """Build ONE gateway's engine adapter from the parsed config.

    This is the credential-bearing connection for warehouse introspection
    (schema discovery, external-model import). Building it from the config
    instead of from a Context is the point: the same introspection used to
    require ``Context(load=True)``, which execs the user's models — the exact
    exposure the P6 close-out removes.

    The caller owns closing the adapter.
    """
    config = load_workspace_config(workspace_path)

    gw_name = gateway or config.default_gateway_name
    gw_config = config.gateways.get(gw_name) if gw_name else None
    if gw_config is None and config.gateways:
        gw_config = next(iter(config.gateways.values()))
    if gw_config is None or gw_config.connection is None:
        raise ValueError(f"No connection configured for gateway '{gw_name or 'default'}'")
    return gw_config.connection.create_engine_adapter()


def ping_gateway(workspace_path: str, gateway: Optional[str] = None) -> None:
    """Probe connectivity of a gateway's warehouse (raises on failure).

    Builds only the engine adapter for the requested gateway (from parsed
    config) and issues a lightweight ``ping``. No SQLMesh Context / state DB
    load is involved, so this fails fast on an unreachable warehouse instead
    of paying the full context-load cost.

    The caller is expected to wrap this in ``asyncio.to_thread`` +
    ``asyncio.wait_for`` to enforce an overall short timeout, since some
    drivers may not honor their own connect-timeout in every failure mode.
    """
    adapter = engine_adapter_for(workspace_path, gateway)
    try:
        # ``ping`` runs a trivial round-trip (e.g. SELECT 1) and raises on any
        # connection/auth failure.
        adapter.ping()
    finally:
        try:
            adapter.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


def clear_project_cache(project_root: str) -> None:
    """Remove a project's local SQLMesh ``.cache`` (regenerable).

    Part of the state-schema repair contract: when ``config.yaml``'s
    ``state_schema`` is rewritten, the cache must go so the next load reads
    snapshots from the corrected schema instead of stale ones.
    """
    import shutil

    cache_dir = Path(project_root) / ".cache"
    if cache_dir.is_dir():
        shutil.rmtree(cache_dir, ignore_errors=True)
        logger.info("workspace_cache_cleared", project_root=project_root)
