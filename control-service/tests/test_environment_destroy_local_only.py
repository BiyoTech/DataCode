"""Tests for ``EnvironmentRegistry.destroy`` — the local-only release.

Destroying an environment releases what the PLATFORM created and nothing else.
The remote branch belongs to the user: it may predate the workspace, hold the
only durable copy of the code, and it is what lets a same-named environment
resume the same line of development later. Three things are therefore pinned:

* no code path can even ask for a remote branch deletion (structural, so a
  future "cleanup" helper cannot quietly re-introduce it);
* unpushed commits are REPORTED, never refused — the user is warned before the
  confirmation dialog and stays in charge of the decision;
* uncommitted changes keep refusing exactly as before, because the TTL reaper's
  ``force=True`` is the only thing that may override that.

A fourth contract is that a destroy can be RE-ISSUED on a row stranded in
``archiving``. That status is written before the release starts and cleared when
it ends, and nothing else advances it — the reconciler inspects only live and
archived rows, the TTL reaper's query selects neither, and a same-named create
refuses it. Repeating the delete is therefore the only way out of it, and the
UI relies on that: it is what the "finish release" button calls.

The binders are substituted: this is the orchestration's decision logic, not an
integration test of git or the warehouse.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field

import pytest
from datacode_common import naming

from src.environments.git_binder import (
    BRANCH_SOURCE_CREATED_FROM_BASE,
    BRANCH_SOURCE_REUSED_REMOTE,
    PendingWork,
)
from src.environments.registry import EnvironmentRegistry
from src.utils.errors import ConflictError

_TENANT = "t1"
_WORKSPACE = "ws-t1-sales"
_ENV = "dev_alice"
_ENV_ID = f"{_WORKSPACE}:{_ENV}"


@dataclass
class _EnvRow:
    """Only the columns ``destroy`` reads."""

    id: str = _ENV_ID
    tenant_id: str = _TENANT
    workspace_id: str = _WORKSPACE
    name: str = _ENV
    branch: str = f"env/{_ENV}"
    env_type: str = naming.ENV_TYPE_SANDBOX
    base_env: str = "prod"
    owner_user_id: str = "alice"
    status: str = naming.STATUS_ACTIVE
    deployment_name: str = f"{_WORKSPACE}__{_ENV}"


class _FakeRepo:
    def __init__(self, row: _EnvRow) -> None:
        self.row = row
        self.status_updates: t.List[str] = []
        self.events: t.List[t.Dict[str, t.Any]] = []

    async def get(self, environment_id, tenant_id=None):
        return self.row if environment_id == self.row.id else None

    async def update_status(self, environment_id, status, expected=None):
        self.status_updates.append(status)
        self.row.status = status
        return True

    async def record_event(self, environment_id, action, **kwargs):
        self.events.append({"action": action, **kwargs})
        return "event-id"


class _FakeGit:
    """Git binder double that records every probe and release it is asked for.

    Deliberately exposes NO remote-deletion method: if the registry ever tries
    one, the test fails with an AttributeError naming the call.
    """

    def __init__(
        self,
        *,
        members: t.Optional[t.List[str]] = None,
        pending: t.Optional[t.Dict[str, PendingWork]] = None,
    ) -> None:
        self.members = members if members is not None else ["alice"]
        self.pending = pending or {}
        self.removed_worktrees: t.List[str] = []
        self.deleted_local_branches: t.List[str] = []
        self.pending_probes: t.List[t.Tuple[str, str]] = []

    async def list_member_user_ids(self, *, tenant_id, workspace_id):
        return list(self.members)

    async def pending_work(
        self, *, tenant_id, workspace_id, user_id, environment, branch
    ):
        self.pending_probes.append((user_id, branch))
        return self.pending.get(user_id, PendingWork(uncommitted=0, unpushed=0))

    async def remove_worktree(self, *, tenant_id, workspace_id, user_id, environment):
        self.removed_worktrees.append(user_id)
        return True

    async def delete_local_branch(self, *, tenant_id, workspace_id, user_id, branch):
        self.deleted_local_branches.append(user_id)
        return True


class _FakeSqlMesh:
    def __init__(self) -> None:
        self.invalidated: t.List[str] = []
        self.addressed_by: t.List[t.Tuple[str, str]] = []

    async def invalidate_virtual_environment(
        self, *, tenant_id, workspace_id, environment
    ):
        self.addressed_by.append((tenant_id, workspace_id))
        self.invalidated.append(environment)
        return True


class _FakeDeployment:
    def __init__(self) -> None:
        self.destroyed: t.List[str] = []

    async def destroy_deployment(self, *, deployment_name):
        self.destroyed.append(deployment_name)
        return True


@dataclass
class _Fakes:
    registry: EnvironmentRegistry
    repo: _FakeRepo
    git: _FakeGit
    sqlmesh: _FakeSqlMesh
    deployment: _FakeDeployment
    locks: t.List[str] = field(default_factory=list)


@pytest.fixture()
def fakes(monkeypatch) -> _Fakes:
    """A registry whose every collaborator is observable and in-memory."""
    import contextlib

    from src.environments import registry as registry_module

    taken: t.List[str] = []

    @contextlib.asynccontextmanager
    async def _lock(workspace_id, environment, *, operation):
        taken.append(operation)
        yield

    monkeypatch.setattr(registry_module, "environment_lock_by_key", _lock)

    instance = EnvironmentRegistry.__new__(EnvironmentRegistry)
    repo = _FakeRepo(_EnvRow())
    git = _FakeGit()
    sqlmesh = _FakeSqlMesh()
    deployment = _FakeDeployment()
    instance._repo = repo  # type: ignore[attr-defined]
    instance._git = git  # type: ignore[attr-defined]
    instance._sqlmesh = sqlmesh  # type: ignore[attr-defined]
    instance._deployment = deployment  # type: ignore[attr-defined]
    instance._workspaces = None  # type: ignore[attr-defined]
    return _Fakes(instance, repo, git, sqlmesh, deployment, taken)


async def _destroy(fakes: _Fakes, **kwargs):
    return await fakes.registry.destroy(
        environment_id=_ENV_ID,
        tenant_id=_TENANT,
        actor_user_id="alice",
        **kwargs,
    )


# ── The remote branch survives ──────────────────────────────────────────────


async def test_destroy_releases_local_state_only(fakes):
    """Views, working copies, local branch and deployment go; the remote stays."""
    result = await _destroy(fakes)

    assert result.archived is True
    assert fakes.sqlmesh.invalidated == [_ENV]
    assert fakes.git.removed_worktrees == ["alice"]
    assert fakes.git.deleted_local_branches == ["alice"]
    assert fakes.deployment.destroyed == [f"{_WORKSPACE}__{_ENV}"]
    assert fakes.repo.status_updates == [
        naming.STATUS_ARCHIVING,
        naming.STATUS_ARCHIVED,
    ]


async def test_destroy_result_reports_no_remote_outcome(fakes):
    """Reporting a remote-deletion outcome would describe an operation that no
    longer exists, so the field is gone rather than hard-coded to False."""
    result = await _destroy(fakes)

    assert not hasattr(result, "remote_branch_deleted")


async def test_destroy_audits_the_preserved_branch(fakes):
    """The audit trail must state the branch survived, not leave it inferred
    from the absence of a deletion record."""
    await _destroy(fakes)

    destroyed = [e for e in fakes.repo.events if e["action"] == naming.ACTION_DESTROY]
    assert destroyed[0]["detail"]["remote_branch_preserved"] == f"env/{_ENV}"


async def test_destroy_result_names_what_survived_and_what_was_lost(fakes):
    """The API layer reports both to the client, so the outcome can be
    reconciled with the warning the user saw before confirming."""
    fakes.git.pending = {"alice": PendingWork(uncommitted=0, unpushed=2)}

    result = await _destroy(fakes)

    assert result.preserved_branch == f"env/{_ENV}"
    assert result.unpushed_commits_discarded == 2


async def test_destroy_signature_takes_no_remote_deletion_flag(fakes):
    """Structural guard: a default-off switch would still be an entry point a
    later caller could flip, so the parameter must not exist at all."""
    import inspect

    params = inspect.signature(fakes.registry.destroy).parameters
    assert "delete_remote_branch" not in params


# ── Unpushed commits: warned, never refused ─────────────────────────────────


async def test_unpushed_commits_do_not_block_the_destroy(fakes):
    """The user was told before confirming; refusing here would turn a hint
    back into the block this contract removed."""
    fakes.git.pending = {"alice": PendingWork(uncommitted=0, unpushed=4)}

    result = await _destroy(fakes)

    assert result.archived is True
    assert fakes.repo.status_updates[-1] == naming.STATUS_ARCHIVED


async def test_discarded_unpushed_commits_are_audited(fakes):
    """The only code a destroy can really lose, so it is always recorded."""
    fakes.git.pending = {
        "alice": PendingWork(uncommitted=0, unpushed=4),
        "bob": PendingWork(uncommitted=0, unpushed=1),
    }
    fakes.git.members = ["alice", "bob"]

    await _destroy(fakes)

    destroyed = [e for e in fakes.repo.events if e["action"] == naming.ACTION_DESTROY]
    assert destroyed[0]["detail"]["unpushed_commits_discarded"] == 5


async def test_pending_work_is_probed_with_the_branch(fakes):
    """The tracking ref the unpushed count is measured against is derived from
    the branch, so omitting it would silently report zero."""
    await _destroy(fakes)

    assert fakes.git.pending_probes == [("alice", f"env/{_ENV}")]


# ── Uncommitted changes: unchanged behaviour ────────────────────────────────


async def test_uncommitted_changes_still_refuse_the_destroy(fakes):
    fakes.git.pending = {"alice": PendingWork(uncommitted=3, unpushed=0)}

    with pytest.raises(ConflictError, match="uncommitted changes"):
        await _destroy(fakes)

    # Refused BEFORE anything was released: a rejected destroy must leave the
    # environment exactly as it was.
    assert fakes.repo.status_updates == []
    assert fakes.git.removed_worktrees == []


async def test_force_discards_uncommitted_changes(fakes):
    """The TTL reaper depends on this: an abandoned sandbox is precisely the
    case where nobody will come to resolve its changes."""
    fakes.git.pending = {"alice": PendingWork(uncommitted=3, unpushed=2)}

    result = await _destroy(fakes, force=True)

    assert result.archived is True
    destroyed = [e for e in fakes.repo.events if e["action"] == naming.ACTION_DESTROY]
    assert destroyed[0]["detail"]["forced"] is True
    # Forced or not, the loss is recorded.
    assert destroyed[0]["detail"]["unpushed_commits_discarded"] == 2


async def test_every_member_is_probed_not_just_the_actor(fakes):
    """Another member's uncommitted work is exactly as unrecoverable as the
    caller's own, because destroy removes their worktree too."""
    fakes.git.members = ["alice", "bob"]
    fakes.git.pending = {"bob": PendingWork(uncommitted=2, unpushed=0)}

    with pytest.raises(ConflictError, match="bob"):
        await _destroy(fakes)


# ── Read-only pre-flight ────────────────────────────────────────────────────


async def test_pending_work_report_summarizes_every_member(fakes):
    fakes.git.members = ["alice", "bob"]
    fakes.git.pending = {
        "alice": PendingWork(uncommitted=1, unpushed=2),
        "bob": PendingWork(uncommitted=0, unpushed=3),
    }

    report = await fakes.registry.pending_work(
        environment_id=_ENV_ID, tenant_id=_TENANT, actor_user_id="alice"
    )

    assert (report.uncommitted, report.unpushed) == (1, 5)
    assert [m.user_id for m in report.members] == ["alice", "bob"]
    assert report.branch == f"env/{_ENV}"


async def test_pending_work_takes_no_environment_lock(fakes):
    """It runs while the delete dialog is open, so queueing it behind an apply
    would stall the UI on an operation that changes nothing."""
    await fakes.registry.pending_work(
        environment_id=_ENV_ID, tenant_id=_TENANT, actor_user_id="alice"
    )

    assert fakes.locks == []


async def test_pending_work_omits_members_with_nothing_to_lose(fakes):
    """Most members never materialized the environment; listing them as zeros
    would bury the one member who does have work."""
    fakes.git.members = ["alice", "bob"]
    fakes.git.pending = {"bob": PendingWork(uncommitted=0, unpushed=1)}

    report = await fakes.registry.pending_work(
        environment_id=_ENV_ID, tenant_id=_TENANT, actor_user_id="alice"
    )

    assert [m.user_id for m in report.members] == ["bob"]


async def test_destroy_never_strands_the_row_in_archiving(fakes):
    """Regression for the "stuck in archiving forever" report.

    ``archiving`` has no self-healing path: the reconciler does not inspect it,
    the reaper's query does not return it, and a same-named create refuses it.
    So the release step must not raise on a defect of its own making — it used
    to, by synthesizing an invalid working-copy path to address the runtime.
    """
    await _destroy(fakes)

    assert fakes.repo.row.status == naming.STATUS_ARCHIVED
    # Addressed by ids, never by a fabricated path.
    assert fakes.sqlmesh.addressed_by == [(_TENANT, _WORKSPACE)]


# ── Re-entering a stranded release ──────────────────────────────────────────


async def test_archived_rows_short_circuit_before_any_release(fakes):
    """The early return is keyed on `archived` ALONE — the one status for which
    a repeat release has nothing left to do. Widening it to `archiving` is
    exactly the regression the tests below exist for."""
    fakes.repo.row.status = naming.STATUS_ARCHIVED

    result = await _destroy(fakes)

    assert result.archived is False
    assert fakes.git.removed_worktrees == []
    assert fakes.deployment.destroyed == []
    assert fakes.locks == []


async def test_a_row_stranded_in_archiving_is_released_not_refused(fakes):
    """A process that stops between the two status writes leaves the row in a
    status NOTHING else advances, so re-issuing the delete must reach the
    release. Answering "already being released" here is what strands it
    permanently."""
    fakes.repo.row.status = naming.STATUS_ARCHIVING

    result = await _destroy(fakes)

    assert result.archived is True
    assert fakes.repo.row.status == naming.STATUS_ARCHIVED
    assert fakes.sqlmesh.invalidated == [_ENV]
    assert fakes.git.removed_worktrees == ["alice"]
    assert fakes.git.deleted_local_branches == ["alice"]
    assert fakes.deployment.destroyed == [f"{_WORKSPACE}__{_ENV}"]


async def test_the_retry_replays_the_whole_status_sequence(fakes):
    """`archiving` is re-written although the row already carries it: that write
    is what marks THIS attempt as under way, and the terminal `archived` is what
    ends it. Skipping it would leave no transition to observe."""
    fakes.repo.row.status = naming.STATUS_ARCHIVING

    await _destroy(fakes)

    assert fakes.repo.status_updates == [
        naming.STATUS_ARCHIVING,
        naming.STATUS_ARCHIVED,
    ]


async def test_the_retry_releases_every_member_not_just_the_actor(fakes):
    """A stranded release stopped after releasing an arbitrary PREFIX of the
    members, so the retry may not assume only the actor's copy is left."""
    fakes.repo.row.status = naming.STATUS_ARCHIVING
    fakes.git.members = ["alice", "bob"]

    await _destroy(fakes)

    assert sorted(fakes.git.removed_worktrees) == ["alice", "bob"]


async def test_the_retry_takes_the_environment_lock(fakes):
    """The row LOOKS abandoned but may not be: another replica can still hold
    the lock with a release in flight. Queueing behind that lock is what turns
    a genuine concurrent release into 409 ENVIRONMENT_BUSY instead of a second
    teardown racing the first."""
    fakes.repo.row.status = naming.STATUS_ARCHIVING

    await _destroy(fakes)

    assert fakes.locks == ["destroy"]


async def test_the_retry_still_protects_uncommitted_work(fakes):
    """Finishing a release is not a licence to discard code: the retry is an
    ordinary destroy that happens to start from `archiving`, so the same
    confirmation gates it."""
    fakes.repo.row.status = naming.STATUS_ARCHIVING
    fakes.git.pending = {"alice": PendingWork(uncommitted=3, unpushed=0)}

    with pytest.raises(ConflictError, match="uncommitted changes"):
        await _destroy(fakes)

    # Refused before the row was touched again: a refused retry must not leave
    # a stranded release worse than it found it.
    assert fakes.repo.status_updates == []
    assert fakes.repo.row.status == naming.STATUS_ARCHIVING


async def test_the_retry_is_audited_like_any_other_destroy(fakes):
    """The audit trail is how an operator answers "was that reclaim finished,
    and by whom". A retry that recorded nothing would be indistinguishable from
    the abandoned attempt it completed."""
    fakes.repo.row.status = naming.STATUS_ARCHIVING

    await _destroy(fakes)

    destroyed = [e for e in fakes.repo.events if e["action"] == naming.ACTION_DESTROY]
    assert len(destroyed) == 1
    assert destroyed[0]["detail"]["remote_branch_preserved"] == f"env/{_ENV}"


# ── Branch source (creation side of the same contract) ──────────────────────


def test_branch_source_vocabulary_is_shared_with_the_runtime():
    """The registry compares against these constants; a rename on one side
    would silently stop matching and make every reuse look like a fresh
    branch."""
    assert BRANCH_SOURCE_REUSED_REMOTE == "reused_remote"
    assert BRANCH_SOURCE_CREATED_FROM_BASE == "created_from_base"
