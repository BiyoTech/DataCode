"""Workspace row lifecycle: creation, physical deletion, and name reuse.

This file replaces the former ``reclaim_deleted`` timestamp tests. Their subject
no longer exists, and the reason is worth recording: soft deletion left a
tombstone that kept occupying the primary key (ids derive from
``{tenant}/{name}``) while being invisible to every lookup, so recreating a name
had to go through a special "reclaim" path. That path produced two production
bugs on its own — an archived production environment that could not be opened,
and un-stamped ORM timestamps that crashed the response with
``None.isoformat()`` AFTER the workspace, its environment, the scaffold and the
push had all succeeded.

Deletion is now physical, so recreating a name is an ordinary INSERT and the ORM
fills the timestamps itself. These tests pin exactly that, against real SQLite so
the real statements are exercised: a stubbed repository would hide the mechanism
that broke last time.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.workspace.database import init_db, reset_engine
from src.workspace.models import WorkspaceModel
from src.workspace.repository import WorkspaceRepository

_WS_ID = "ws-t1-admin-test"
_TENANT = "t1"


def _as_utc(value: datetime) -> datetime:
    """Normalize a timestamp read back from SQLite to a UTC-aware datetime.

    SQLite has no temporal type: SQLAlchemy hands back a NAIVE datetime even
    from a ``DateTime(timezone=True)`` column, whereas PostgreSQL returns an
    aware one. Values written are always UTC, so treating a naive read-back as
    UTC is exact rather than a convenience.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _model(owner: str, status: str = "active") -> WorkspaceModel:
    """A workspace model as ``WorkspaceManager.create`` builds it.

    No timestamps are passed, because the ORM defaults are supposed to fill
    them in — the property the old reclaim path silently broke.
    """
    return WorkspaceModel(
        id=_WS_ID,
        tenant_id=_TENANT,
        owner_user_id=owner,
        name="admin-test",
        dialect="duckdb",
        git_repo_url=None,
        production_env="prod",
        default_env="prod",
        status=status,
    )


@pytest.fixture()
async def db(tmp_path, monkeypatch):
    """A throwaway SQLite database with the workspaces table created."""
    db_path = tmp_path / "workspaces.db"
    monkeypatch.setenv("CONTROL_SERVICE_DB_TYPE", "sqlite")
    monkeypatch.setenv("CONTROL_SERVICE_DB_SQLITE_PATH", str(db_path))
    reset_engine()
    await init_db()
    yield
    reset_engine()


async def test_create_stamps_every_timestamp(db) -> None:
    """The response serializer calls ``.isoformat()`` on these directly."""
    repo = WorkspaceRepository()
    model = _model(owner="owner-1")
    assert model.created_at is None

    await repo.create(model)

    assert model.created_at is not None
    assert model.updated_at is not None
    assert model.last_active_at is not None
    # The exact calls that used to raise AttributeError on the reclaim path.
    assert model.created_at.isoformat()
    assert model.last_active_at.isoformat()


async def test_purge_removes_the_row_entirely(db) -> None:
    """No tombstone: the id must be free for an ordinary INSERT afterwards."""
    repo = WorkspaceRepository()
    await repo.create(_model(owner="owner-1"))

    assert await repo.purge(_WS_ID) is True

    assert await repo.get_by_id(_WS_ID) is None
    assert await repo.get_in_tenant(_WS_ID, _TENANT) is None


async def test_purge_is_idempotent(db) -> None:
    """Teardown is retried on failure paths; a second call must not raise."""
    repo = WorkspaceRepository()
    await repo.create(_model(owner="owner-1"))

    assert await repo.purge(_WS_ID) is True
    assert await repo.purge(_WS_ID) is False


async def test_recreating_a_purged_name_is_a_plain_insert(db) -> None:
    """The scenario behind both historical bugs, now with no special path.

    A fresh row means fresh timestamps and a fresh owner — nothing is inherited
    from the workspace that used to hold this id.
    """
    repo = WorkspaceRepository()
    await repo.create(_model(owner="old-owner"))
    first_created = _as_utc((await repo.get_by_id(_WS_ID)).created_at)
    await repo.purge(_WS_ID)

    recreated = _model(owner="new-owner")
    await repo.create(recreated)

    stored = await repo.get_by_id(_WS_ID)
    assert stored is not None
    assert stored.owner_user_id == "new-owner"
    assert stored.status == "active"
    # A new lifetime, not the previous one carried over.
    assert _as_utc(stored.created_at) >= first_created


async def test_purged_workspace_is_not_visible_to_the_duplicate_check(db) -> None:
    """``create`` rejects an existing id via ``get_by_id``; a purged row is gone.

    With a tombstone this lookup answered "absent" while the INSERT still hit a
    primary-key conflict — the retry could never succeed.
    """
    repo = WorkspaceRepository()
    await repo.create(_model(owner="owner-1"))
    await repo.purge(_WS_ID)

    assert await repo.get_by_id(_WS_ID) is None
    # And the INSERT the check guards really does succeed.
    await repo.create(_model(owner="owner-2"))
    assert (await repo.get_by_id(_WS_ID)).owner_user_id == "owner-2"


async def test_cross_tenant_lookup_still_hides_the_workspace(db) -> None:
    """Physical deletion must not have widened tenant visibility."""
    repo = WorkspaceRepository()
    await repo.create(_model(owner="owner-1"))
    assert await repo.get_in_tenant(_WS_ID, "another-tenant") is None
