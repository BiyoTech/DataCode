"""Tests for ``EnvironmentRegistry.ensure_production``.

Replaying ``WorkspaceManager.create`` for a workspace id that was used before is
the path the user reported breaking (``Environment prod cannot be opened while it
is archived``). The cause was a production row that OUTLIVED its workspace: a
soft-deleted workspace left its environments archived, and every recreation had
to resurrect them through a special "reclaim" path.

Deleting a workspace now removes its environment rows physically, so there is no
such row to meet and no reclaim path to get wrong. What remains to pin is
therefore sharper: a fresh workspace inserts, an operable row is returned
untouched, and ANY other status means a concurrent create is in flight — a
conflict to report, never something to overwrite.

Each test substitutes the registry's repository, Git binder, SQLMesh binder,
Dagster binder and workspace repository with in-memory fakes: ``ensure_production``
only touches ``_repo`` and the audit recorder, so the rest of the registry's
baggage is replaced with no-op stand-ins to keep the test honest about scope.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from datacode_common import naming

from src.environments.registry import EnvironmentRegistry
from src.utils.errors import ConflictError
from src.workspace.models import WorkspaceModel

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
_WS_ID = "ws-t1-admin-test"
_PROD_ID = naming.environment_id(_WS_ID, naming.PRODUCTION_ENV)


@dataclass
class _EnvRow:
    """Stand-in for an :class:`EnvironmentModel` row.

    Only the fields ``ensure_production`` and ``_to_view`` read are populated;
    everything else defaults so the fixture stays readable.
    """

    id: str
    workspace_id: str
    tenant_id: str
    name: str
    branch: str = "main"
    env_type: str = naming.ENV_TYPE_PRODUCTION
    base_env: str = naming.PRODUCTION_ENV
    owner_user_id: str = "old-owner"
    status: str = naming.STATUS_ACTIVE
    deployment_name: str = ""
    schedules_enabled: int = 1
    last_applied_commit: t.Optional[str] = None
    last_applied_at: t.Optional[datetime] = None
    last_applied_dirty: int = 0
    last_promoted_to: t.Optional[str] = None
    ttl_hours: int = 0
    expires_at: t.Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: _NOW)
    updated_at: datetime = field(default_factory=lambda: _NOW)


class _FakeEnvironmentRepository:
    """Captures every write ``ensure_production`` may attempt."""

    def __init__(self, existing: t.Optional[_EnvRow] = None) -> None:
        self._current = existing
        self.created: t.List[_EnvRow] = []
        self.events: t.List[t.Dict[str, t.Any]] = []
        self.deleted_workspaces: t.List[str] = []

    async def get_by_name(self, workspace_id: str, name: str):
        row = self._current
        if row is not None and row.workspace_id == workspace_id and row.name == name:
            return row
        return None

    async def create(self, environment: _EnvRow) -> _EnvRow:
        self.created.append(environment)
        self._current = environment
        return environment

    async def delete_by_workspace(self, workspace_id: str) -> int:
        self.deleted_workspaces.append(workspace_id)
        removed = 1 if self._current is not None else 0
        self._current = None
        return removed

    async def record_event(
        self, environment_id: str, action: str, **kwargs: t.Any
    ) -> str:
        self.events.append(
            {"environment_id": environment_id, "action": action, **kwargs}
        )
        return "event-id"


def _workspace() -> WorkspaceModel:
    """Workspace stub carrying only the fields ``ensure_production`` reads."""
    return WorkspaceModel(
        id=_WS_ID,
        tenant_id="t1",
        owner_user_id="new-owner",
        name="admin-test",
        dialect="duckdb",
        git_repo_url=None,
        production_env=naming.PRODUCTION_ENV,
        default_env=naming.PRODUCTION_ENV,
        status="active",
    )


def _row(status: str, **overrides: t.Any) -> _EnvRow:
    return _EnvRow(
        id=_PROD_ID,
        workspace_id=_WS_ID,
        tenant_id="t1",
        name=naming.PRODUCTION_ENV,
        status=status,
        **overrides,
    )


def _build_registry(
    *, existing: t.Optional[_EnvRow] = None
) -> t.Tuple[EnvironmentRegistry, _FakeEnvironmentRepository]:
    """A registry with the binders stubbed and only the repo being the fake."""
    registry = EnvironmentRegistry.__new__(EnvironmentRegistry)
    fake_repo = _FakeEnvironmentRepository(existing=existing)
    registry._repo = fake_repo  # type: ignore[attr-defined]
    # The binders are constructed by ``__init__`` but ``ensure_production``
    # never touches them — leaving them absent makes that contract auditable:
    # any future call into one of them would raise ``AttributeError`` here.
    registry._git = None  # type: ignore[attr-defined]
    registry._sqlmesh = None  # type: ignore[attr-defined]
    registry._deployment = None  # type: ignore[attr-defined]
    registry._workspaces = None  # type: ignore[attr-defined]
    return registry, fake_repo


async def test_fresh_workspace_creates_a_new_production_row() -> None:
    """No row exists, so ``ensure_production`` inserts and audits."""
    registry, fake_repo = _build_registry()

    view = await registry.ensure_production(_workspace(), owner_user_id="new-owner")

    assert view.status == naming.STATUS_ACTIVE
    assert view.owner_user_id == "new-owner"
    assert view.is_production is True
    assert fake_repo.created
    created = fake_repo.created[0]
    assert created.owner_user_id == "new-owner"
    # The audit event carries the bootstrap tag so an operator can tell a
    # brand-new workspace apart from anything else.
    [event] = fake_repo.events
    assert event["action"] == naming.ACTION_CREATE
    assert event["detail"] == {
        "env_type": naming.ENV_TYPE_PRODUCTION,
        "bootstrap": True,
    }


async def test_recreated_workspace_name_starts_from_an_empty_registry() -> None:
    """The user-reported scenario, now trivial.

    Deleting the workspace removed its environment rows, so recreating the same
    name meets nothing and takes the ordinary insert path — no archived row to
    resurrect, and therefore no way to hand ``open()`` an unopenable status.
    """
    registry, fake_repo = _build_registry(
        existing=_row(naming.STATUS_ACTIVE, owner_user_id="previous-owner")
    )
    # What workspace deletion does to the registry.
    assert await fake_repo.delete_by_workspace(_WS_ID) == 1

    view = await registry.ensure_production(_workspace(), owner_user_id="new-owner")

    assert view.status == naming.STATUS_ACTIVE
    assert view.owner_user_id == "new-owner"
    # Nothing was inherited from the previous life of this id.
    assert view.last_applied_commit == ""
    assert view.last_applied_at is None
    assert view.last_promoted_to == ""
    assert fake_repo.created


async def test_active_production_row_is_returned_unchanged() -> None:
    """If the production row is already openable, no write happens."""
    existing = _row(
        naming.STATUS_ACTIVE,
        owner_user_id="previous-owner",
        last_applied_commit="cafebabe",
    )
    registry, fake_repo = _build_registry(existing=existing)

    view = await registry.ensure_production(_workspace(), owner_user_id="new-owner")

    assert view.status == naming.STATUS_ACTIVE
    assert view.owner_user_id == "previous-owner"
    assert view.last_applied_commit == "cafebabe"
    assert fake_repo.created == []
    assert fake_repo.events == []


@pytest.mark.parametrize(
    "openable_status",
    [naming.STATUS_APPLYING, naming.STATUS_PROMOTING, naming.STATUS_DRIFTED],
)
async def test_openable_in_flight_status_is_returned_unchanged(
    openable_status: str,
) -> None:
    """An apply or promotion in flight is live state; it must not be touched."""
    existing = _row(
        openable_status,
        owner_user_id="previous-owner",
        last_applied_commit="cafebabe",
    )
    registry, fake_repo = _build_registry(existing=existing)

    view = await registry.ensure_production(_workspace(), owner_user_id="new-owner")

    assert view.status == openable_status
    assert view.owner_user_id == "previous-owner"
    assert fake_repo.created == []
    assert fake_repo.events == []


@pytest.mark.parametrize(
    "stuck_status",
    [
        naming.STATUS_CREATING,
        naming.STATUS_ARCHIVING,
        naming.STATUS_ARCHIVED,
        naming.STATUS_FAILED,
    ],
)
async def test_non_openable_row_is_a_conflict_not_an_overwrite(
    stuck_status: str,
) -> None:
    """Any non-openable row means another operation owns it.

    Post-deletion such a row can only come from a create that is still running
    (or died mid-flight). Overwriting it would steal state another caller still
    owns, or resurrect a row whose downstream resources were never built — so
    the caller is told to wait instead.
    """
    existing = _row(stuck_status, owner_user_id="other-owner")
    registry, fake_repo = _build_registry(existing=existing)

    with pytest.raises(ConflictError, match=stuck_status):
        await registry.ensure_production(_workspace(), owner_user_id="new-owner")

    assert fake_repo.created == []
    assert fake_repo.events == []


async def test_ensure_production_never_deletes_rows_itself() -> None:
    """Creation must not remove registry rows: that is deletion's job alone.

    Guard against "make it work" repairs — a create that silently purged a row
    it did not understand would destroy an in-flight operation's state.
    """
    registry, fake_repo = _build_registry(
        existing=_row(naming.STATUS_CREATING, owner_user_id="other-owner")
    )

    with pytest.raises(ConflictError):
        await registry.ensure_production(_workspace(), owner_user_id="new-owner")

    assert fake_repo.deleted_workspaces == []
