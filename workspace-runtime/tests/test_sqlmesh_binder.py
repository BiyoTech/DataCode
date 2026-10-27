"""SqlMeshBinder tests — the last in-process Context, and the contract around it.

This module owns the two write operations that still construct an EXECUTABLE
SQLMesh Context inside the platform process (a recorded P6 residual: the plan
build runs the user's models, the apply writes state, and splitting that across
the Q5 boundary is separate work). It is therefore the one place where a
regression is a security regression, and it had no tests at all.

What is pinned here, with the Context faked:

* the virtual-environment plan is built with the ZERO-COST options
  (``create_from`` + ``skip_backfill`` + ``include_unmodified``) — drop one and
  a "free" environment silently becomes a full backfill;
* a failure is REPORTED as drift, never raised, and the Context is closed even
  then (a leaked engine connection is worse than the failure);
* invalidate does BOTH steps — marking the state record is not what releases
  the views;
* ``list_state_environments`` distinguishes "the state is unreachable" (None)
  from "no environments exist" (empty set), which is what keeps the reconciler
  from recreating or invalidating everything during a warehouse outage;
* error translation of a project that cannot be loaded.
"""

from __future__ import annotations

from typing import Optional

import pytest

import src.environments.sqlmesh_binder as binder_module
from src.environments.sqlmesh_binder import (
    SqlMeshBinder,
    _close_context,
    _load_context,
    _lock_for,
)
from src.utils.errors import ContextConfigError, ContextLoadError

WS = "/data/workspaces/t1/ws-t1-abc/u1/envs/dev"


# ── fakes ───────────────────────────────────────────────────────────────


class FakePlanBuilder:
    """Records the options the binder asks for and returns a marker plan."""

    def __init__(self, context: "FakeContext", **kwargs):
        self.context = context
        self.kwargs = kwargs

    def build(self):
        self.context.built_plans.append(self.kwargs)
        return f"plan:{self.kwargs.get('environment')}"


class FakeStateSync:
    def __init__(self) -> None:
        self.invalidated: list[str] = []
        self.expired_deleted = 0

    def invalidate_environment(self, name: str) -> None:
        self.invalidated.append(name)

    def delete_expired_environments(self) -> None:
        self.expired_deleted += 1


class FakeEngineAdapter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeContext:
    """A Context the binder can plan and apply against."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.built_plans: list[dict] = []
        self.applied: list = []
        self.state_sync = FakeStateSync()
        self.engine_adapter = FakeEngineAdapter()

    def plan_builder(self, **kwargs):
        return FakePlanBuilder(self, **kwargs)

    def apply(self, plan) -> None:
        self.applied.append(plan)


class ExplodingContext(FakeContext):
    """A Context whose apply fails — the drift path."""

    def apply(self, plan) -> None:
        raise RuntimeError("warehouse unreachable")


class FakeMetadataStore:
    def __init__(self, names: Optional[list[str]] = None) -> None:
        self.names = names
        self.invalidated: list[str] = []

    async def raw_environment_names(self, workspace_path, gateway=None):
        return self.names

    def invalidate(self, workspace_path: str) -> None:
        self.invalidated.append(workspace_path)


class FakeExecutorManager:
    def __init__(self) -> None:
        self.invalidated: list[str] = []

    def invalidate(self, workspace_path: str) -> None:
        self.invalidated.append(workspace_path)


@pytest.fixture
def invalidation(monkeypatch):
    """Capture both invalidation targets the binder is expected to hit."""
    store, manager = FakeMetadataStore(), FakeExecutorManager()
    monkeypatch.setattr(
        "src.sqlmesh.metadata_context.get_metadata_context", lambda: store
    )
    monkeypatch.setattr(
        "src.executor.process.get_executor_manager", lambda: manager
    )
    return store, manager


# ── the zero-cost plan ──────────────────────────────────────────────────


class TestVirtualPlanOptions:
    def test_the_plan_points_at_the_base_environment(self):
        context = FakeContext()

        plan = SqlMeshBinder._build_virtual_plan(context, "feature-a", "prod")

        options = context.built_plans[0]
        assert options["environment"] == "feature-a"
        assert options["create_from"] == "prod"
        assert plan == "plan:feature-a"

    def test_the_plan_computes_nothing(self):
        """The whole point of a virtual environment: a pointer switch, not a
        backfill. Dropping skip_backfill turns 'new branch' into 'full rebuild'."""
        context = FakeContext()

        SqlMeshBinder._build_virtual_plan(context, "feature-a", "prod")

        options = context.built_plans[0]
        assert options["skip_backfill"] is True
        assert options["skip_tests"] is True

    def test_every_base_model_gets_a_view(self):
        """include_unmodified is what makes the new environment complete.
        Without it only the DIFFERING models get views, and the environment
        looks created while most of its models are missing."""
        context = FakeContext()

        SqlMeshBinder._build_virtual_plan(context, "feature-a", "prod")

        assert context.built_plans[0]["include_unmodified"] is True

    def test_the_plan_never_prompts(self):
        """A control-plane call has no terminal to prompt on."""
        context = FakeContext()

        SqlMeshBinder._build_virtual_plan(context, "feature-a", "prod")

        assert context.built_plans[0]["no_prompts"] is True


# ── create ──────────────────────────────────────────────────────────────


class TestEnsureVirtualEnvironment:
    async def test_success_applies_the_plan(self, monkeypatch, invalidation):
        context = FakeContext()
        monkeypatch.setattr(
            binder_module, "_load_context", lambda ws, gw: context
        )
        store, manager = invalidation

        ok = await SqlMeshBinder().ensure_virtual_environment(
            workspace_path=WS, environment="feature-a", base_env="prod"
        )

        assert ok is True
        assert context.applied == ["plan:feature-a"]

    async def test_success_closes_the_engine_adapter(self, monkeypatch, invalidation):
        """The Context is built per call, so its connection leaks unless closed."""
        context = FakeContext()
        monkeypatch.setattr(binder_module, "_load_context", lambda ws, gw: context)

        await SqlMeshBinder().ensure_virtual_environment(
            workspace_path=WS, environment="feature-a", base_env="prod"
        )

        assert context.engine_adapter.closed is True

    async def test_success_invalidates_both_caches(self, monkeypatch, invalidation):
        """A new environment changes every derived view on BOTH holders."""
        monkeypatch.setattr(
            binder_module, "_load_context", lambda ws, gw: FakeContext()
        )
        store, manager = invalidation

        await SqlMeshBinder().ensure_virtual_environment(
            workspace_path=WS, environment="feature-a", base_env="prod"
        )

        assert store.invalidated == [WS]
        assert manager.invalidated == [WS]

    async def test_a_failure_is_reported_as_drift_not_raised(
        self, monkeypatch, invalidation
    ):
        """An unreachable warehouse while the branch exists is drift, and drift
        is a registry state the reconciler repairs — not a reason to lose it."""
        monkeypatch.setattr(
            binder_module, "_load_context", lambda ws, gw: ExplodingContext()
        )

        ok = await SqlMeshBinder().ensure_virtual_environment(
            workspace_path=WS, environment="feature-a", base_env="prod"
        )

        assert ok is False

    async def test_a_failed_apply_still_closes_the_context(
        self, monkeypatch, invalidation
    ):
        """The failure path is where connections leak: the success path is the
        one people remember to test."""
        context = ExplodingContext()
        monkeypatch.setattr(binder_module, "_load_context", lambda ws, gw: context)

        await SqlMeshBinder().ensure_virtual_environment(
            workspace_path=WS, environment="feature-a", base_env="prod"
        )

        assert context.engine_adapter.closed is True

    async def test_a_context_that_cannot_be_loaded_is_reported(
        self, monkeypatch, invalidation
    ):
        def boom(ws, gw):
            raise ContextLoadError(ws, detail="no project")

        monkeypatch.setattr(binder_module, "_load_context", boom)

        ok = await SqlMeshBinder().ensure_virtual_environment(
            workspace_path=WS, environment="feature-a", base_env="prod"
        )

        assert ok is False

    async def test_writes_over_one_working_copy_serialize(self):
        """Two control-plane calls building conflicting virtual plans over one
        project is the race the lock exists for."""
        assert _lock_for(WS) is _lock_for(WS)
        assert _lock_for(WS) is not _lock_for("/data/workspaces/t1/ws-other/u1/envs/dev")


# ── invalidate ──────────────────────────────────────────────────────────


class TestInvalidateVirtualEnvironment:
    async def test_it_marks_the_state_and_releases_the_views(
        self, monkeypatch, invalidation
    ):
        """Both steps are required: invalidate_environment only marks the
        record, delete_expired_environments is what drops the views."""
        context = FakeContext()
        monkeypatch.setattr(binder_module, "_load_context", lambda ws, gw: context)

        ok = await SqlMeshBinder().invalidate_virtual_environment(
            workspace_path=WS, environment="feature-a"
        )

        assert ok is True
        assert context.state_sync.invalidated == ["feature-a"]
        assert context.state_sync.expired_deleted == 1

    async def test_it_closes_the_context(self, monkeypatch, invalidation):
        context = FakeContext()
        monkeypatch.setattr(binder_module, "_load_context", lambda ws, gw: context)

        await SqlMeshBinder().invalidate_virtual_environment(
            workspace_path=WS, environment="feature-a"
        )

        assert context.engine_adapter.closed is True

    async def test_a_failure_is_reported_not_raised(self, monkeypatch, invalidation):
        def boom(ws, gw):
            raise RuntimeError("state unreachable")

        monkeypatch.setattr(binder_module, "_load_context", boom)

        ok = await SqlMeshBinder().invalidate_virtual_environment(
            workspace_path=WS, environment="feature-a"
        )

        assert ok is False


# ── reading the state ───────────────────────────────────────────────────


class TestListStateEnvironments:
    async def test_unreachable_state_is_none_not_empty(self, monkeypatch):
        """THE distinction: treating an outage as 'nothing exists' would have
        the reconciler recreate or invalidate every environment."""
        monkeypatch.setattr(
            "src.sqlmesh.metadata_context.get_metadata_context",
            lambda: FakeMetadataStore(names=None),
        )

        names = await SqlMeshBinder().list_state_environments(workspace_path=WS)

        assert names is None

    async def test_an_empty_state_is_an_empty_set(self, monkeypatch):
        monkeypatch.setattr(
            "src.sqlmesh.metadata_context.get_metadata_context",
            lambda: FakeMetadataStore(names=[]),
        )

        names = await SqlMeshBinder().list_state_environments(workspace_path=WS)

        assert names == set()

    async def test_names_are_returned_verbatim(self, monkeypatch):
        """Raw names, no synthesis of prod: a UI may invent prod, a reconciler
        must not — a missing production environment would read as present."""
        monkeypatch.setattr(
            "src.sqlmesh.metadata_context.get_metadata_context",
            lambda: FakeMetadataStore(names=["dev", "prod"]),
        )

        names = await SqlMeshBinder().list_state_environments(workspace_path=WS)

        assert names == {"dev", "prod"}


# ── teardown and error translation ──────────────────────────────────────


class TestCloseContext:
    def test_a_failing_close_does_not_propagate(self):
        """Teardown must not raise: the operation already succeeded."""

        class Broken:
            class engine_adapter:
                @staticmethod
                def close():
                    raise RuntimeError("socket already gone")

        _close_context(Broken())

    def test_a_context_without_an_adapter_is_tolerated(self):
        _close_context(object())


class TestLoadContextErrorTranslation:
    def test_a_configuration_error_becomes_context_config_error(self, monkeypatch):
        """A broken config.yaml is the user's to fix, and must not read as an
        internal failure."""
        monkeypatch.setattr(
            binder_module, "_resolve_project", lambda ws: "/project", raising=False
        )
        monkeypatch.setattr(
            "src.sqlmesh.config_ops.reconcile_state_schema", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "src.workspace.identity.resolve_workspace_identity",
            lambda ws: _Identity(),
        )
        monkeypatch.setattr(
            "src.files.layout.sqlmesh_root", lambda ws: "/project", raising=False
        )

        import sqlmesh.core.context as sqlmesh_context
        from sqlmesh.utils.errors import ConfigError

        class Boom:
            def __init__(self, **kwargs):
                raise ConfigError("gateways: missing connection")

        monkeypatch.setattr(sqlmesh_context, "Context", Boom)

        with pytest.raises(ContextConfigError):
            _load_context(WS, None)

    def test_any_other_failure_becomes_context_load_error(self, monkeypatch):
        monkeypatch.setattr(
            "src.sqlmesh.config_ops.reconcile_state_schema", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "src.workspace.identity.resolve_workspace_identity",
            lambda ws: _Identity(),
        )
        monkeypatch.setattr(
            "src.files.layout.sqlmesh_root", lambda ws: "/project", raising=False
        )

        import sqlmesh.core.context as sqlmesh_context

        class Boom:
            def __init__(self, **kwargs):
                raise RuntimeError("duckdb file is corrupt")

        monkeypatch.setattr(sqlmesh_context, "Context", Boom)

        with pytest.raises(ContextLoadError):
            _load_context(WS, None)


class _Identity:
    workspace_id = "ws-t1-abc"
    environment = "dev"
    tenant_id = "t1"
    user_id = "u1"
