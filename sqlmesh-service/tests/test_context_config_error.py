"""Tests for graceful handling of invalid workspace SQLMesh config.

A workspace whose config.yaml is unloadable (e.g. a gateway with missing
connection credentials) must surface as a clean 422 ContextConfigError rather
than a raw 500 that leaks the SQLMesh traceback / config internals.
"""

from __future__ import annotations

import pytest
from sqlmesh.utils.errors import ConfigError

from src.core import context_pool as cp_module
from src.core.context_pool import ContextPool
from src.utils.errors import ContextConfigError, ErrorCode
from src.workspace.identity import workspace_base_path

# A working copy of environment ``prod`` in workspace ``ws-t1-test2``.
_WORKSPACE_PATH = f"{workspace_base_path()}/t1/ws-t1-test2/user-admin-001/envs/prod"


def test_context_config_error_maps_to_422() -> None:
    err = ContextConfigError(
        _WORKSPACE_PATH,
        detail="Invalid field 'host'",
        workspace_id="ws-t1-test2",
        environment="prod",
    )
    assert err.status_code == 422
    assert err.code == ErrorCode.CONTEXT_CONFIG_INVALID
    # Public message identifies workspace + environment, never the on-disk path.
    assert "ws-t1-test2" in err.message
    assert "prod" in err.message
    assert workspace_base_path() not in err.message
    payload = err.to_dict()
    assert payload["error_code"] == "CONTEXT_CONFIG_INVALID"
    assert payload["detail"] == "Invalid field 'host'"


async def test_get_context_converts_configerror(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_config_error(*args, **kwargs):
        raise ConfigError(
            "Invalid 'mysql' connection config: Invalid field 'host'"
        )

    # Context construction happens via asyncio.to_thread(Context, **kwargs);
    # patch the symbol the pool uses.
    monkeypatch.setattr(cp_module, "Context", _raise_config_error)

    pool = ContextPool()
    with pytest.raises(ContextConfigError) as excinfo:
        await pool.get_context(_WORKSPACE_PATH, gateway="mysql")

    err = excinfo.value
    assert err.status_code == 422
    assert err.code == ErrorCode.CONTEXT_CONFIG_INVALID
    assert "ws-t1-test2" in err.message
    # The underlying (safe) reason is preserved in detail for debugging.
    assert "host" in (err.detail or "")
