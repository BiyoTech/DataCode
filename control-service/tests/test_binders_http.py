"""HTTP binder tests (design 06 P5).

The binders keep the legacy in-process CONTRACT (method names, return types,
"report drift, never raise") while shipping every side effect to
workspace-runtime. What needs pinning here is that translation:

* a workspace_path handed to the SQLMesh binder reaches the runtime as logical
  identifiers — the control plane never trusts its own paths over the wire;
* a 404 from the runtime (no materialized copy) surfaces as ``False``, the
  signal the registry records as an honest partial outcome;
* the Git binder's command results mirror the runtime's ok/reason/detail shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.environments.git_binder import GitBinder, GitCommandResult  # noqa: E402
from src.environments.sqlmesh_binder import SqlMeshBinder  # noqa: E402
from src.runtime.client import RuntimeReply, RuntimeTarget  # noqa: E402
from src.utils.errors import RuntimeUnavailableError, WorkspaceNotFoundError  # noqa: E402


class FakeRuntime:
    """In-memory double of the env-control surface, recording every call.

    Records the routing target as well: which replica a call reaches is part
    of the binder's contract, not an implementation detail of the client.
    """

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.targets: list[RuntimeTarget | None] = []
        self.responses: dict[str, object] = {}

    def _answer(self, method: str, path: str):
        key = f"{method} {path.split('?')[0]}"
        answer = self.responses.get(key)
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else {}

    async def post(self, path: str, payload: dict, *, target=None) -> dict:
        self.calls.append(("POST", path, payload))
        self.targets.append(target)
        return self._answer("POST", path)

    async def get(self, path: str, params: dict | None = None, *, target=None) -> dict:
        self.calls.append(("GET", path, dict(params or {})))
        self.targets.append(target)
        return self._answer("GET", path)

    async def post_all(self, path: str, payload: dict, *, target, also=()):
        return [RuntimeReply(addr="r1", body=await self.post(path, payload, target=target))]

    async def get_all(self, path: str, params: dict | None = None, *, target):
        return [RuntimeReply(addr="r1", body=await self.get(path, params, target=target))]


@pytest.fixture()
def fake() -> FakeRuntime:
    return FakeRuntime()


# ── GitBinder ───────────────────────────────────────────────────────────────


async def test_ensure_worktree_returns_runtime_materialization(fake):
    fake.responses["POST /internal/env-control/git/ensure-worktree"] = {
        "workspace_path": "/base/t1/ws/u1/envs/dev",
        "branch": "env/dev",
        "commit": "abc1234",
        "created": True,
        "branch_source": "reused_remote",
    }
    binder = GitBinder(client=fake)

    result = await binder.ensure_worktree(
        tenant_id="t1",
        workspace_id="ws",
        user_id="u1",
        environment="dev",
        branch="env/dev",
        start_branch="main",
    )

    assert result.created is True
    assert result.commit == "abc1234"
    assert result.workspace_path == "/base/t1/ws/u1/envs/dev"
    # The runtime alone decides where the branch came from; the binder carries
    # the verdict rather than re-deriving it.
    assert result.branch_source == "reused_remote"
    method, path, payload = fake.calls[-1]
    assert (method, path) == ("POST", "/internal/env-control/git/ensure-worktree")
    assert payload["tenant_id"] == "t1"
    assert payload["environment"] == "dev"
    # Addressed at the member's own copy: this call places the lease.
    assert fake.targets[-1] == RuntimeTarget(
        tenant_id="t1", workspace_id="ws", user_id="u1", environment="dev"
    )


async def test_push_branch_is_addressed_by_the_copy_it_reads(fake):
    """The credential's user and the copy's owner are distinct identities."""
    import os

    from src.config import settings

    base = os.path.expanduser(settings.workspace_base_path)
    binder = GitBinder(client=fake)

    await binder.push_branch(
        worktree_path=f"{base}/t1/ws-t1-shop/owner/envs/dev",
        branch="env/dev",
        user_id="someone-else",
        git_repo_url="https://github.com/org/repo",
    )

    assert fake.targets[-1] == RuntimeTarget(
        tenant_id="t1", workspace_id="ws-t1-shop", user_id="owner", environment="dev"
    )


async def test_pending_work_returns_both_counts(fake):
    """Both halves of "what would this release lose" in one round trip."""
    fake.responses["GET /internal/env-control/git/pending-work"] = {
        "uncommitted": 2,
        "unpushed": 3,
    }
    binder = GitBinder(client=fake)

    work = await binder.pending_work(
        tenant_id="t1",
        workspace_id="ws",
        user_id="u1",
        environment="dev",
        branch="env/dev",
    )

    assert (work.uncommitted, work.unpushed) == (2, 3)
    method, path, params = fake.calls[-1]
    assert (method, path) == ("GET", "/internal/env-control/git/pending-work")
    # The branch must travel: without it the runtime cannot resolve the
    # tracking ref the unpushed count is measured against.
    assert params["branch"] == "env/dev"


async def test_git_binder_exposes_no_remote_branch_deletion(fake):
    """Structural guard for the whole point of this contract.

    The platform never deletes a remote branch, and the verifiable form of
    that promise is that no binder method can even ask for it. A future
    "cleanup" helper re-introducing the capability fails here.
    """
    binder = GitBinder(client=fake)

    assert not [
        name
        for name in dir(binder)
        if "delete" in name and "remote" in name
    ]


async def test_members_round_trip(fake):
    fake.responses["GET /internal/env-control/git/members"] = {"members": ["u1", "u2"]}
    binder = GitBinder(client=fake)

    members = await binder.list_member_user_ids(tenant_id="t1", workspace_id="ws")

    assert members == ["u1", "u2"]
    method, path, params = fake.calls[-1]
    assert (method, path) == ("GET", "/internal/env-control/git/members")
    assert params == {"tenant_id": "t1", "workspace_id": "ws"}


async def test_first_existing_worktree_queries_logical_endpoint(fake):
    fake.responses["GET /internal/env-control/git/first-existing"] = {
        "repo_path": "/base/t1/ws/u1/.repo",
        "worktree_path": "/base/t1/ws/u1/envs/dev",
    }
    binder = GitBinder(client=fake)

    worktree = await binder.first_existing_worktree(
        tenant_id="t1", workspace_id="ws", environment="dev"
    )

    assert worktree == "/base/t1/ws/u1/envs/dev"
    _, _, params = fake.calls[-1]
    assert params["environment"] == "dev"


async def test_push_branch_mirrors_command_result(fake):
    fake.responses["POST /internal/env-control/git/push-branch"] = {
        "ok": False,
        "reason": "credential_missing",
        "detail": "no credential configured",
    }
    binder = GitBinder(client=fake)
    import os

    from src.config import settings

    base = os.path.expanduser(settings.workspace_base_path)
    result = await binder.push_branch(
        worktree_path=f"{base}/t1/ws-t1-shop/u1/envs/dev",
        branch="env/dev",
        user_id="u1",
        git_repo_url="https://github.com/org/repo",
    )

    assert result == GitCommandResult(
        ok=False, reason="credential_missing", detail="no credential configured"
    )


async def test_branch_presence_parses_repo_path_for_tenancy(fake):
    """The legacy signature carries a repo path; only its tenancy crosses the wire."""
    fake.responses["GET /internal/env-control/git/branch-presence"] = {
        "local": True,
        "remote": False,
    }
    binder = GitBinder(client=fake)
    import os

    from src.config import settings

    base = os.path.expanduser(settings.workspace_base_path)
    repo = f"{base}/t1/ws-t1-shop/u1/.repo"

    local, remote = await binder.branch_presence(repo_path=repo, branch="env/dev")

    assert (local, remote) == (True, False)
    _, _, params = fake.calls[-1]
    assert params["tenant_id"] == "t1"
    assert params["workspace_id"] == "ws-t1-shop"


# ── SqlMeshBinder ───────────────────────────────────────────────────────────


async def test_ensure_virtual_environment_reports_drift_not_raise(fake):
    fake.responses["POST /internal/env-control/sqlmesh/ensure-virtual-environment"] = {
        "ok": False
    }
    binder = SqlMeshBinder(client=fake)
    import os

    from src.config import settings

    base = os.path.expanduser(settings.workspace_base_path)
    worktree = f"{base}/t1/ws-t1-shop/u1/envs/dev"

    ok = await binder.ensure_virtual_environment(
        workspace_path=worktree, environment="dev", base_env="prod"
    )

    assert ok is False  # drift, never an exception
    _, _, payload = fake.calls[-1]
    assert payload["tenant_id"] == "t1"
    assert payload["workspace_id"] == "ws-t1-shop"
    assert payload["environment"] == "dev"


async def test_state_environments_none_is_not_empty(fake):
    """``None`` (engine unreachable) must stay distinguishable from an empty set."""
    fake.responses["GET /internal/env-control/sqlmesh/state-environments"] = {
        "environments": None,
        "reason": "no_materialized_worktree",
    }
    binder = SqlMeshBinder(client=fake)
    import os

    from src.config import settings

    base = os.path.expanduser(settings.workspace_base_path)
    worktree = f"{base}/t1/ws-t1-shop/u1/envs/dev"

    assert await binder.list_state_environments(workspace_path=worktree) is None


async def test_invalidate_maps_missing_worktree_to_false(fake):
    fake.responses["POST /internal/env-control/sqlmesh/invalidate-virtual-environment"] = (
        WorkspaceNotFoundError("no copy materialized")
    )
    binder = SqlMeshBinder(client=fake)

    assert await binder.invalidate_virtual_environment(
        tenant_id="t1", workspace_id="ws-t1-shop", environment="dev"
    ) is False


async def test_invalidate_maps_unreachable_runtime_to_false(fake):
    fake.responses["POST /internal/env-control/sqlmesh/invalidate-virtual-environment"] = (
        RuntimeUnavailableError("invalidate", "down")
    )
    binder = SqlMeshBinder(client=fake)

    assert await binder.invalidate_virtual_environment(
        tenant_id="t1", workspace_id="ws-t1-shop", environment="dev"
    ) is False


async def test_invalidate_is_addressed_by_ids_not_by_a_path(fake):
    """Regression: this used to take a working-copy path, and its only caller
    synthesized one with a ``_`` user segment — which the layout validator
    rejects. A destroy therefore raised immediately after writing
    ``archiving``, stranding the environment in that status forever.

    Taking the ids directly removes the ability to express that mistake.
    """
    import inspect

    params = inspect.signature(
        SqlMeshBinder(client=fake).invalidate_virtual_environment
    ).parameters
    assert "workspace_path" not in params
    assert {"tenant_id", "workspace_id"} <= set(params)
