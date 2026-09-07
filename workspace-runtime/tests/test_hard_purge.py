"""Hard purge: everything this service holds for a workspace, and nothing else.

Deleting a workspace used to remove only its directories. Everything else
survived and was inherited by the next workspace created under the same name —
ghost knowledge-base entries, resurrected editor drafts, another workspace's
apply history. These tests pin the full erasure, and equally importantly its
LIMITS:

* only rows of the three tables this service OWNS are deleted (the control
  plane's rows are its own to remove; two writers on one table is how drift
  starts);
* scoping is by ``workspace_path`` prefix WITH a trailing slash, so purging
  ``ws-shop`` cannot take ``ws-shop-archive`` with it;
* the remote Git repository is never touched — the capability to delete one no
  longer exists anywhere in this service.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings  # noqa: E402
from src.workspace.database import get_session, init_db, reset_engine  # noqa: E402
from src.workspace.models import (  # noqa: E402
    FileDraftModel,
    KnowledgeDocumentModel,
    PlanApplyHistoryModel,
)
from src.workspace.scaffold import WorkspaceProvisioner, _is_under  # noqa: E402

_TENANT = "t1"
_WORKSPACE = "ws-t1-shop"
_NEIGHBOUR = "ws-t1-shop-archive"
_USER = "u1"


@pytest.fixture()
async def db(tmp_path, monkeypatch):
    """A throwaway SQLite database plus a workspace base directory."""
    base = tmp_path / "workspaces"
    base.mkdir()
    monkeypatch.setattr(settings, "workspace_base_path", str(base))
    monkeypatch.setenv("WORKSPACE_RUNTIME_DB_TYPE", "sqlite")
    monkeypatch.setenv(
        "WORKSPACE_RUNTIME_DB_SQLITE_PATH", str(tmp_path / "runtime.db")
    )
    reset_engine()
    await init_db()
    yield str(base)
    reset_engine()


def _worktree(base: str, workspace_id: str, environment: str = "prod") -> str:
    path = Path(base) / _TENANT / workspace_id / _USER / "envs" / environment
    path.mkdir(parents=True, exist_ok=True)
    (path / "marker.txt").write_text("x", encoding="utf-8")
    return str(path)


async def _seed_rows(workspace_path: str) -> None:
    """One row in each owned table, all keyed by ``workspace_path``."""
    async with get_session() as session:
        session.add(
            KnowledgeDocumentModel(
                id=f"kd-{workspace_path}",
                workspace_path=workspace_path,
                title="doc",
                filename="doc.md",
                content_type="markdown",
                file_size=1,
            )
        )
        session.add(
            FileDraftModel(
                id=f"fd-{workspace_path}",
                workspace_path=workspace_path,
                file_path="models/a.sql",
                content="select 1",
            )
        )
        session.add(
            PlanApplyHistoryModel(
                id=f"pah-{workspace_path}",
                workspace_path=workspace_path,
                environment="prod",
                applied=1,
            )
        )


async def _count(model, workspace_path: str) -> int:
    async with get_session() as session:
        result = await session.execute(
            select(func.count())
            .select_from(model)
            .where(model.workspace_path == workspace_path)
        )
        return int(result.scalar_one() or 0)


# ── Full erasure ───────────────────────────────────────────────────────────


async def test_hard_purge_removes_rows_and_directories(db):
    """The inherited-residue bug: nothing of the previous life may survive."""
    base = db
    workspace_path = _worktree(base, _WORKSPACE)
    await _seed_rows(workspace_path)

    result = await WorkspaceProvisioner().hard_purge_workspace(
        tenant_id=_TENANT, workspace_id=_WORKSPACE
    )

    assert result["purged"] is True
    assert result["directory_removed"] is True
    assert result["rows_deleted"] == {
        "knowledge_documents": 1,
        "file_drafts": 1,
        "plan_apply_history": 1,
    }
    for model in (KnowledgeDocumentModel, FileDraftModel, PlanApplyHistoryModel):
        assert await _count(model, workspace_path) == 0
    assert not (Path(base) / _TENANT / _WORKSPACE).exists()


async def test_hard_purge_removes_every_member_copy(db):
    """A workspace is materialized once per member; a partial purge leaks code."""
    base = db
    for user in (_USER, "u2"):
        path = Path(base) / _TENANT / _WORKSPACE / user / "envs" / "prod"
        path.mkdir(parents=True, exist_ok=True)

    await WorkspaceProvisioner().hard_purge_workspace(
        tenant_id=_TENANT, workspace_id=_WORKSPACE
    )

    assert not (Path(base) / _TENANT / _WORKSPACE).exists()


async def test_hard_purge_is_idempotent(db):
    """Broadcast to several replicas and retried, so it must never raise."""
    base = db
    _worktree(base, _WORKSPACE)
    provisioner = WorkspaceProvisioner()

    first = await provisioner.hard_purge_workspace(
        tenant_id=_TENANT, workspace_id=_WORKSPACE
    )
    second = await provisioner.hard_purge_workspace(
        tenant_id=_TENANT, workspace_id=_WORKSPACE
    )

    assert first["directory_removed"] is True
    assert second["directory_removed"] is False
    assert second["purged"] is True
    assert second["rows_deleted"] == {
        "knowledge_documents": 0,
        "file_drafts": 0,
        "plan_apply_history": 0,
    }


# ── Scope: the trailing-slash guard ───────────────────────────────────────


async def test_purge_does_not_reach_a_prefix_neighbour(db):
    """``ws-t1-shop`` must not match ``ws-t1-shop-archive``.

    Without the trailing slash on the prefix this deletes another workspace's
    rows and stops its deployments — a silent cross-workspace data loss.
    """
    base = db
    target = _worktree(base, _WORKSPACE)
    neighbour = _worktree(base, _NEIGHBOUR)
    await _seed_rows(target)
    await _seed_rows(neighbour)

    result = await WorkspaceProvisioner().hard_purge_workspace(
        tenant_id=_TENANT, workspace_id=_WORKSPACE
    )

    assert result["rows_deleted"]["file_drafts"] == 1
    # The neighbour is untouched, on disk and in the database.
    assert Path(neighbour).is_dir()
    for model in (KnowledgeDocumentModel, FileDraftModel, PlanApplyHistoryModel):
        assert await _count(model, neighbour) == 1


def test_is_under_rejects_prefix_neighbours():
    """The same guard, asserted directly on the helper the deployments use."""
    assert _is_under("/base/t1/ws-shop/u1/envs/prod", "/base/t1/ws-shop")
    assert not _is_under("/base/t1/ws-shop-archive/u1/envs/prod", "/base/t1/ws-shop")
    assert not _is_under("", "/base/t1/ws-shop")
    assert not _is_under("/base/t1/ws-shop/u1", "")


# ── Deployments: stopped, and only this workspace's ────────────────────────


async def test_deployments_of_the_workspace_are_stopped_first(db, monkeypatch):
    """A running code location has the working copy mounted.

    Removing the directory underneath it turns a clean teardown into a crash loop
    against a path that no longer exists. This also exercises the real deployment
    manager API — the broad ``except`` around it would otherwise hide a renamed
    function as a silent "0 deployments stopped".
    """
    base = db
    target_path = _worktree(base, _WORKSPACE)
    neighbour_path = _worktree(base, _NEIGHBOUR)

    from src.codeloc import deployments as deployments_module

    destroyed: list[str] = []

    class _Spec:
        def __init__(self, name: str, path: str) -> None:
            self.deployment_name = name
            self.workspace_path = path

    class _Info:
        def __init__(self, spec: _Spec) -> None:
            self.spec = spec

    class _Manager:
        def list(self):
            return [
                _Info(_Spec("shop-prod", target_path)),
                _Info(_Spec("archive-prod", neighbour_path)),
            ]

        async def destroy(self, deployment_name: str) -> bool:
            destroyed.append(deployment_name)
            return True

    monkeypatch.setattr(
        deployments_module, "get_deployment_manager", lambda: _Manager()
    )

    result = await WorkspaceProvisioner().hard_purge_workspace(
        tenant_id=_TENANT, workspace_id=_WORKSPACE
    )

    assert result["deployments_stopped"] == 1
    # The neighbour workspace keeps serving.
    assert destroyed == ["shop-prod"]


async def test_deployment_manager_entry_point_exists():
    """Guard the name the purge imports.

    The import sits inside a best-effort ``try``, so a rename would degrade to
    "nothing to stop" instead of failing — exactly the kind of silent regression
    this asserts away.
    """
    from src.codeloc.deployments import get_deployment_manager

    manager = get_deployment_manager()
    assert hasattr(manager, "list")
    assert hasattr(manager, "destroy")


# ── Limits: never the remote, never another owner's tables ────────────────


def test_remote_deletion_capability_does_not_exist():
    """Requirement: deleting a workspace must never delete the Git repository.

    Asserted structurally rather than behaviourally: a test that "no delete was
    called" passes just as well when someone adds the call back behind a flag.
    The capability itself is what must be absent.
    """
    from src.git import ops
    from src.workspace import scaffold

    assert not hasattr(ops.GitOperations, "delete_remote_repo")
    assert not hasattr(scaffold.WorkspaceProvisioner, "_delete_remote_repo")
    source = Path(scaffold.__file__).read_text(encoding="utf-8")
    assert "delete_remote" not in source


def test_purge_only_touches_tables_this_service_owns():
    """Reaching into control-plane tables would put two writers on one table."""
    source = Path(
        WorkspaceProvisioner.__module__.replace(".", "/") + ".py"
    )
    text = source.read_text(encoding="utf-8")
    for foreign in ("WorkspaceModel", "EnvironmentModel", "UserGitCredentialModel"):
        assert foreign not in text, f"{foreign} is owned by control-service"
