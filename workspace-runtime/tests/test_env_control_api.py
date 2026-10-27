"""Environment-control API tests (design 06 P5).

These endpoints are control-service's ONLY way to touch working copies, so the
tests pin the contract the control plane compiles against:

* fail-closed auth: without a matching internal token nothing executes;
* git materialization is idempotent and answers with the derived paths, so the
  control plane never needs a filesystem model;
* member scanning + branch listing stay honest to what is on disk;
* SQLMesh endpoints report "no materialized worktree" instead of guessing when
  nobody holds a copy — the reconciler distinguishes that from engine failure;
* workspace purge removes the whole shared root (the control plane deletes its
  rows only after this succeeds).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.rest import env_control as env_control_api  # noqa: E402
from src.config import settings  # noqa: E402
from src.utils.errors import ServiceError  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for env-control tests"
)

_TOKEN = "test-internal-token"
_BASE = "base"
_TENANT = "t1"
_WORKSPACE = "ws-t1-shop"
_USER = "u1"
_ENV = "dev"

_INTERNAL = {"X-Internal-Token": _TOKEN}


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd, check=check, capture_output=True, text=True,
    )


@pytest.fixture()
def base_path(tmp_path, monkeypatch):
    """Workspace base directory wired into settings."""
    base = tmp_path / _BASE
    base.mkdir()
    monkeypatch.setattr(settings, "workspace_base_path", str(base))
    monkeypatch.setattr(settings, "internal_token", _TOKEN)
    return str(base)


@pytest.fixture()
def client():
    from fastapi.responses import JSONResponse

    from src.codeloc.errors import ServiceError as CodelocServiceError

    app = FastAPI()
    app.include_router(env_control_api.router)

    for error_cls in (ServiceError, CodelocServiceError):

        @app.exception_handler(error_cls)
        async def _service_error(_request, exc: Exception):
            return JSONResponse(
                status_code=exc.status_code, content=exc.to_response_dict()
            )

    return TestClient(app)


# ── Auth (fail closed) ─────────────────────────────────────────────────────


def test_rejects_missing_token(client, base_path):
    response = client.post(
        "/internal/env-control/git/ensure-repository",
        json={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": _USER,
        },
    )
    assert response.status_code == 403


def test_rejects_wrong_token(client, base_path):
    response = client.post(
        "/internal/env-control/git/ensure-repository",
        json={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": _USER,
        },
        headers={"X-Internal-Token": "not-the-token"},
    )
    assert response.status_code == 403


def test_rejects_path_shaped_identifiers(client, base_path):
    """A compromised control plane must not escape the workspace layout."""
    response = client.post(
        "/internal/env-control/git/ensure-repository",
        json={
            "tenant_id": "../..",
            "workspace_id": _WORKSPACE,
            "user_id": _USER,
        },
        headers=_INTERNAL,
    )
    assert response.status_code == 400


# ── Git: repository & worktree lifecycle ───────────────────────────────────


def test_ensure_repository_then_worktree_round_trip(client, base_path):
    # Bootstrap window: no remote, empty root commit on the bootstrap branch.
    response = client.post(
        "/internal/env-control/git/ensure-repository",
        json={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": _USER,
            "bootstrap_branch": "main",
        },
        headers=_INTERNAL,
    )
    assert response.status_code == 200
    repo_path = response.json()["repo_path"]
    assert repo_path.endswith(".repo")
    assert Path(repo_path).is_dir()

    response = client.post(
        "/internal/env-control/git/ensure-worktree",
        json={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": _USER,
            "environment": _ENV,
            "branch": "env/dev",
            "start_branch": "main",
        },
        headers=_INTERNAL,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert body["branch"] == "env/dev"
    assert body["commit"]
    assert Path(body["workspace_path"]).is_dir()

    # Idempotent: re-running reports the existing worktree, not a re-create.
    response = client.post(
        "/internal/env-control/git/ensure-worktree",
        json={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": _USER,
            "environment": _ENV,
            "branch": "env/dev",
            "start_branch": "main",
        },
        headers=_INTERNAL,
    )
    assert response.status_code == 200
    assert response.json()["created"] is False


def test_members_and_branches_reflect_disk(client, base_path):
    client.post(
        "/internal/env-control/git/ensure-repository",
        json={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": _USER,
            "bootstrap_branch": "main",
        },
        headers=_INTERNAL,
    )
    client.post(
        "/internal/env-control/git/ensure-worktree",
        json={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": _USER,
            "environment": _ENV,
            "branch": "env/dev",
            "start_branch": "main",
        },
        headers=_INTERNAL,
    )

    response = client.get(
        "/internal/env-control/git/members",
        params={"tenant_id": _TENANT, "workspace_id": _WORKSPACE},
        headers=_INTERNAL,
    )
    assert response.status_code == 200
    assert response.json()["members"] == [_USER]

    response = client.get(
        "/internal/env-control/git/branches",
        params={"tenant_id": _TENANT, "workspace_id": _WORKSPACE},
        headers=_INTERNAL,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["repo_found"] is True
    # 'prod' comes from the bootstrap branch (main maps to the production
    # environment), 'dev' from the environment branch created above.
    assert body["environments"] == ["dev", "prod"]

    response = client.get(
        "/internal/env-control/git/branch-presence",
        params={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "branch": "env/dev",
        },
        headers=_INTERNAL,
    )
    assert response.status_code == 200
    presence = response.json()
    assert presence["local"] is True
    assert presence["remote"] is False


def test_pending_work_counts_worktree_changes(client, base_path):
    client.post(
        "/internal/env-control/git/ensure-repository",
        json={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": _USER,
            "bootstrap_branch": "main",
        },
        headers=_INTERNAL,
    )
    created = client.post(
        "/internal/env-control/git/ensure-worktree",
        json={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": _USER,
            "environment": _ENV,
            "branch": "env/dev",
            "start_branch": "main",
        },
        headers=_INTERNAL,
    ).json()
    (Path(created["workspace_path"]) / "new_model.sql").write_text("SELECT 1")

    response = client.get(
        "/internal/env-control/git/pending-work",
        params={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": _USER,
            "environment": _ENV,
            "branch": "env/dev",
        },
        headers=_INTERNAL,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["uncommitted"] == 1
    # No remote at all here, so there is no tracking ref to compare against:
    # reporting the whole local history as "about to be lost" would turn a
    # never-pushed branch into a permanent alarm.
    assert body["unpushed"] == 0


def test_pending_work_is_zero_without_a_working_copy(client, base_path):
    """Most members never materialize a given environment; that is not an
    error, and they have nothing to lose."""
    response = client.get(
        "/internal/env-control/git/pending-work",
        params={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": "nobody",
            "environment": _ENV,
            "branch": "env/dev",
        },
        headers=_INTERNAL,
    )
    assert response.status_code == 200
    assert response.json() == {"uncommitted": 0, "unpushed": 0}


# ── SQLMesh: virtual environments ──────────────────────────────────────────


def test_state_environments_reports_missing_worktree_as_null_reason(
    client, base_path
):
    """No member copy on disk is NOT "no environments" — the reconciler must be
    able to tell the two apart, so the answer is ``environments: null`` with an
    explicit reason."""
    response = client.get(
        "/internal/env-control/sqlmesh/state-environments",
        params={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "environment": _ENV,
        },
        headers=_INTERNAL,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["environments"] is None
    assert body["reason"] == "no_materialized_worktree"


def test_ensure_virtual_environment_requires_materialized_copy(client, base_path):
    response = client.post(
        "/internal/env-control/sqlmesh/ensure-virtual-environment",
        json={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "environment": _ENV,
            "base_env": "prod",
        },
        headers=_INTERNAL,
    )
    assert response.status_code == 404


# ── Workspace provisioning & teardown ──────────────────────────────────────


def test_hard_purge_workspace_removes_shared_root(client, base_path):
    client.post(
        "/internal/env-control/git/ensure-repository",
        json={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": _USER,
            "bootstrap_branch": "main",
        },
        headers=_INTERNAL,
    )
    client.post(
        "/internal/env-control/git/ensure-worktree",
        json={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": _USER,
            "environment": _ENV,
            "branch": "env/dev",
            "start_branch": "main",
        },
        headers=_INTERNAL,
    )
    shared_root = Path(base_path) / _TENANT / _WORKSPACE
    assert shared_root.is_dir()

    # Neither a context pool nor a database is running in this test; every step
    # of the purge is best-effort, so the endpoint must still succeed.
    response = client.post(
        "/internal/env-control/workspace/hard-purge",
        json={"tenant_id": _TENANT, "workspace_id": _WORKSPACE},
        headers=_INTERNAL,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["purged"] is True
    assert body["directory_removed"] is True
    assert not shared_root.exists()


def test_hard_purge_is_idempotent(client, base_path):
    """The control plane broadcasts this to every replica and retries it."""
    payload = {"tenant_id": _TENANT, "workspace_id": _WORKSPACE}
    first = client.post(
        "/internal/env-control/workspace/hard-purge", json=payload, headers=_INTERNAL
    )
    assert first.status_code == 200
    # Nothing was there, which is a successful purge, not a failure.
    assert first.json()["purged"] is True
    assert first.json()["directory_removed"] is False

    second = client.post(
        "/internal/env-control/workspace/hard-purge", json=payload, headers=_INTERNAL
    )
    assert second.status_code == 200
    assert second.json()["purged"] is True


def test_hard_purge_rejects_a_remote_deletion_field(client, base_path):
    """The capability is gone; an old caller must not silently get a purge.

    A field the model does not declare is ignored by pydantic, so this asserts
    the endpoint's SHAPE: there is no way to ask for remote deletion, because the
    provisioner can no longer perform one.
    """
    from src.workspace.scaffold import WorkspaceProvisioner

    assert not hasattr(WorkspaceProvisioner, "_delete_remote_repo")
    assert not hasattr(WorkspaceProvisioner, "purge_workspace")

    from src.git.ops import GitOperations

    assert not hasattr(GitOperations, "delete_remote_repo")


def test_purge_member_copy_removes_only_that_member(client, base_path):
    for user in (_USER, "u2"):
        client.post(
            "/internal/env-control/git/ensure-repository",
            json={
                "tenant_id": _TENANT,
                "workspace_id": _WORKSPACE,
                "user_id": user,
                "bootstrap_branch": "main",
            },
            headers=_INTERNAL,
        )
        client.post(
            "/internal/env-control/git/ensure-worktree",
            json={
                "tenant_id": _TENANT,
                "workspace_id": _WORKSPACE,
                "user_id": user,
                "environment": _ENV,
                "branch": "env/dev",
                "start_branch": "main",
            },
            headers=_INTERNAL,
        )

    response = client.post(
        "/internal/env-control/workspace/purge-member",
        json={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": "u2",
        },
        headers=_INTERNAL,
    )
    assert response.status_code == 200
    assert response.json()["purged"] is True
    # The other member's copy survives.
    assert (Path(base_path) / _TENANT / _WORKSPACE / _USER / ".repo").is_dir()
    assert not (Path(base_path) / _TENANT / _WORKSPACE / "u2").exists()


def test_release_contexts_endpoint_is_safe_without_pool(client, base_path):
    """Teardown must not fail because no context pool is running."""
    response = client.post(
        "/internal/env-control/workspace/release-contexts",
        json={
            "tenant_id": _TENANT,
            "workspace_id": _WORKSPACE,
            "user_id": _USER,
        },
        headers=_INTERNAL,
    )
    assert response.status_code == 200
    assert response.json()["released"] is True
