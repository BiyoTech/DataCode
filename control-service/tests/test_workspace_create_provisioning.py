"""Workspace creation: the remote decides whether the template may be written.

The one assertion that carries the requirement is
``test_reusable_remote_is_never_scaffolded``: a repository that already contains
a project must reach the user's working copy verbatim. Scaffolding it would
overwrite real code with the platform template — a data-loss outcome, not a
cosmetic one.

The rest pins the surrounding decisions:

* the remote is resolved BEFORE the workspace row exists, so a rejected URL
  leaves nothing behind (this is also what turns "repository not found" into an
  immediate, named failure instead of a later "add worktree" error);
* a repository we created, or no remote at all, IS scaffolded and pushed;
* an empty existing remote gets a bootstrap commit but no template and no push;
* a failure after creation rolls the workspace back and never touches the remote.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field

import pytest
from _operation_fakes import MemoryRecorder
from datacode_common import naming

from src.utils.errors import ConflictError, GitRemoteAccessError
from src.workspace.manager import WorkspaceManager

_TENANT = "t1"
_OWNER = "u1"
_NAME = "loganhu-test"
_URL = "https://github.com/BiyoTech/loganhu-test.git"


@dataclass
class _FakeRuntimeClient:
    """Records every env-control call so the scaffold decision is observable.

    The routing target is recorded too: the scaffold and the push must be
    addressed at the owner's production copy, which is where ``open`` placed
    the lease.
    """

    provision: dict = field(default_factory=dict)
    provision_error: t.Optional[Exception] = None
    push_ok: bool = True
    calls: list = field(default_factory=list)
    targets: dict = field(default_factory=dict)

    async def post(self, path: str, payload: dict, *, target=None) -> dict:
        self.calls.append((path, payload))
        self.targets[path] = target
        if path.endswith("/workspace/provision-remote"):
            if self.provision_error is not None:
                raise self.provision_error
            return self.provision
        if path.endswith("/workspace/push-initial"):
            return {"ok": self.push_ok, "reason": "" if self.push_ok else "repo_not_found"}
        return {}

    async def post_all(self, path: str, payload: dict, *, target, also=()):
        from src.runtime.client import RuntimeReply

        return [RuntimeReply(addr="r1", body=await self.post(path, payload, target=target))]

    async def get(self, path: str, params: dict | None = None, *, target=None) -> dict:
        self.calls.append((path, params or {}))
        return {"members": []}

    def paths(self) -> list:
        return [path for path, _ in self.calls]


@dataclass
class _FakeWorkspaceRepository:
    existing: t.Optional[t.Any] = None
    created: list = field(default_factory=list)

    async def get_by_id(self, workspace_id: str, include_deleted: bool = False):
        return self.existing

    async def create(self, model):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        model.created_at = now
        model.updated_at = now
        model.last_active_at = now
        self.created.append(model)
        self.existing = model
        return model

    async def purge(self, workspace_id: str) -> bool:
        self.existing = None
        return True


@dataclass
class _FakeRegistry:
    workspace_path: str = "/base/t1/ws-t1-loganhu-test/u1/envs/prod"
    open_error: t.Optional[Exception] = None
    opens: list = field(default_factory=list)
    purged: list = field(default_factory=list)

    async def purge_all_for_workspace(self, *, workspace_id: str) -> int:
        self.purged.append(workspace_id)
        return 1

    async def ensure_production(self, model, owner_user_id: str):
        @dataclass
        class _View:
            environment_id: str

        return _View(environment_id=naming.environment_id(model.id, naming.PRODUCTION_ENV))

    async def open(
        self,
        *,
        environment_id: str,
        tenant_id: str,
        user_id: str,
        allow_bootstrap_commit: bool = False,
    ):
        self.opens.append(
            {"environment_id": environment_id, "allow_bootstrap_commit": allow_bootstrap_commit}
        )
        if self.open_error is not None:
            raise self.open_error

        @dataclass
        class _Opened:
            workspace_path: str
            branch: str = "main"
            environment: str = naming.PRODUCTION_ENV
            head_commit: str = "abc1234"

        return _Opened(workspace_path=self.workspace_path)


@pytest.fixture()
def wired(monkeypatch):
    """Wire the manager against fakes and hand them back for assertions."""
    client = _FakeRuntimeClient()
    repository = _FakeWorkspaceRepository()
    registry = _FakeRegistry()

    monkeypatch.setattr("src.workspace.manager.get_runtime_client", lambda: client)
    monkeypatch.setattr(
        "src.workspace.manager.get_environment_registry", lambda: registry
    )
    monkeypatch.setattr(
        WorkspaceManager, "_repository", staticmethod(lambda: repository)
    )

    class _Quota:
        async def check_workspace_creation(self, tenant_id: str) -> None:
            return None

    monkeypatch.setattr("src.quota.get_quota_service", lambda: _Quota())

    class _NoLeases:
        async def list_for_workspace(self, *, tenant_id, workspace_id):
            return []

    monkeypatch.setattr(
        "src.environments.lease_binder.get_lease_binder", lambda: _NoLeases()
    )
    recorder = MemoryRecorder()
    monkeypatch.setattr("src.workspace.manager.get_operation_recorder", lambda: recorder)
    manager = WorkspaceManager()
    manager.recorder = recorder  # exposed for the operation-record assertions
    return manager, client, repository, registry


def _provision(state: str, *, scaffold: bool, bootstrap: bool, created: bool = False):
    return {
        "state": state,
        "normalized_url": _URL,
        "default_branch": "main" if state == "reusable" else "",
        "created": created,
        "needs_scaffold": scaffold,
        "needs_bootstrap_commit": bootstrap,
    }


# ── Reuse: never overwrite an existing project ──────────────────────────────


async def test_reusable_remote_is_never_scaffolded(wired):
    """The requirement: existing code is reused verbatim, template not written."""
    manager, client, _repo, registry = wired
    client.provision = _provision("reusable", scaffold=False, bootstrap=False)

    view = await manager.create(_TENANT, _OWNER, _NAME, git_repo_url=_URL)

    assert view.git_repo_url == _URL
    assert "/internal/env-control/workspace/scaffold" not in client.paths()
    # Nor is anything pushed: the repository is already published, and a
    # first commit of ours would be rejected or would land a template on it.
    assert "/internal/env-control/workspace/push-initial" not in client.paths()
    # No root commit is invented either — the remote's history seeds the copy.
    assert registry.opens[0]["allow_bootstrap_commit"] is False


async def test_empty_remote_is_reused_without_a_template(wired):
    """Chosen behaviour for a branchless repository: empty copy, no scaffold."""
    manager, client, _repo, registry = wired
    client.provision = _provision("remote_empty", scaffold=False, bootstrap=True)

    await manager.create(_TENANT, _OWNER, _NAME, git_repo_url=_URL)

    assert "/internal/env-control/workspace/scaffold" not in client.paths()
    assert "/internal/env-control/workspace/push-initial" not in client.paths()
    # A worktree cannot attach to an unborn branch, so the empty root commit is
    # required — it carries no files.
    assert registry.opens[0]["allow_bootstrap_commit"] is True


# ── Creation: scaffold and publish ─────────────────────────────────────────


async def test_created_remote_is_scaffolded_and_pushed(wired):
    manager, client, _repo, registry = wired
    client.provision = _provision(
        "created_empty", scaffold=True, bootstrap=True, created=True
    )

    await manager.create(_TENANT, _OWNER, _NAME, git_repo_url=_URL)

    paths = client.paths()
    assert "/internal/env-control/workspace/scaffold" in paths
    assert "/internal/env-control/workspace/push-initial" in paths
    assert registry.opens[0]["allow_bootstrap_commit"] is True
    # The push targets the URL the provisioner settled on, not the raw input.
    push = next(p for path, p in client.calls if path.endswith("push-initial"))
    assert push["git_repo_url"] == _URL
    # Scaffold and push are addressed at the owner's production copy — the one
    # ``open`` placed the lease for — so they land on the replica that serves
    # the workspace afterwards. Provisioning a remote touches no copy at all.
    from src.runtime.client import RuntimeTarget

    owner_copy = RuntimeTarget(
        tenant_id=_TENANT,
        workspace_id=f"ws-{_TENANT}-{_NAME}",
        user_id=_OWNER,
        environment=naming.PRODUCTION_ENV,
    )
    assert client.targets["/internal/env-control/workspace/scaffold"] == owner_copy
    assert client.targets["/internal/env-control/workspace/push-initial"] == owner_copy
    assert client.targets["/internal/env-control/workspace/provision-remote"] is None


async def test_workspace_without_a_remote_keeps_the_bootstrap_behaviour(wired):
    """No URL means no remote to resolve; the local scaffold path is unchanged."""
    manager, client, _repo, registry = wired

    view = await manager.create(_TENANT, _OWNER, _NAME)

    assert view.git_repo_url == ""
    assert "/internal/env-control/workspace/provision-remote" not in client.paths()
    assert "/internal/env-control/workspace/scaffold" in client.paths()
    assert "/internal/env-control/workspace/push-initial" not in client.paths()
    assert registry.opens[0]["allow_bootstrap_commit"] is True


async def test_failed_initial_push_is_not_fatal(wired):
    """The owner keeps a usable local copy; only sharing is blocked."""
    manager, client, _repo, _registry = wired
    client.provision = _provision(
        "created_empty", scaffold=True, bootstrap=True, created=True
    )
    client.push_ok = False

    view = await manager.create(_TENANT, _OWNER, _NAME, git_repo_url=_URL)
    assert view.workspace_id


# ── Ordering: a rejected URL leaves nothing behind ─────────────────────────


async def test_remote_is_resolved_before_the_workspace_row_exists(wired):
    """A repository the token cannot create must not leave a half workspace.

    This ordering is also what makes the failure legible: the user reads
    "repository not found" instead of a later "add worktree" error.
    """
    manager, client, repository, registry = wired
    client.provision_error = GitRemoteAccessError(
        "provision", "repo_creation_denied", "github.com", "Resource not accessible"
    )

    with pytest.raises(GitRemoteAccessError):
        await manager.create(_TENANT, _OWNER, _NAME, git_repo_url=_URL)

    assert repository.created == []
    assert registry.opens == []
    # The record is closed (nothing was built), so the resumer never sees it.
    assert manager.recorder.steps() == []
    assert manager.recorder.terminal() == "rolled_back"


async def test_existing_workspace_is_rejected_before_touching_the_remote(wired):
    manager, client, repository, _registry = wired
    repository.existing = object()

    with pytest.raises(ConflictError):
        await manager.create(_TENANT, _OWNER, _NAME, git_repo_url=_URL)
    assert client.calls == []


async def test_malformed_url_is_rejected_before_touching_the_remote(wired):
    manager, client, repository, _registry = wired

    with pytest.raises(ConflictError):
        await manager.create(_TENANT, _OWNER, _NAME, git_repo_url="ftp://x/y.git")
    assert client.calls == []


# ── Rollback: never delete the remote ─────────────────────────────────────


async def test_rollback_after_creation_does_not_touch_the_remote(wired):
    """A repository we created is kept: the retry reuses it.

    Deleting it would be the platform destroying a Git repository on the user's
    account — never an acceptable automatic action.
    """
    manager, client, repository, registry = wired
    client.provision = _provision(
        "created_empty", scaffold=True, bootstrap=True, created=True
    )
    registry.open_error = RuntimeError("materialization failed")

    with pytest.raises(RuntimeError):
        await manager.create(_TENANT, _OWNER, _NAME, git_repo_url=_URL)

    # The workspace is rolled back...
    assert repository.existing is None
    assert registry.purged == [f"ws-{_TENANT}-{_NAME}"]
    # ...and nothing resembling a remote deletion was attempted.
    assert not any("delete-remote" in path for path in client.paths())
    assert not any("provision-remote" == path.rsplit("/", 1)[-1]
                   for path in client.paths()[1:])
    # A complete in-process rollback closes the record: nothing to resume.
    assert manager.recorder.terminal() == "rolled_back"
    assert manager.recorder.steps() == ["writing_row", "row_written"]


async def test_successful_creation_records_every_step(wired):
    """The record is what lets the resumer tell 'done' from 'interrupted'."""
    manager, client, _repository, _registry = wired
    client.provision = _provision(
        "created_empty", scaffold=True, bootstrap=True, created=True
    )

    await manager.create(_TENANT, _OWNER, _NAME, git_repo_url=_URL)

    assert manager.recorder.steps() == [
        "writing_row",
        "row_written",
        "opened",
        "scaffolded",
        "pushed",
    ]
    assert manager.recorder.terminal() == "succeeded"
    # The planned final step is recorded up front, so a crash after the push
    # but before ``succeed`` is recognised as finished.
    first_advance = next(p for _, e, p in manager.recorder.events if e == "advance")
    assert first_advance["final_step"] == "pushed"
