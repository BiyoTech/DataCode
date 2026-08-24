"""Tests for drift detection and repair (environments.reconciler).

The reconciler is the only component allowed to change the world on nobody's
explicit instruction, so the tests pin both what it MUST fix and what it must
refuse to touch:

* an unreachable warehouse yields no SQLMesh verdicts at all — "cannot read" is
  never treated as "nothing exists";
* an unreachable dagster-service withholds every deployment verdict the same
  way, and the served list is filtered to the reconciled workspace so another
  workspace's deployment can never be judged stale;
* branches, virtual environments and deployments the registry owns are rebuilt;
* an archived environment's leftovers are released (finishing what a partial
  destroy reported);
* ``commit_drift`` and unregistered branches are reported, never acted on;
* ``drifted`` returns to ``active`` only when nothing repairable is left.

Git, SQLMesh and the Dagster service are substituted: this is the reconciler's
decision logic.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field

import pytest

from src.environments import naming, reconciler as reconciler_module
from src.environments.reconciler import (
    DRIFT_APPLIED_DIRTY,
    DRIFT_COMMIT_DRIFT,
    DRIFT_MISSING_BRANCH,
    DRIFT_MISSING_DEPLOYMENT,
    DRIFT_MISSING_VIRTUAL_ENV,
    DRIFT_ORPHAN_BRANCH,
    DRIFT_ORPHAN_SQLMESH_ENV,
    DRIFT_STALE_DEPLOYMENT,
    DRIFT_STALE_RESOURCES,
    EnvironmentReconciler,
)


@dataclass
class _Env:
    """Minimal stand-in for an environment row."""

    name: str
    status: str = naming.STATUS_ACTIVE
    env_type: str = naming.ENV_TYPE_SANDBOX
    base_env: str = "prod"
    owner_user_id: str = "alice"
    tenant_id: str = "t1"
    workspace_id: str = "ws-t1-sales"
    last_applied_commit: str = ""
    last_applied_dirty: int = 0
    # Empty exercises the legacy-row fallback: the expected name is recomputed
    # with the same pure function creation would have frozen.
    deployment_name: str = ""
    schedules_enabled: int = 0

    @property
    def id(self) -> str:
        return naming.environment_id(self.workspace_id, self.name)

    @property
    def branch(self) -> str:
        return naming.branch_of(self.name)

    @property
    def expected_deployment(self) -> str:
        return self.deployment_name or naming.deployment_name(
            self.workspace_id, self.name
        )


@dataclass
class _Workspace:
    id: str = "ws-t1-sales"
    tenant_id: str = "t1"
    git_repo_url: str = "https://git.example.com/acme/sales.git"
    production_env: str = "prod"


@dataclass
class _FakeRepo:
    environments: t.List[_Env] = field(default_factory=list)
    status_updates: t.List[t.Tuple[str, str, str]] = field(default_factory=list)
    events: t.List[t.Dict[str, t.Any]] = field(default_factory=list)

    async def list_by_workspace(self, workspace_id, include_archived=False):
        return [
            env
            for env in self.environments
            if include_archived or env.status != naming.STATUS_ARCHIVED
        ]

    async def get_by_name(self, workspace_id, name):
        return next((env for env in self.environments if env.name == name), None)

    async def update_status(self, environment_id, status, *, expected=""):
        self.status_updates.append((environment_id, status, expected))
        return True

    async def record_event(self, environment_id, action, **kwargs):
        self.events.append({"environment_id": environment_id, "action": action, **kwargs})
        return "event-id"


@dataclass
class _PushResult:
    ok: bool = True
    reason: str = ""


@dataclass
class _Materialization:
    workspace_path: str = "/base/t1/ws-t1-sales/alice/envs/dev_alice"
    commit: str = "cafebabe1234"


class _FakeGit:
    """Git state expressed as plain data, so each test states its own world."""

    def __init__(
        self,
        *,
        members: t.Optional[t.List[str]] = None,
        repo: str = "/base/t1/ws-t1-sales/alice/.repo",
        local_branches: t.Optional[t.Set[str]] = None,
        remote_branches: t.Optional[t.Set[str]] = None,
        worktrees: t.Optional[t.Set[str]] = None,
        heads: t.Optional[t.Dict[str, str]] = None,
        push_ok: bool = True,
    ) -> None:
        self.members = members if members is not None else ["alice"]
        self.repo = repo
        self.local_branches = local_branches if local_branches is not None else set()
        self.remote_branches = remote_branches if remote_branches is not None else set()
        self.worktrees = worktrees if worktrees is not None else set()
        self.heads = heads or {}
        self.push_ok = push_ok
        self.pushed: t.List[str] = []
        self.worktrees_created: t.List[t.Tuple[str, str]] = []

    async def list_member_user_ids(self, *, tenant_id, workspace_id):
        return list(self.members)

    def first_existing_repo(self, *, tenant_id, workspace_id, members):
        return self.repo

    def first_existing_worktree(self, *, tenant_id, workspace_id, environment, members):
        return f"/base/wt/{environment}" if environment in self.worktrees else ""

    async def branch_presence(self, *, repo_path, branch):
        if not repo_path:
            return False, False
        return branch in self.local_branches, branch in self.remote_branches

    async def list_environment_branches(self, *, repo_path):
        names = set()
        for branch in self.local_branches | self.remote_branches:
            environment = naming.env_of(branch)
            if environment:
                names.add(environment)
        return names

    async def ensure_worktree(
        self, *, tenant_id, workspace_id, user_id, environment, branch, start_branch=""
    ):
        self.worktrees_created.append((environment, start_branch))
        self.local_branches.add(branch)
        self.worktrees.add(environment)
        return _Materialization()

    async def push_branch(self, *, worktree_path, branch, user_id, git_repo_url):
        if self.push_ok:
            self.pushed.append(branch)
        return _PushResult(ok=self.push_ok, reason="credential_missing")


class _FakeSqlMesh:
    """SQLMesh state as a set; ``None`` models an unreachable warehouse."""

    def __init__(
        self,
        state: t.Optional[t.Set[str]],
        *,
        ensure_ok: bool = True,
        invalidate_ok: bool = True,
    ) -> None:
        self.state = state
        self.ensure_ok = ensure_ok
        self.invalidate_ok = invalidate_ok
        self.ensured: t.List[str] = []
        self.invalidated: t.List[str] = []

    async def list_state_environments(self, *, workspace_path, gateway=None):
        return set(self.state) if self.state is not None else None

    async def ensure_virtual_environment(self, *, workspace_path, environment, base_env):
        self.ensured.append(environment)
        return self.ensure_ok

    async def invalidate_virtual_environment(self, *, workspace_path, environment):
        self.invalidated.append(environment)
        return self.invalidate_ok


@dataclass
class _Released:
    remote_branch_deleted: bool = True
    virtual_environment_released: bool = True
    worktrees_removed: int = 1
    deployment_destroyed: bool = False


class _FakeDeployment:
    """Dagster side as a set of served names; ``None`` models an outage.

    ``foreign`` names are served under ANOTHER workspace id: they must never
    produce a verdict here, which pins the workspace_id filtering.
    """

    def __init__(
        self,
        served: t.Optional[t.Set[str]],
        *,
        foreign: t.Optional[t.Set[str]] = None,
        ensure_ok: bool = True,
        destroy_ok: bool = True,
    ) -> None:
        self.served = served
        self.foreign = foreign or set()
        self.ensure_ok = ensure_ok
        self.destroy_ok = destroy_ok
        self.ensured: t.List[t.Dict[str, t.Any]] = []
        self.destroyed: t.List[str] = []

    async def list_deployments(self):
        if self.served is None:
            return None
        entries = [
            {"deployment_name": name, "workspace_id": "ws-t1-sales"}
            for name in self.served
        ]
        entries.extend(
            {"deployment_name": name, "workspace_id": "ws-other"}
            for name in self.foreign
        )
        return entries

    async def ensure_deployment(
        self,
        *,
        workspace_path,
        environment,
        env_type,
        schedules_enabled,
    ):
        self.ensured.append(
            {
                "workspace_path": workspace_path,
                "environment": environment,
                "env_type": env_type,
                "schedules_enabled": schedules_enabled,
            }
        )
        return self.ensure_ok

    async def destroy_deployment(self, *, deployment_name):
        self.destroyed.append(deployment_name)
        return self.destroy_ok


class _FakeRegistry:
    def __init__(self, released: t.Optional[_Released] = None) -> None:
        self.released = released or _Released()
        self.calls: t.List[t.Dict[str, t.Any]] = []

    async def release_archived_resources(self, **kwargs):
        self.calls.append(kwargs)
        return self.released


@pytest.fixture
def build(monkeypatch):
    """Assemble a reconciler over fake Git / SQLMesh / Dagster / registry."""

    def _build(environments, git, sqlmesh, registry=None, deployment=None):
        monkeypatch.setattr(
            reconciler_module, "_head_of", lambda path: git.heads.get(path, "")
        )
        if deployment is None:
            # Healthy default: every LIVE row's deployment is served, so the
            # new checks stay exercised by the pre-existing tests and would
            # fail loudly if they ever flagged a served deployment.
            deployment = _FakeDeployment(
                served={
                    env.expected_deployment
                    for env in environments
                    if env.status != naming.STATUS_ARCHIVED
                }
            )
        repo = _FakeRepo(environments=list(environments))
        instance = EnvironmentReconciler(
            base_path="/base",
            repository=repo,
            registry_factory=lambda: registry or _FakeRegistry(),
        )
        instance._git = git  # noqa: SLF001 - substituting the Git layer
        instance._sqlmesh = sqlmesh  # noqa: SLF001 - substituting the engine layer
        instance._deployment = deployment  # noqa: SLF001 - substituting Dagster
        return instance, repo

    return _build


def _kinds(report):
    return {drift.kind for drift in report.drifts}


async def test_unreachable_warehouse_withholds_every_sqlmesh_verdict(build):
    """A routine warehouse outage must not look like "no environments exist":
    acting on that would rebuild or invalidate the whole workspace."""
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    sqlmesh = _FakeSqlMesh(None)
    instance, _ = build([env], git, sqlmesh)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    assert report.sqlmesh_reachable is False
    assert DRIFT_MISSING_VIRTUAL_ENV not in _kinds(report)
    assert sqlmesh.ensured == []


async def test_missing_branch_is_recreated_from_its_base(build):
    env = _Env(name="dev_alice")
    base = _Env(name="prod", env_type=naming.ENV_TYPE_PRODUCTION)
    git = _FakeGit(local_branches={"main"}, remote_branches={"main"})
    sqlmesh = _FakeSqlMesh({"prod", "dev_alice"})
    instance, _ = build([env, base], git, sqlmesh)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    drift = next(d for d in report.drifts if d.kind == DRIFT_MISSING_BRANCH)
    assert drift.repaired is True
    assert git.worktrees_created == [("dev_alice", "main")]
    assert env.branch in git.pushed


async def test_local_only_branch_is_published(build):
    """A branch that never reached the remote makes the environment unopenable
    for every other member, so publishing it is the repair."""
    env = _Env(name="dev_alice")
    git = _FakeGit(local_branches={env.branch}, worktrees={env.name})
    sqlmesh = _FakeSqlMesh({"dev_alice"})
    instance, _ = build([env], git, sqlmesh)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    drift = next(d for d in report.drifts if d.kind == DRIFT_MISSING_BRANCH)
    assert drift.repaired is True
    assert git.pushed == [env.branch]


async def test_missing_virtual_environment_is_rebuilt(build):
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    sqlmesh = _FakeSqlMesh({"prod"})
    instance, _ = build([env], git, sqlmesh)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    drift = next(d for d in report.drifts if d.kind == DRIFT_MISSING_VIRTUAL_ENV)
    assert drift.repaired is True
    assert sqlmesh.ensured == ["dev_alice"]


async def test_commit_drift_is_reported_but_never_repaired(build):
    """Re-syncing data is an apply, whose backfill cost only the user may
    authorize."""
    env = _Env(name="dev_alice", last_applied_commit="aaaaaaaaaaaa")
    git = _FakeGit(
        local_branches={env.branch},
        remote_branches={env.branch},
        worktrees={env.name},
        heads={"/base/wt/dev_alice": "bbbbbbbbbbbb"},
    )
    sqlmesh = _FakeSqlMesh({"dev_alice"})
    instance, _ = build([env], git, sqlmesh)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    drift = next(d for d in report.drifts if d.kind == DRIFT_COMMIT_DRIFT)
    assert (drift.repairable, drift.repaired) == (False, False)


async def test_data_applied_from_uncommitted_code_is_reported(build):
    env = _Env(name="dev_alice", last_applied_commit="aaaaaaaaaaaa", last_applied_dirty=1)
    git = _FakeGit(
        local_branches={env.branch},
        remote_branches={env.branch},
        worktrees={env.name},
        heads={"/base/wt/dev_alice": "aaaaaaaaaaaa"},
    )
    sqlmesh = _FakeSqlMesh({"dev_alice"})
    instance, _ = build([env], git, sqlmesh)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    assert DRIFT_APPLIED_DIRTY in _kinds(report)
    # The dirty flag already says the data is untraceable; no second verdict.
    assert DRIFT_COMMIT_DRIFT not in _kinds(report)


async def test_archived_leftovers_are_released(build):
    """Finishes what a partial destroy honestly reported it could not do."""
    env = _Env(name="dev_alice", status=naming.STATUS_ARCHIVED)
    git = _FakeGit(remote_branches={env.branch})
    sqlmesh = _FakeSqlMesh({"dev_alice"})
    registry = _FakeRegistry()
    instance, _ = build([env], git, sqlmesh, registry)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    drift = next(d for d in report.drifts if d.kind == DRIFT_STALE_RESOURCES)
    assert drift.repaired is True
    assert registry.calls[0]["environment_id"] == env.id


async def test_healthy_archived_environment_reports_nothing(build):
    env = _Env(name="dev_alice", status=naming.STATUS_ARCHIVED)
    git = _FakeGit()
    sqlmesh = _FakeSqlMesh({"prod"})
    registry = _FakeRegistry()
    instance, _ = build([env], git, sqlmesh, registry)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    assert report.drifts == []
    assert registry.calls == []


async def test_drifted_returns_to_active_once_nothing_is_broken(build):
    """Completes the `drifted -> active` edge of the lifecycle state machine."""
    env = _Env(name="dev_alice", status=naming.STATUS_DRIFTED)
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    sqlmesh = _FakeSqlMesh({"prod"})  # virtual env missing, will be repaired
    instance, repo = build([env], git, sqlmesh)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    assert report.repaired_count == 1
    assert (env.id, naming.STATUS_ACTIVE, naming.STATUS_DRIFTED) in repo.status_updates


async def test_drifted_stays_drifted_while_a_repair_fails(build):
    env = _Env(name="dev_alice", status=naming.STATUS_DRIFTED)
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    sqlmesh = _FakeSqlMesh({"prod"}, ensure_ok=False)
    instance, repo = build([env], git, sqlmesh)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    assert report.repaired_count == 0
    assert repo.status_updates == []


async def test_commit_drift_alone_does_not_block_recovery(build):
    """A branch ahead of its data is normal development, not a broken
    environment."""
    env = _Env(
        name="dev_alice",
        status=naming.STATUS_DRIFTED,
        last_applied_commit="aaaaaaaaaaaa",
    )
    git = _FakeGit(
        local_branches={env.branch},
        remote_branches={env.branch},
        worktrees={env.name},
        heads={"/base/wt/dev_alice": "bbbbbbbbbbbb"},
    )
    sqlmesh = _FakeSqlMesh({"dev_alice"})
    instance, repo = build([env], git, sqlmesh)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    assert DRIFT_COMMIT_DRIFT in _kinds(report)
    assert (env.id, naming.STATUS_ACTIVE, naming.STATUS_DRIFTED) in repo.status_updates


async def test_detect_only_mode_changes_nothing(build):
    env = _Env(name="dev_alice")
    git = _FakeGit(local_branches={env.branch}, worktrees={env.name})
    sqlmesh = _FakeSqlMesh({"prod"})
    instance, repo = build([env], git, sqlmesh)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice", repair=False
    )

    assert {DRIFT_MISSING_BRANCH, DRIFT_MISSING_VIRTUAL_ENV} <= _kinds(report)
    assert all(drift.repairable and not drift.repaired for drift in report.drifts)
    assert (git.pushed, sqlmesh.ensured, repo.status_updates) == ([], [], [])


async def test_unregistered_branch_is_reported_not_adopted(build):
    """Adopting a branch would create schemas and a deployment nobody asked for,
    so the decision stays with a human (design §11.2)."""
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch, "env/from_outside", "release/1.0"},
        remote_branches={env.branch},
        worktrees={env.name},
    )
    sqlmesh = _FakeSqlMesh({"dev_alice"})
    instance, _ = build([env], git, sqlmesh)

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    orphans = [d for d in report.drifts if d.kind == DRIFT_ORPHAN_BRANCH]
    assert [d.environment for d in orphans] == ["from_outside"]
    assert orphans[0].repaired is False


async def test_orphan_virtual_environment_is_invalidated(build):
    """Derived views nobody registered are safe to drop: any registered
    environment can rebuild its own for free."""
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    sqlmesh = _FakeSqlMesh({"dev_alice", "ghost_env", "prod"})
    instance, _ = build([env], git, sqlmesh)

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    orphans = [d for d in report.drifts if d.kind == DRIFT_ORPHAN_SQLMESH_ENV]
    assert [d.environment for d in orphans] == ["ghost_env"]
    assert sqlmesh.invalidated == ["ghost_env"]


async def test_dagster_outage_withholds_every_deployment_verdict(build):
    """A dagster-service restart window must not look like "no deployment
    exists": acting on that would re-ensure the whole workspace for nothing."""
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    sqlmesh = _FakeSqlMesh({"dev_alice"})
    deployment = _FakeDeployment(None)
    instance, _ = build([env], git, sqlmesh, deployment=deployment)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    assert report.dagster_reachable is False
    assert DRIFT_MISSING_DEPLOYMENT not in _kinds(report)
    assert deployment.ensured == []


async def test_missing_deployment_is_ensured_with_the_row_policy(build):
    """The repair is a code location respawn carrying the row's frozen policy,
    so a re-ensured production-alike deployment comes back with its schedules
    instead of the safe sandbox default."""
    env = _Env(name="dev_alice", env_type=naming.ENV_TYPE_SHARED, schedules_enabled=1)
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    sqlmesh = _FakeSqlMesh({"dev_alice"})
    deployment = _FakeDeployment(set())  # reachable, nothing served
    instance, _ = build([env], git, sqlmesh, deployment=deployment)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    drift = next(d for d in report.drifts if d.kind == DRIFT_MISSING_DEPLOYMENT)
    assert drift.repaired is True
    assert deployment.ensured == [
        {
            "workspace_path": "/base/wt/dev_alice",
            "environment": "dev_alice",
            "env_type": naming.ENV_TYPE_SHARED,
            "schedules_enabled": True,
        }
    ]


async def test_missing_deployment_without_working_copy_is_reported(build):
    """Ensuring needs a working copy to load code from; with none on this
    machine the repair is an honest unrepaired report, never a guess."""
    env = _Env(name="dev_alice")
    git = _FakeGit(local_branches={env.branch}, remote_branches={env.branch})
    sqlmesh = _FakeSqlMesh({"dev_alice"})
    deployment = _FakeDeployment(set())
    instance, _ = build([env], git, sqlmesh, deployment=deployment)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    drift = next(d for d in report.drifts if d.kind == DRIFT_MISSING_DEPLOYMENT)
    assert (drift.repairable, drift.repaired) == (True, False)
    assert "no working copy" in drift.repair_detail
    assert deployment.ensured == []


async def test_archived_deployment_is_destroyed_via_release(build):
    """The deployment an archived row still holds is released by the SAME
    ``release_archived_resources`` call as every other leftover, and reported
    as its own drift kind with its own outcome slice."""
    env = _Env(name="dev_alice", status=naming.STATUS_ARCHIVED)
    git = _FakeGit()  # no git leftovers: the deployment is the only residue
    sqlmesh = _FakeSqlMesh({"prod"})
    deployment = _FakeDeployment({env.expected_deployment})
    registry = _FakeRegistry(
        _Released(
            remote_branch_deleted=False,
            virtual_environment_released=False,
            worktrees_removed=0,
            deployment_destroyed=True,
        )
    )
    instance, _ = build([env], git, sqlmesh, registry=registry, deployment=deployment)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    assert DRIFT_STALE_RESOURCES not in _kinds(report)
    drift = next(d for d in report.drifts if d.kind == DRIFT_STALE_DEPLOYMENT)
    assert drift.repaired is True
    assert registry.calls[0]["environment_id"] == env.id


async def test_deploy_only_mode_reports_deployment_drifts_without_acting(build):
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    sqlmesh = _FakeSqlMesh({"dev_alice"})
    deployment = _FakeDeployment(set())
    instance, _ = build([env], git, sqlmesh, deployment=deployment)

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice", repair=False
    )

    drift = next(d for d in report.drifts if d.kind == DRIFT_MISSING_DEPLOYMENT)
    assert (drift.repairable, drift.repaired) == (True, False)
    assert deployment.ensured == []


async def test_foreign_workspace_deployments_are_never_judged(build):
    """The served list spans every workspace; a deployment of ANOTHER
    workspace must produce no verdict here — destroying it would tear down a
    live environment this registry knows nothing about."""
    env = _Env(name="dev_alice", status=naming.STATUS_ARCHIVED)
    foreign_name = naming.deployment_name("ws-other", "prod")
    git = _FakeGit()
    sqlmesh = _FakeSqlMesh({"prod"})
    deployment = _FakeDeployment(set(), foreign={foreign_name})
    instance, _ = build([env], git, sqlmesh, deployment=deployment)

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    assert foreign_name not in {d.detail for d in report.drifts}
    assert all(foreign_name not in d.detail for d in report.drifts)
    assert deployment.destroyed == []
