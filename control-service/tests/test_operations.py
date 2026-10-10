"""``workspace_operations``: the recorder on real SQLite, the resumer's policy.

What must hold:

* the attempt counter is an ownership token — once a resumer claims an
  operation, the original (slow, not dead) process can no longer move it;
* two resumers never both claim one operation;
* an interrupted DELETE is always finished forward;
* an interrupted CREATE is rolled back, except when only the bookkeeping was
  lost (``succeeded``) — and it is NEVER rolled back when a newer operation on
  the same workspace exists, or when the row belongs to someone else: the
  workspace id is derived from the name, so "stalled create, then the user's
  successful retry" shares one id, and rolling back would delete the retry.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

import src.workspace.database as database
from src.config import settings
from src.operations import steps
from src.operations.models import WorkspaceOperationModel
from src.operations.recorder import (
    KIND_CREATE,
    KIND_DELETE,
    STATUS_ABANDONED,
    STATUS_FAILED,
    STATUS_ROLLED_BACK,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_SUPERSEDED,
    OperationRecorder,
)
from src.operations.resumer import OperationResumer

_WS = "ws-t1-shop"


@pytest.fixture()
async def recorder(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'ops.db'}"
    database.reset_engine()
    monkeypatch.setattr(database, "_build_database_url", lambda: url)
    await database.init_db()
    yield OperationRecorder()
    await database.close_db()


async def _age(op_id: str, *, seconds: float) -> None:
    """Pretend the operation's last progress was ``seconds`` ago."""
    past = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    async with database.get_session() as session:
        await session.execute(
            update(WorkspaceOperationModel)
            .where(WorkspaceOperationModel.op_id == op_id)
            .values(updated_at=past, created_at=past)
        )


async def _begin(recorder: OperationRecorder, kind: str = KIND_CREATE, ws: str = _WS):
    return await recorder.begin(
        kind=kind, workspace_id=ws, tenant_id="t1", actor_user_id="u1"
    )


# ── recorder ──────────────────────────────────────────────────────────────


async def test_steps_and_detail_accumulate(recorder):
    op = await _begin(recorder)
    await recorder.advance(op, steps.WRITING_ROW, final_step=steps.PUSHED)
    await recorder.advance(op, steps.ROW_WRITTEN)

    row = await recorder.get(op.op_id)
    assert row.step == steps.ROW_WRITTEN
    assert row.status == STATUS_RUNNING
    assert row.detail["final_step"] == steps.PUSHED  # merged, not replaced


async def test_terminal_status_is_final(recorder):
    op = await _begin(recorder)
    await recorder.succeed(op)
    await recorder.fail(op, error="late")

    assert (await recorder.get(op.op_id)).status == STATUS_SUCCEEDED


async def test_claim_revokes_the_original_handle(recorder):
    """A slow original must not overwrite what the resumer is doing."""
    op = await _begin(recorder)
    await _age(op.op_id, seconds=3600)
    [row] = await recorder.stuck(older_than_seconds=60)

    takeover = await recorder.claim(row)
    assert takeover is not None and takeover.attempt == 1

    await recorder.advance(op, steps.OPENED)  # the straggler
    await recorder.succeed(op)
    current = await recorder.get(op.op_id)
    assert current.step == steps.BEGUN
    assert current.status == STATUS_RUNNING

    await recorder.rolled_back(takeover)
    assert (await recorder.get(op.op_id)).status == STATUS_ROLLED_BACK


async def test_only_one_claim_wins(recorder):
    op = await _begin(recorder)
    await _age(op.op_id, seconds=3600)
    [row] = await recorder.stuck(older_than_seconds=60)

    assert await recorder.claim(row) is not None
    assert await recorder.claim(row) is None  # same snapshot, lost the CAS


async def test_stuck_ignores_fresh_and_terminal_operations(recorder):
    fresh = await _begin(recorder, ws="ws-a")
    done = await _begin(recorder, ws="ws-b")
    await recorder.succeed(done)
    await _age(done.op_id, seconds=3600)
    failed = await _begin(recorder, ws="ws-c")
    await recorder.fail(failed, error="boom")
    await _age(failed.op_id, seconds=3600)

    stuck = await recorder.stuck(older_than_seconds=60)

    assert [r.op_id for r in stuck] == [failed.op_id]
    assert fresh.op_id not in {r.op_id for r in stuck}


async def test_a_none_handle_is_a_no_op(recorder):
    """A recording outage degrades durability, never the operation."""
    await recorder.advance(None, steps.OPENED)
    await recorder.succeed(None)


# ── resumer ───────────────────────────────────────────────────────────────


@dataclass
class _Manager:
    rollback_complete: bool = True
    deletions: list = field(default_factory=list)
    rollbacks: list = field(default_factory=list)
    recorder: t.Optional[OperationRecorder] = None

    async def run_deletion(self, *, workspace_id, tenant_id, user_id, op):
        self.deletions.append(workspace_id)
        await self.recorder.advance(op, steps.ROWS_DELETED)
        await self.recorder.succeed(op)
        return {"deleted": True}

    async def rollback_creation(self, *, workspace_id, tenant_id, owner_user_id):
        self.rollbacks.append(workspace_id)
        return self.rollback_complete


@dataclass
class _Workspace:
    created_at: datetime


@dataclass
class _Workspaces:
    row: t.Optional[_Workspace] = None

    async def get_by_id(self, workspace_id):
        return self.row


@pytest.fixture()
def resumer(recorder, monkeypatch):
    monkeypatch.setattr(settings, "operation_resume_grace_seconds", 60)
    monkeypatch.setattr(settings, "operation_resume_max_attempts", 3)
    manager = _Manager(recorder=recorder)
    workspaces = _Workspaces()
    return (
        OperationResumer(recorder=recorder, manager=manager, workspaces=workspaces),
        manager,
        workspaces,
    )


async def _stalled(recorder, kind, *, step=None, final_step=None, ws=_WS):
    op = await _begin(recorder, kind=kind, ws=ws)
    if final_step:
        await recorder.advance(op, steps.WRITING_ROW, final_step=final_step)
    if step:
        await recorder.advance(op, step)
    await _age(op.op_id, seconds=3600)
    return op


async def test_detection_only_without_repair(recorder, resumer):
    res, manager, _ = resumer
    op = await _stalled(recorder, KIND_DELETE, step=steps.LEASES_RELEASED)

    outcome = await res.run_once(repair=False)

    assert outcome.stuck == 1
    assert manager.deletions == []
    assert (await recorder.get(op.op_id)).status == STATUS_RUNNING


async def test_interrupted_delete_is_finished_forward(recorder, resumer):
    res, manager, _ = resumer
    op = await _stalled(recorder, KIND_DELETE, step=steps.REPLICAS_PURGED)

    outcome = await res.run_once(repair=True)

    assert manager.deletions == [_WS]
    assert outcome.resumed == 1
    assert (await recorder.get(op.op_id)).status == STATUS_SUCCEEDED


async def test_create_that_lost_only_its_bookkeeping_succeeds(recorder, resumer):
    res, manager, workspaces = resumer
    workspaces.row = _Workspace(created_at=datetime.now(timezone.utc))
    op = await _stalled(
        recorder, KIND_CREATE, step=steps.PUSHED, final_step=steps.PUSHED
    )

    await res.run_once(repair=True)

    assert manager.rollbacks == []
    assert (await recorder.get(op.op_id)).status == STATUS_SUCCEEDED


async def test_scaffolded_but_unpushed_create_is_kept(recorder, resumer):
    """An unpushed initial commit is create's documented non-fatal outcome."""
    res, manager, _ = resumer
    op = await _stalled(
        recorder, KIND_CREATE, step=steps.SCAFFOLDED, final_step=steps.PUSHED
    )

    await res.run_once(repair=True)

    assert manager.rollbacks == []
    assert (await recorder.get(op.op_id)).status == STATUS_SUCCEEDED


async def test_half_built_create_is_rolled_back(recorder, resumer):
    res, manager, workspaces = resumer
    op = await _stalled(
        recorder, KIND_CREATE, step=steps.OPENED, final_step=steps.PUSHED
    )
    # Our own row: written after the operation began.
    workspaces.row = _Workspace(created_at=datetime.now(timezone.utc))

    await res.run_once(repair=True)

    assert manager.rollbacks == [_WS]
    assert (await recorder.get(op.op_id)).status == STATUS_ROLLED_BACK


async def test_incomplete_rollback_stays_resumable(recorder, resumer):
    res, manager, workspaces = resumer
    manager.rollback_complete = False
    workspaces.row = _Workspace(created_at=datetime.now(timezone.utc))
    op = await _stalled(
        recorder, KIND_CREATE, step=steps.OPENED, final_step=steps.PUSHED
    )

    outcome = await res.run_once(repair=True)

    assert outcome.failed == 1
    row = await recorder.get(op.op_id)
    assert row.status == STATUS_FAILED
    assert row.attempts == 1


async def test_create_never_rolls_back_a_row_it_did_not_write(recorder, resumer):
    """The row predates the operation: a concurrent creation won the key."""
    res, manager, workspaces = resumer
    workspaces.row = _Workspace(
        created_at=datetime.now(timezone.utc) - timedelta(days=1)
    )
    op = await _stalled(
        recorder, KIND_CREATE, step=steps.WRITING_ROW, final_step=steps.PUSHED
    )

    await res.run_once(repair=True)

    assert manager.rollbacks == []
    row = await recorder.get(op.op_id)
    assert row.status == STATUS_ROLLED_BACK
    assert row.detail["action"] == "nothing_to_undo"


async def test_create_that_died_before_the_row_undoes_nothing(recorder, resumer):
    res, manager, _ = resumer
    op = await _stalled(recorder, KIND_CREATE)  # step = begun

    await res.run_once(repair=True)

    assert manager.rollbacks == []
    assert (await recorder.get(op.op_id)).status == STATUS_ROLLED_BACK


async def test_a_newer_operation_supersedes_the_stalled_one(recorder, resumer):
    """Stalled create, then the user's successful retry: the retry must survive."""
    res, manager, workspaces = resumer
    stale = await _stalled(
        recorder, KIND_CREATE, step=steps.OPENED, final_step=steps.PUSHED
    )
    retry = await _begin(recorder)
    await recorder.succeed(retry)
    workspaces.row = _Workspace(created_at=datetime.now(timezone.utc))

    outcome = await res.run_once(repair=True)

    assert manager.rollbacks == []
    assert outcome.superseded == 1
    row = await recorder.get(stale.op_id)
    assert row.status == STATUS_SUPERSEDED
    assert row.detail["superseded_by"] == retry.op_id


async def test_attempts_are_bounded(recorder, resumer):
    res, manager, workspaces = resumer
    manager.rollback_complete = False
    workspaces.row = _Workspace(created_at=datetime.now(timezone.utc))
    op = await _stalled(
        recorder, KIND_CREATE, step=steps.OPENED, final_step=steps.PUSHED
    )

    for _ in range(3):
        await res.run_once(repair=True)
        await _age(op.op_id, seconds=3600)
    outcome = await res.run_once(repair=True)

    assert outcome.abandoned == 1
    assert (await recorder.get(op.op_id)).status == STATUS_ABANDONED
