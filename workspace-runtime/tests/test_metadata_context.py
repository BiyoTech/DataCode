"""MetadataContextStore tests — the read-side cache and its freshness rules.

What these pin:
* cacheable ops are fetched from the sandbox ONCE and then served from cache;
* mark_dirty / invalidate force a re-fetch;
* the TTL is the backstop for edits made outside our API;
* execute-class ops (model detail, column lineage, query, ...) never cache;
* the sandbox's ``model_not_found`` op error becomes the API's 404 error;
* the no-gateway model listing aggregates every configured gateway and
  survives one gateway failing to load.
"""

from __future__ import annotations

from typing import Optional

import pytest

import src.sqlmesh.metadata_context as md_module
from src.sqlmesh.metadata_context import MetadataContextStore
from src.utils.errors import ModelNotFoundError, ServiceError

WS = "/data/workspaces/t1/ws1/user/env"


class FakeExecutorManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.results: dict[str, dict] = {}
        self.error: Optional[Exception] = None

    async def call(self, workspace_path, environment, op, params=None, *, gateway=""):
        self.calls.append((workspace_path, op, dict(params or {})))
        if self.error is not None:
            raise self.error
        return dict(self.results.get(op, {}))


@pytest.fixture
def manager(monkeypatch):
    fake = FakeExecutorManager()
    monkeypatch.setattr(md_module, "get_executor_manager", lambda: fake)
    return fake


@pytest.fixture
def store(manager, monkeypatch):
    monkeypatch.setattr(
        "src.workspace.identity.resolve_workspace_identity",
        lambda ws: _Identity(),
    )
    return MetadataContextStore(ttl_seconds=60.0)


class _Identity:
    environment = "dev"
    workspace_id = "ws1"
    user_id = "user"


class TestCaching:
    async def test_models_are_cached_per_call_params(self, store, manager):
        manager.results["metadata.models"] = {"models": [{"name": "a"}]}

        first = await store.models(WS, gateway="duck")
        second = await store.models(WS, gateway="duck")

        assert first == second == {"models": [{"name": "a"}]}
        # One sandbox round trip for two reads.
        assert len([c for c in manager.calls if c[1] == "metadata.models"]) == 1

    async def test_different_params_are_different_entries(self, store, manager):
        manager.results["metadata.models"] = {"models": []}

        await store.models(WS, environment="prod", gateway="duck")
        await store.models(WS, environment="dev", gateway="duck")

        ops = [c for c in manager.calls if c[1] == "metadata.models"]
        assert len(ops) == 2
        assert ops[0][2]["environment"] == "prod"

    async def test_mark_dirty_forces_refetch(self, store, manager):
        manager.results["metadata.models"] = {"models": [{"name": "a"}]}

        await store.models(WS, gateway="duck")
        store.mark_dirty(WS)
        await store.models(WS, gateway="duck")

        assert len([c for c in manager.calls if c[1] == "metadata.models"]) == 2

    async def test_invalidate_forces_refetch(self, store, manager):
        manager.results["metadata.environments"] = {"environments": {}}

        await store.environments(WS, gateway="duck")
        store.invalidate(WS)
        await store.environments(WS, gateway="duck")

        assert len(manager.calls) == 2

    async def test_ttl_expiry_forces_refetch(self, store, manager, monkeypatch):
        import time as time_module

        manager.results["metadata.models"] = {"models": []}
        clock = {"now": 1000.0}
        real_monotonic = time_module.monotonic

        def fake_monotonic():
            return clock["now"]

        monkeypatch.setattr(md_module.time, "monotonic", fake_monotonic)

        await store.models(WS, gateway="duck")
        clock["now"] = real_monotonic() + 61.0  # past the TTL
        await store.models(WS, gateway="duck")

        assert len([c for c in manager.calls if c[1] == "metadata.models"]) == 2

    async def test_execute_class_ops_never_cache(self, store, manager):
        manager.results["metadata.columns"] = {"name": "m"}
        manager.results["sqlmesh.query"] = {"columns": [], "rows": []}

        await store.model_detail(WS, "m")
        await store.model_detail(WS, "m")
        await store.query(WS, "SELECT 1")
        await store.query(WS, "SELECT 1")

        assert len([c for c in manager.calls if c[1] == "metadata.columns"]) == 2
        assert len([c for c in manager.calls if c[1] == "sqlmesh.query"]) == 2


class TestErrorTranslation:
    async def test_model_not_found_becomes_the_404_error(self, store, manager):
        from src.executor.process import ExecutorError

        manager.error = ExecutorError(
            "model_not_found", "model not found: db.missing"
        )

        with pytest.raises(ModelNotFoundError):
            await store.model_detail(WS, "db.missing", gateway="duck")

    async def test_other_op_errors_become_service_errors(self, store, manager):
        from src.executor.process import ExecutorError

        manager.error = ExecutorError("op_failed", "boom")

        with pytest.raises(ServiceError):
            await store.environments(WS, gateway="duck")

    async def test_validate_reports_op_failure_as_invalid(self, store, manager):
        from src.executor.process import ExecutorError

        manager.error = ExecutorError("context_load_error", "bad model syntax")

        verdict = await store.validate(WS)

        assert verdict["valid"] is False
        assert "bad model syntax" in verdict["errors"][0]

    async def test_validate_reports_success(self, store, manager):
        manager.results["context.refresh"] = {"models": 3}

        verdict = await store.validate(WS)

        assert verdict == {"valid": True, "errors": [], "warnings": []}


class TestGatewayAggregation:
    async def test_no_gateway_lists_every_gateway_merged(self, store, manager, monkeypatch):
        """One gateway failing must not blank the whole listing."""

        class FakeGatewayConfig:
            def __init__(self, name):
                self.name = name

        class FakeConfig:
            gateways = {"duck": FakeGatewayConfig("duck"), "broken": FakeGatewayConfig("broken")}
            default_gateway_name = "duck"

        import src.sqlmesh.config_access as config_access

        monkeypatch.setattr(
            config_access, "load_workspace_config", lambda ws: FakeConfig()
        )

        async def fake_call(workspace_path, environment, op, params=None, *, gateway=""):
            manager.calls.append((workspace_path, op, dict(params or {})))
            if gateway == "broken":
                raise md_module.ExecutorError("context_config_error", "no credentials")
            return {"models": [{"name": f"m_{gateway}", "fqn": f"db.m_{gateway}"}]}

        monkeypatch.setattr(manager, "call", fake_call)

        result = await store.models(WS)

        names = [m["name"] for m in result["models"]]
        assert names == ["m_duck"]
        # Both gateways were attempted; only one produced entries.
        gateways_called = {
            c[2].get("gateway", "")
            for c in manager.calls
            if c[1] == "metadata.models"
        }
        assert gateways_called  # aggregation actually queried gateways


class TestInvalidateUnderPath:
    def test_under_path_evicts_only_subtree(self, store, manager):
        store._cache["j:/data/workspaces/t1/ws1/userA/env:gw:metadata.models:{}"] = (
            md_module._CachedPayload({"models": []})
        )
        store._cache["j:/data/workspaces/t2/wsX/userB/env:gw:metadata.models:{}"] = (
            md_module._CachedPayload({"models": []})
        )

        evicted = store.invalidate_under_path("/data/workspaces/t1")

        assert evicted == 1
        assert store.cached_entry_count() == 1
