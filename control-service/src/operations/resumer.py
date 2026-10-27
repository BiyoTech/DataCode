"""Resumer for interrupted workspace lifecycle operations.

Run by the reconcile sweep. Picks up ``workspace_operations`` rows that are
non-terminal and have made no progress for a grace period — the trace a
control replica leaves when it is killed mid-create or mid-delete — and drives
each to a terminal state.

Policy per kind
---------------
**create** — never finished forward, except where only the bookkeeping was
lost. Finishing forward would need inputs the record deliberately does not keep
(remote URLs may embed tokens), and an interrupted creation has already failed
from the user's point of view: their request errored or timed out. So:

* the last achieved step is the planned final step → ``succeeded`` (the
  process died between its last effect and recording success);
* scaffolded but not yet pushed → ``succeeded``: an unpushed initial commit is
  the documented non-fatal outcome of create (members cannot clone until the
  owner publishes), and the workspace is fully usable by its owner;
* anything earlier → the in-process rollback, re-run; ``rolled_back`` when it
  completes, retried next tick when it does not.

**delete** — always finished forward. The user asked for the workspace to be
gone and every deletion step is idempotent, so re-running the sequence from
the top converges.

Never acting on someone else's workspace
----------------------------------------
A workspace id is derived from ``{tenant}/{name}``, so a stalled operation and
a LATER one can share it (create failed → user retried; delete stalled → user
recreated). Before acting, the resumer looks for a newer operation on the same
workspace; if one exists, the stale one is marked ``superseded`` and nothing is
touched. Rolling back a stalled create after the user's successful retry would
delete their workspace.

Exclusivity
-----------
Every control replica runs the sweep. :meth:`OperationRecorder.claim` is a CAS
on the attempt counter, so exactly one replica resumes a given operation, and
the claim revokes the original process's handle in case it was slow rather than
dead.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field

from src.config import settings
from src.operations import steps
from src.operations.recorder import (
    KIND_CREATE,
    KIND_DELETE,
    OperationRecorder,
    OperationRecordError,
    OperationRow,
    get_operation_recorder,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Actor recorded for resumer-driven work, so the audit trail tells it apart
# from the user's original request.
RESUMER_ACTOR = "system_operation_resumer"


@dataclass
class ResumeOutcome:
    """What one resume pass found and did."""

    stuck: int = 0
    resumed: int = 0
    superseded: int = 0
    abandoned: int = 0
    failed: int = 0
    skipped_claimed: int = 0
    # op_id -> terminal status reached this pass.
    results: t.Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> t.Dict[str, t.Any]:
        return {
            "operations_stuck": self.stuck,
            "operations_resumed": self.resumed,
            "operations_superseded": self.superseded,
            "operations_abandoned": self.abandoned,
            "operations_resume_failed": self.failed,
            "operations_skipped_claimed": self.skipped_claimed,
        }


class OperationResumer:
    """Drives stalled lifecycle operations to a terminal state."""

    def __init__(
        self,
        *,
        recorder: t.Optional[OperationRecorder] = None,
        manager: t.Optional[t.Any] = None,
        workspaces: t.Optional[t.Any] = None,
    ) -> None:
        self._recorder = recorder
        self._manager = manager
        self._workspaces = workspaces

    @property
    def recorder(self) -> OperationRecorder:
        return self._recorder or get_operation_recorder()

    @property
    def workspaces(self) -> t.Any:
        if self._workspaces is None:
            from src.workspace.repository import WorkspaceRepository

            self._workspaces = WorkspaceRepository()
        return self._workspaces

    @property
    def manager(self) -> t.Any:
        if self._manager is None:
            from src.workspace.manager import get_workspace_manager

            self._manager = get_workspace_manager()
        return self._manager

    async def run_once(self, *, repair: bool) -> ResumeOutcome:
        """One pass. ``repair=False`` only reports what would be resumed."""
        outcome = ResumeOutcome()
        try:
            rows = await self.recorder.stuck(
                older_than_seconds=float(settings.operation_resume_grace_seconds),
                limit=int(settings.operation_resume_batch),
            )
        except OperationRecordError as exc:
            logger.error("operation_resume_scan_failed", error=str(exc))
            return outcome

        outcome.stuck = len(rows)
        for row in rows:
            logger.warning(
                "workspace_operation_stuck",
                op_id=row.op_id,
                kind=row.kind,
                workspace_id=row.workspace_id,
                step=row.step,
                status=row.status,
                attempts=row.attempts,
                repair=repair,
            )
            if not repair:
                continue
            try:
                status = await self._resume_one(row)
            except Exception as exc:  # noqa: BLE001 - one operation must not stop the pass
                outcome.failed += 1
                logger.error(
                    "workspace_operation_resume_failed",
                    op_id=row.op_id,
                    kind=row.kind,
                    workspace_id=row.workspace_id,
                    error=str(exc),
                )
                continue
            if status is None:
                outcome.skipped_claimed += 1
                continue
            outcome.results[row.op_id] = status
            if status == "superseded":
                outcome.superseded += 1
            elif status == "abandoned":
                outcome.abandoned += 1
            else:
                outcome.resumed += 1
        return outcome

    # ── one operation ──────────────────────────────────────────────────

    async def _resume_one(self, row: OperationRow) -> t.Optional[str]:
        """Resume ``row``; the terminal status reached, ``None`` if not claimed.

        Raises when the attempt itself failed: the row stays resumable (its
        ``updated_at`` was refreshed by the claim) and is retried after another
        grace period.
        """
        handle = await self.recorder.claim(row)
        if handle is None:
            # Another replica took it between our scan and our claim.
            return None

        newer = await self.recorder.newer_than(row)
        if newer is not None:
            await self.recorder.supersede(handle, by=newer.op_id)
            return "superseded"

        if handle.attempt > int(settings.operation_resume_max_attempts):
            await self.recorder.abandon(
                handle,
                error=f"gave up after {handle.attempt - 1} resume attempts; "
                "needs manual cleanup",
            )
            return "abandoned"

        logger.info(
            "operation_resumed",
            op_id=row.op_id,
            kind=row.kind,
            workspace_id=row.workspace_id,
            step=row.step,
            attempt=handle.attempt,
        )

        if row.kind == KIND_DELETE:
            await self.manager.run_deletion(
                workspace_id=row.workspace_id,
                tenant_id=row.tenant_id,
                user_id=row.actor_user_id or RESUMER_ACTOR,
                op=handle,
            )
            return "succeeded"

        if row.kind == KIND_CREATE:
            if _create_effectively_finished(row):
                await self.recorder.succeed(handle, resumed_from=row.step)
                return "succeeded"
            if not await self._create_owns_row(row):
                # Nothing of this operation's is on the platform: it died
                # before writing the row, or the row belongs to a creation
                # that started EARLIER (a concurrent request that won the
                # key). Rolling back would delete someone else's workspace.
                await self.recorder.rolled_back(
                    handle, resumed_from=row.step, action="nothing_to_undo"
                )
                return "rolled_back"
            complete = await self.manager.rollback_creation(
                workspace_id=row.workspace_id,
                tenant_id=row.tenant_id,
                owner_user_id=row.actor_user_id,
            )
            if not complete:
                await self.recorder.fail(handle, error="resumed rollback incomplete")
                raise RuntimeError("rollback incomplete; retried next pass")
            await self.recorder.rolled_back(handle, resumed_from=row.step)
            return "rolled_back"

        await self.recorder.abandon(handle, error=f"unknown operation kind {row.kind!r}")
        return "abandoned"

    async def _create_owns_row(self, row: OperationRow) -> bool:
        """Whether the workspace row (if any) was written by THIS creation.

        Before ``writing_row`` the operation wrote nothing. After it, the row
        is ours only if it is not older than the operation — a row created
        before the operation began belongs to someone else. A MISSING row
        still counts as ours from ``row_written`` on: the material on the
        runtimes may outlive a row that a partial rollback already deleted.
        """
        if row.step == steps.BEGUN:
            return False
        workspace = await self.workspaces.get_by_id(row.workspace_id)
        if workspace is None:
            return row.step != steps.WRITING_ROW
        created_at = getattr(workspace, "created_at", None)
        if created_at is None:
            return True
        if created_at.tzinfo is None:
            from datetime import timezone

            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at >= row.created_at


def _create_effectively_finished(row: OperationRow) -> bool:
    """Whether a stalled create already produced a usable workspace."""
    final = str(row.detail.get("final_step") or "")
    if final and row.step == final:
        return True
    # Scaffolded, push pending: the documented non-fatal outcome of create.
    return row.step == steps.SCAFFOLDED and final == steps.PUSHED


_resumer: t.Optional[OperationResumer] = None


def get_operation_resumer() -> OperationResumer:
    """Get or create the global :class:`OperationResumer`."""
    global _resumer
    if _resumer is None:
        _resumer = OperationResumer()
    return _resumer
