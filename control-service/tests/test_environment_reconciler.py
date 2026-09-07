"""Tests for drift detection and repair (environments.reconciler).

The reconciler is the only component allowed to change the world on nobody's
explicit instruction, so the tests pin both what it MUST fix and what it must
refuse to touch:

* an unreachable warehouse yields no SQLMesh verdicts at all — "cannot read" is
  never treated as "nothing exists";
* an unreachable runtime withholds every deployment verdict the same way, and
  the served list is filtered to the reconciled workspace so another
  workspace's deployment can never be judged stale;
* branches, virtual environments and deployments the registry owns are rebuilt;
* an archived environment's leftovers are released (finishing what a partial
  destroy reported);
* ``commit_drift`` and unregistered branches are reported, never acted on;
* ``drifted`` returns to ``active`` only when nothing repairable is left.

Git, SQLMesh and the Dagster data plane are substituted: this is the
reconciler's decision logic. (P5: the substitutes mirror the HTTP binder
contract — asynchronous first-existing probes included.)
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from datacode_common import naming
from datacode_common.lease import LeaseRecord, RuntimeHeartbeat

from src.environments import reconciler as reconciler_module
from src.environments.lease_binder import LeaseWorld
from src.environments.reconciler import (
    DRIFT_APPLIED_DIRTY,
    DRIFT_COMMIT_DRIFT,
    DRIFT_LEASE_SPLIT_BRAIN,
    DRIFT_MISSING_BRANCH,
    DRIFT_MISSING_DEPLOYMENT,
    DRIFT_MISSING_VIRTUAL_ENV,
    DRIFT_ORPHAN_BRANCH,
    DRIFT_ORPHAN_CODELOC_POD,
    DRIFT_ORPHAN_LEASE,
    DRIFT_ORPHAN_SQLMESH_ENV,
    DRIFT_STALE_DEPLOYMENT,
    DRIFT_STALE_LEASE,
    DRIFT_STALE_RESOURCES,
    DRIFT_ZOMBIE_TASK,
    SEVERITY_CRITICAL,
    EnvironmentReconciler,
)
from src.environments.sandbox_binder import SandboxWorld
from src.records.repository import UnfinishedTask


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
    """Git state expressed as plain data, so each test states its own world.

    Mirrors the HTTP GitBinder contract (P5): the first-existing probes are
    asynchronous round trips and the HEAD lookup is addressed logically.
    """

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
        # environment name -> HEAD commit
        self.heads = heads or {}
        self.push_ok = push_ok
        self.pushed: t.List[str] = []
        self.worktrees_created: t.List[t.Tuple[str, str]] = []

    async def list_member_user_ids(self, *, tenant_id, workspace_id):
        return list(self.members)

    async def first_existing_repo(self, *, tenant_id, workspace_id, members=()):
        return self.repo

    async def first_existing_worktree(
        self, *, tenant_id, workspace_id, environment, members=()
    ):
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

    async def worktree_head(self, *, tenant_id, workspace_id, environment):
        return self.heads.get(environment, "")

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


class _FakeLeases:
    """The lease party as plain data; ``None`` models unreadable Redis.

    Records every release so a test can assert not just the drift kind but that
    the repair was (or was not) actually attempted — the distinction between
    "detected" and "acted on" is the whole point of the repair/report split.
    """

    def __init__(self, world: t.Optional[LeaseWorld]) -> None:
        self.world = world
        self.released: t.List[str] = []
        self.release_ok = True

    async def read_world(self):
        return self.world

    async def release_stale(self, record):
        self.released.append(record.lease_key)
        return self.release_ok


class _FakeSandboxes:
    """The sandbox party as plain data; ``None`` models an unreachable runtime."""

    def __init__(self, world: t.Optional[SandboxWorld]) -> None:
        self.world = world
        self.deleted: t.List[t.Tuple[str, str]] = []
        self.delete_ok = True

    async def read_world(self):
        return self.world

    async def delete_code_location_pod(self, *, pod_name, environment):
        self.deleted.append((pod_name, environment))
        return self.delete_ok


class _FakeTaskRecords:
    """``task_records`` as a list of unfinished rows."""

    def __init__(self, unfinished: t.Optional[t.List[UnfinishedTask]] = None) -> None:
        self.unfinished = unfinished or []
        self.failed: t.List[t.Tuple[str, str]] = []
        self.mark_ok = True

    async def list_unfinished(self, *, workspace_id="", older_than_seconds=0, now=None):
        # The grace filter belongs to the repository (and is tested there); the
        # reconciler's tests state directly which rows are old enough to judge.
        return [
            task
            for task in self.unfinished
            if not workspace_id or task.workspace_id == workspace_id
        ]

    async def mark_failed(self, task_id, reason):
        self.failed.append((task_id, reason))
        return self.mark_ok


def _lease_record(lease_key: str, runtime_id: str, token: str = "tok") -> LeaseRecord:
    return LeaseRecord(
        lease_key=lease_key,
        token=token,
        runtime_id=runtime_id,
        runtime_addr=f"{runtime_id}:8002",
        acquired_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )


def _heartbeat(runtime_id: str, *leases: str) -> RuntimeHeartbeat:
    return RuntimeHeartbeat(
        runtime_id=runtime_id,
        addr=f"{runtime_id}:8002",
        lease_count=len(leases),
        max_leases=10,
        memory_used_ratio=0.2,
        reported_at=datetime.now(timezone.utc),
        leases=tuple(leases),
    )


def _lease_world(
    records: t.Sequence[LeaseRecord],
    heartbeats: t.Sequence[RuntimeHeartbeat] = (),
    *,
    truncated: bool = False,
) -> LeaseWorld:
    return LeaseWorld(
        leases={record.lease_key: record for record in records},
        heartbeats={hb.runtime_id: hb for hb in heartbeats},
        truncated=truncated,
    )


def _unfinished(task_id: str, *, workspace_id="ws-t1-sales", environment="dev_alice"):
    return UnfinishedTask(
        task_id=task_id,
        kind="sqlmesh.apply",
        tenant_id="t1",
        workspace_id=workspace_id,
        environment=environment,
        runtime_id="workspace-runtime-0",
        status="running",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )


@pytest.fixture
def build():
    """Assemble a reconciler over fake Git / SQLMesh / Dagster / registry.

    The lease and sandbox parties default to "nothing to see" rather than to the
    real binders: a test that did not state a lease world would otherwise reach
    for Redis and the runtime, and its outcome would depend on whether the
    developer happened to have both running.
    """

    def _build(
        environments,
        git,
        sqlmesh,
        registry=None,
        deployment=None,
        leases=None,
        sandboxes=None,
        task_records=None,
    ):
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
            lease_binder=leases if leases is not None else _FakeLeases(_lease_world([])),
            sandbox_binder=(
                sandboxes
                if sandboxes is not None
                else _FakeSandboxes(
                    SandboxWorld(
                        available=True, code_location_pods=[], task_pods=[]
                    )
                )
            ),
            task_records=task_records or _FakeTaskRecords(),
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
        heads={"dev_alice": "bbbbbbbbbbbb"},
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
        heads={"dev_alice": "aaaaaaaaaaaa"},
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
        heads={"dev_alice": "bbbbbbbbbbbb"},
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
    """A runtime restart window must not look like "no deployment exists":
    acting on that would re-ensure the whole workspace for nothing."""
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
    """Ensuring needs a working copy to load code from; with none on any
    replica the repair is an honest unrepaired report, never a guess."""
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


# ---------------------------------------------------------------------------
# Lease party (design 05 §5.1)
# ---------------------------------------------------------------------------


async def test_stale_lease_is_released_when_its_holder_stopped_heartbeating(build):
    """A dead holder's claim blocks the working copy for a full TTL.

    The request path only cleans this up when someone asks for that exact
    workspace, so a lease nobody requests again is never noticed at all — which
    is precisely why the periodic pass has to detect it.
    """
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    lease_key = "t1/ws-t1-sales/alice/dev_alice"
    leases = _FakeLeases(
        _lease_world(
            [_lease_record(lease_key, "workspace-runtime-3")],
            # Only runtime-0 is alive; the claimant has no heartbeat.
            [_heartbeat("workspace-runtime-0")],
        )
    )
    instance, _ = build([env], git, _FakeSqlMesh({"dev_alice"}), leases=leases)

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    drift = next(d for d in report.drifts if d.kind == DRIFT_STALE_LEASE)
    assert (drift.repairable, drift.repaired) == (True, True)
    assert leases.released == [lease_key]
    assert report.lease_readable is True


async def test_lease_of_a_live_environment_with_a_live_holder_is_not_drift(build):
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    lease_key = "t1/ws-t1-sales/alice/dev_alice"
    leases = _FakeLeases(
        _lease_world(
            [_lease_record(lease_key, "workspace-runtime-0")],
            [_heartbeat("workspace-runtime-0", lease_key)],
        )
    )
    instance, _ = build([env], git, _FakeSqlMesh({"dev_alice"}), leases=leases)

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    assert not _kinds(report) & {DRIFT_STALE_LEASE, DRIFT_ORPHAN_LEASE}
    assert leases.released == []


async def test_orphan_lease_is_released_when_its_environment_is_archived(build):
    """Nothing will ever release a lease whose environment is gone."""
    env = _Env(name="dev_alice", status=naming.STATUS_ARCHIVED)
    git = _FakeGit()
    lease_key = "t1/ws-t1-sales/alice/dev_alice"
    leases = _FakeLeases(
        _lease_world(
            [_lease_record(lease_key, "workspace-runtime-0")],
            # The holder is ALIVE: this is an orphan, not a stale lease, and the
            # distinction matters because only the registry can tell them apart.
            [_heartbeat("workspace-runtime-0", lease_key)],
        )
    )
    instance, _ = build([env], git, _FakeSqlMesh({"prod"}), leases=leases)

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    assert DRIFT_ORPHAN_LEASE in _kinds(report)
    assert DRIFT_STALE_LEASE not in _kinds(report)
    assert leases.released == [lease_key]


async def test_split_brain_is_reported_critical_and_never_repaired(build):
    """Two claimants means "the holder is gone" is false, so deleting is wrong.

    The design assigns arbitration to the working copy's file lock; a periodic
    loop reading a stale snapshot must not pick a winner. It raises the alarm
    (Page-level severity) and leaves the claim alone.
    """
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    lease_key = "t1/ws-t1-sales/alice/dev_alice"
    leases = _FakeLeases(
        _lease_world(
            [_lease_record(lease_key, "workspace-runtime-0")],
            [
                _heartbeat("workspace-runtime-0", lease_key),
                # A second replica also believes it holds the same working copy.
                _heartbeat("workspace-runtime-1", lease_key),
            ],
        )
    )
    instance, _ = build([env], git, _FakeSqlMesh({"dev_alice"}), leases=leases)

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    drift = next(d for d in report.drifts if d.kind == DRIFT_LEASE_SPLIT_BRAIN)
    assert drift.severity == SEVERITY_CRITICAL
    assert (drift.repairable, drift.repaired) == (False, False)
    assert leases.released == []
    assert report.critical_count == 1


async def test_split_brain_suppresses_the_staleness_verdict(build):
    """The dangerous shape of a split brain: the RECORD's holder looks dead.

    Redis names runtime-3 as the owner, but runtime-3's heartbeat is gone while
    runtime-1 is alive and also claims the key. Evaluating staleness here would
    delete the lease — and runtime-1, which is demonstrably alive and mounted,
    would keep writing to a working copy the platform now considers free to
    hand out. Hence split brain is checked first and stops the pass for that key.
    """
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    lease_key = "t1/ws-t1-sales/alice/dev_alice"
    leases = _FakeLeases(
        _lease_world(
            [_lease_record(lease_key, "workspace-runtime-3")],
            # The record's holder has no heartbeat; a DIFFERENT live replica
            # claims the same working copy.
            [_heartbeat("workspace-runtime-1", lease_key)],
        )
    )
    instance, _ = build([env], git, _FakeSqlMesh({"dev_alice"}), leases=leases)

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    assert DRIFT_LEASE_SPLIT_BRAIN in _kinds(report)
    assert DRIFT_STALE_LEASE not in _kinds(report)
    # The decisive assertion: nothing was deleted.
    assert leases.released == []


async def test_another_workspaces_lease_is_never_touched(build):
    """Leases are enumerated cell-wide; scoping is what keeps passes isolated.

    Without the workspace filter, every workspace's pass would find every other
    workspace's leases, conclude that no local row accounts for them, and delete
    the whole cell's leases on the first run.
    """
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    foreign_key = "t1/ws-other/bob/dev_bob"
    leases = _FakeLeases(
        _lease_world([_lease_record(foreign_key, "workspace-runtime-9")], [])
    )
    instance, _ = build([env], git, _FakeSqlMesh({"dev_alice"}), leases=leases)

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    assert not _kinds(report) & {DRIFT_STALE_LEASE, DRIFT_ORPHAN_LEASE}
    assert leases.released == []


async def test_unreadable_lease_party_withholds_every_lease_verdict(build):
    """A Redis blip must not turn every lease into an orphan."""
    env = _Env(name="dev_alice", status=naming.STATUS_ARCHIVED)
    git = _FakeGit()
    leases = _FakeLeases(None)
    instance, _ = build([env], git, _FakeSqlMesh({"prod"}), leases=leases)

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    assert report.lease_readable is False
    assert not _kinds(report) & {
        DRIFT_STALE_LEASE,
        DRIFT_ORPHAN_LEASE,
        DRIFT_LEASE_SPLIT_BRAIN,
    }
    assert leases.released == []


async def test_truncated_lease_scan_reports_but_repairs_nothing(build):
    """A pass that could not read the whole keyspace may not conclude "no owner"."""
    env = _Env(name="dev_alice", status=naming.STATUS_ARCHIVED)
    git = _FakeGit()
    lease_key = "t1/ws-t1-sales/alice/dev_alice"
    leases = _FakeLeases(
        _lease_world([_lease_record(lease_key, "workspace-runtime-0")], [], truncated=True)
    )
    instance, _ = build([env], git, _FakeSqlMesh({"prod"}), leases=leases)

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    drift = next(d for d in report.drifts if d.kind == DRIFT_ORPHAN_LEASE)
    assert (drift.repairable, drift.repaired) == (True, False)
    assert leases.released == []


# ---------------------------------------------------------------------------
# Sandbox party (design 05 §5.1)
# ---------------------------------------------------------------------------


async def test_orphan_code_location_pod_is_deleted(build):
    """An orphaned code location keeps running user code and holds a mount."""
    env = _Env(name="dev_alice", status=naming.STATUS_ARCHIVED)
    git = _FakeGit()
    sandboxes = _FakeSandboxes(
        SandboxWorld(
            available=True,
            code_location_pods=[
                {
                    "pod_name": "codeloc-dev-alice-abc12345",
                    "environment": "dev_alice",
                    "location_name": "ws-t1-sales__dev_alice",
                    "phase": "running",
                }
            ],
            task_pods=[],
        )
    )
    instance, _ = build([env], git, _FakeSqlMesh({"prod"}), sandboxes=sandboxes)

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    drift = next(d for d in report.drifts if d.kind == DRIFT_ORPHAN_CODELOC_POD)
    assert (drift.repairable, drift.repaired) == (True, True)
    assert sandboxes.deleted == [("codeloc-dev-alice-abc12345", "dev_alice")]


async def test_pod_of_a_live_environment_is_left_alone(build):
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    sandboxes = _FakeSandboxes(
        SandboxWorld(
            available=True,
            code_location_pods=[
                {
                    "pod_name": "codeloc-dev-alice-abc12345",
                    "environment": "dev_alice",
                    "location_name": "ws-t1-sales__dev_alice",
                    "phase": "running",
                }
            ],
            task_pods=[],
        )
    )
    instance, _ = build([env], git, _FakeSqlMesh({"dev_alice"}), sandboxes=sandboxes)

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    assert DRIFT_ORPHAN_CODELOC_POD not in _kinds(report)
    assert sandboxes.deleted == []


async def test_pod_of_an_unknown_environment_is_reported_never_deleted(build):
    """A Pod this workspace does not own belongs to another workspace's pass.

    The namespace is cell-wide, so acting on an unrecognized environment here
    would delete a live code location whose registry row lives elsewhere.
    """
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    sandboxes = _FakeSandboxes(
        SandboxWorld(
            available=True,
            code_location_pods=[
                {
                    "pod_name": "codeloc-dev-bob-99887766",
                    "environment": "dev_bob",
                    "location_name": "ws-other__dev_bob",
                    "phase": "running",
                }
            ],
            task_pods=[],
        )
    )
    instance, _ = build([env], git, _FakeSqlMesh({"dev_alice"}), sandboxes=sandboxes)

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    assert DRIFT_ORPHAN_CODELOC_POD not in _kinds(report)
    assert sandboxes.deleted == []


async def test_zombie_task_is_settled_as_failed(build):
    """A task with no Pod would otherwise be "running" forever."""
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    tasks = _FakeTaskRecords([_unfinished("task-abc")])
    sandboxes = _FakeSandboxes(
        SandboxWorld(available=True, code_location_pods=[], task_pods=[])
    )
    instance, _ = build(
        [env],
        git,
        _FakeSqlMesh({"dev_alice"}),
        sandboxes=sandboxes,
        task_records=tasks,
    )

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    drift = next(d for d in report.drifts if d.kind == DRIFT_ZOMBIE_TASK)
    assert (drift.repairable, drift.repaired) == (True, True)
    assert [task_id for task_id, _ in tasks.failed] == ["task-abc"]


async def test_task_with_a_pending_pod_is_not_a_zombie(build):
    """A Pod waiting to be scheduled is a healthy young task, not a lost one."""
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    tasks = _FakeTaskRecords([_unfinished("task-abc")])
    sandboxes = _FakeSandboxes(
        SandboxWorld(
            available=True,
            code_location_pods=[],
            task_pods=[
                {
                    "pod_name": "task-task-abc",
                    "task_id": "task-abc",
                    "tenant_id": "t1",
                    "phase": "pending",
                }
            ],
        )
    )
    instance, _ = build(
        [env],
        git,
        _FakeSqlMesh({"dev_alice"}),
        sandboxes=sandboxes,
        task_records=tasks,
    )

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    assert DRIFT_ZOMBIE_TASK not in _kinds(report)
    assert tasks.failed == []


async def test_unreadable_task_pods_withhold_every_zombie_verdict(build):
    """Half the sandbox party can be unreadable on its own."""
    env = _Env(name="dev_alice")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    tasks = _FakeTaskRecords([_unfinished("task-abc")])
    sandboxes = _FakeSandboxes(
        SandboxWorld(available=True, code_location_pods=[], task_pods=None)
    )
    instance, _ = build(
        [env],
        git,
        _FakeSqlMesh({"dev_alice"}),
        sandboxes=sandboxes,
        task_records=tasks,
    )

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    assert DRIFT_ZOMBIE_TASK not in _kinds(report)
    assert tasks.failed == []


async def test_deployment_without_a_container_plane_has_no_sandbox_party(build):
    """dev / compose: no cluster at all is benign, not an outage."""
    env = _Env(name="dev_alice", status=naming.STATUS_ARCHIVED)
    git = _FakeGit()
    tasks = _FakeTaskRecords([_unfinished("task-abc")])
    sandboxes = _FakeSandboxes(
        SandboxWorld(available=False, code_location_pods=None, task_pods=None)
    )
    instance, _ = build(
        [env], git, _FakeSqlMesh({"prod"}), sandboxes=sandboxes, task_records=tasks
    )

    report = await instance.reconcile_workspace(
        workspace=_Workspace(), actor_user_id="alice"
    )

    assert report.sandbox_readable is None
    assert not _kinds(report) & {DRIFT_ORPHAN_CODELOC_POD, DRIFT_ZOMBIE_TASK}
    assert tasks.failed == []


async def test_environment_scope_draws_no_lease_or_sandbox_verdict(build):
    """Both parties need the whole workspace's rows to judge ownership.

    A single-row pass that consulted them would find every sibling
    environment's lease unaccounted for and delete it.
    """
    env = _Env(name="dev_alice")
    other = _Env(name="dev_bob")
    git = _FakeGit(
        local_branches={env.branch}, remote_branches={env.branch}, worktrees={env.name}
    )
    leases = _FakeLeases(
        _lease_world([_lease_record("t1/ws-t1-sales/bob/dev_bob", "workspace-runtime-9")])
    )
    instance, _ = build(
        [env, other], git, _FakeSqlMesh({"dev_alice", "dev_bob"}), leases=leases
    )

    report = await instance.reconcile_environment(
        model=env, workspace=_Workspace(), actor_user_id="alice"
    )

    assert report.lease_readable is None
    assert report.sandbox_readable is None
    assert leases.released == []


# Silence the unused-import warning for the module reference used above.
_ = reconciler_module
