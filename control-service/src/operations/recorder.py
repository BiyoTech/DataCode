"""Recorder for ``workspace_operations`` — the write side of operation progress.

Contract
--------
* :meth:`OperationRecorder.begin` writes a ``running`` row and returns an
  :class:`OperationHandle`. ``None`` (the row could not be written) turns every
  later call into a no-op, so a recording outage degrades durability, never the
  operation itself. That trade-off is logged at ERROR, because it means this
  operation is no longer resumable.
* Every write is a compare-and-swap on ``(status, attempts)``. ``attempts`` is
  the ownership token: :meth:`claim` bumps it when a resumer takes an
  operation over, after which a straggling original (a process that was slow,
  not dead) can no longer move the row — its handle names an attempt that is
  no longer current.
* :meth:`claim` is equally a CAS, so two control replicas never resume the
  same operation concurrently.

``updated_at`` is set explicitly on every write (Core statements bypass ORM
``onupdate``), and the resumer's staleness test must see exactly the time of
the last real progress.
"""

from __future__ import annotations

import json
import typing as t
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select, update

from src.operations.models import WorkspaceOperationModel
from src.utils.logging import get_logger
from src.workspace.database import get_session

logger = get_logger(__name__)

KIND_CREATE = "create"
KIND_DELETE = "delete"

STATUS_RUNNING = "running"
# The in-process path gave up mid-way and its own compensation did not finish.
STATUS_FAILED = "failed"
STATUS_SUCCEEDED = "succeeded"
STATUS_ROLLED_BACK = "rolled_back"
# A newer operation on the same workspace made this one moot.
STATUS_SUPERSEDED = "superseded"
# Resume attempts exhausted; needs a human.
STATUS_ABANDONED = "abandoned"

# Statuses the resumer may act on.
RESUMABLE_STATUSES = (STATUS_RUNNING, STATUS_FAILED)

_MAX_ERROR_CHARS = 2000


class OperationRecordError(RuntimeError):
    """A recorder READ failed (writes never raise; see the module docstring)."""


@dataclass(frozen=True)
class OperationHandle:
    """The right to advance one operation: its id and the attempt that owns it."""

    op_id: str
    attempt: int = 0


@dataclass(frozen=True)
class OperationRow:
    """Read model of one operation."""

    op_id: str
    kind: str
    workspace_id: str
    tenant_id: str
    actor_user_id: str
    step: str
    status: str
    attempts: int
    detail: t.Mapping[str, t.Any]
    error: str
    created_at: datetime
    updated_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """SQLite reads timezone-aware columns back naive; they were written as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _decode_detail(raw: t.Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _row(model: WorkspaceOperationModel) -> OperationRow:
    return OperationRow(
        op_id=model.op_id,
        kind=model.kind,
        workspace_id=model.workspace_id,
        tenant_id=model.tenant_id,
        actor_user_id=model.actor_user_id or "",
        step=model.step,
        status=model.status,
        attempts=int(model.attempts or 0),
        detail=_decode_detail(model.detail),
        error=model.error or "",
        created_at=_as_utc(model.created_at),
        updated_at=_as_utc(model.updated_at),
    )


class OperationRecorder:
    """Writes and reads ``workspace_operations``."""

    # ── writes (never raise) ───────────────────────────────────────────

    async def begin(
        self,
        *,
        kind: str,
        workspace_id: str,
        tenant_id: str,
        actor_user_id: str,
        detail: t.Optional[t.Mapping[str, t.Any]] = None,
    ) -> t.Optional[OperationHandle]:
        """Record a new running operation; ``None`` when it could not be written."""
        op_id = uuid.uuid4().hex
        now = _now()
        try:
            async with get_session() as session:
                await session.execute(
                    insert(WorkspaceOperationModel).values(
                        op_id=op_id,
                        kind=kind,
                        workspace_id=workspace_id,
                        tenant_id=tenant_id,
                        actor_user_id=actor_user_id,
                        step="begun",
                        status=STATUS_RUNNING,
                        attempts=0,
                        detail=json.dumps(dict(detail or {})),
                        created_at=now,
                        updated_at=now,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - recording must not block the operation
            logger.error(
                "workspace_operation_record_failed",
                kind=kind,
                workspace_id=workspace_id,
                error=str(exc),
                effect="this operation is not resumable if the process dies mid-way",
            )
            return None
        logger.info(
            "workspace_operation_begun", op_id=op_id, kind=kind, workspace_id=workspace_id
        )
        return OperationHandle(op_id=op_id, attempt=0)

    async def advance(
        self, handle: t.Optional[OperationHandle], step: str, **detail: t.Any
    ) -> None:
        """Record that ``step``'s effect has been achieved."""
        await self._transition(
            handle,
            values={"step": step},
            detail=detail,
            event="workspace_operation_advanced",
        )

    async def succeed(self, handle: t.Optional[OperationHandle], **detail: t.Any) -> None:
        await self._transition(
            handle,
            values={"status": STATUS_SUCCEEDED},
            detail=detail,
            event="workspace_operation_succeeded",
        )

    async def rolled_back(
        self, handle: t.Optional[OperationHandle], **detail: t.Any
    ) -> None:
        await self._transition(
            handle,
            values={"status": STATUS_ROLLED_BACK},
            detail=detail,
            event="workspace_operation_rolled_back",
        )

    async def fail(self, handle: t.Optional[OperationHandle], *, error: str) -> None:
        """The in-process path gave up; the resumer takes it from here."""
        await self._transition(
            handle,
            values={"status": STATUS_FAILED, "error": error[:_MAX_ERROR_CHARS]},
            detail={},
            event="workspace_operation_failed",
        )

    async def supersede(self, handle: t.Optional[OperationHandle], *, by: str) -> None:
        await self._transition(
            handle,
            values={"status": STATUS_SUPERSEDED},
            detail={"superseded_by": by},
            event="workspace_operation_superseded",
        )

    async def abandon(self, handle: t.Optional[OperationHandle], *, error: str) -> None:
        await self._transition(
            handle,
            values={"status": STATUS_ABANDONED, "error": error[:_MAX_ERROR_CHARS]},
            detail={},
            event="workspace_operation_abandoned",
        )

    async def claim(self, row: OperationRow) -> t.Optional[OperationHandle]:
        """Take ``row`` over for one resume attempt; ``None`` if someone else did.

        Bumping ``attempts`` both elects exactly one resumer and revokes the
        previous owner's handle. Refreshing ``updated_at`` restarts the
        staleness clock, so a resumer that dies mid-attempt is retried only
        after another grace period.
        """
        try:
            async with get_session() as session:
                result = await session.execute(
                    update(WorkspaceOperationModel)
                    .where(
                        WorkspaceOperationModel.op_id == row.op_id,
                        WorkspaceOperationModel.attempts == row.attempts,
                        WorkspaceOperationModel.status.in_(RESUMABLE_STATUSES),
                    )
                    .values(
                        attempts=row.attempts + 1,
                        status=STATUS_RUNNING,
                        updated_at=_now(),
                    )
                )
                claimed = (result.rowcount or 0) > 0
        except Exception as exc:  # noqa: BLE001 - an unclaimed op is retried next tick
            logger.warning(
                "workspace_operation_claim_failed", op_id=row.op_id, error=str(exc)
            )
            return None
        if not claimed:
            return None
        logger.info(
            "workspace_operation_claimed", op_id=row.op_id, attempt=row.attempts + 1
        )
        return OperationHandle(op_id=row.op_id, attempt=row.attempts + 1)

    # ── reads (raise OperationRecordError) ─────────────────────────────

    async def get(self, op_id: str) -> t.Optional[OperationRow]:
        try:
            async with get_session() as session:
                model = await session.get(WorkspaceOperationModel, op_id)
        except Exception as exc:  # noqa: BLE001
            raise OperationRecordError(str(exc)) from exc
        return _row(model) if model is not None else None

    async def stuck(
        self, *, older_than_seconds: float, limit: int = 100
    ) -> t.List[OperationRow]:
        """Non-terminal operations with no progress for ``older_than_seconds``.

        Oldest first, so a backlog drains in the order it was created.
        """
        cutoff = _now() - timedelta(seconds=older_than_seconds)
        try:
            async with get_session() as session:
                models = (
                    (
                        await session.execute(
                            select(WorkspaceOperationModel)
                            .where(
                                WorkspaceOperationModel.status.in_(RESUMABLE_STATUSES),
                                WorkspaceOperationModel.updated_at < cutoff,
                            )
                            .order_by(WorkspaceOperationModel.updated_at.asc())
                            .limit(max(1, limit))
                        )
                    )
                    .scalars()
                    .all()
                )
        except Exception as exc:  # noqa: BLE001
            raise OperationRecordError(str(exc)) from exc
        return [_row(model) for model in models]

    async def newer_than(self, row: OperationRow) -> t.Optional[OperationRow]:
        """The most recent operation on the same workspace begun after ``row``."""
        try:
            async with get_session() as session:
                model = (
                    await session.execute(
                        select(WorkspaceOperationModel)
                        .where(
                            WorkspaceOperationModel.workspace_id == row.workspace_id,
                            WorkspaceOperationModel.created_at > row.created_at,
                            WorkspaceOperationModel.op_id != row.op_id,
                        )
                        .order_by(WorkspaceOperationModel.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001
            raise OperationRecordError(str(exc)) from exc
        return _row(model) if model is not None else None

    # ── internals ──────────────────────────────────────────────────────

    async def _transition(
        self,
        handle: t.Optional[OperationHandle],
        *,
        values: t.Mapping[str, t.Any],
        detail: t.Mapping[str, t.Any],
        event: str,
    ) -> None:
        """CAS one write on ``(status in RESUMABLE, attempts == handle.attempt)``."""
        if handle is None:
            return
        try:
            async with get_session() as session:
                model = await session.get(WorkspaceOperationModel, handle.op_id)
                if (
                    model is None
                    or model.status not in RESUMABLE_STATUSES
                    or int(model.attempts or 0) != handle.attempt
                ):
                    logger.warning(
                        "workspace_operation_transition_skipped",
                        op_id=handle.op_id,
                        attempt=handle.attempt,
                        current_status=getattr(model, "status", None),
                        current_attempt=getattr(model, "attempts", None),
                        attempted=event,
                    )
                    return
                merged = _decode_detail(model.detail)
                merged.update(detail)
                result = await session.execute(
                    update(WorkspaceOperationModel)
                    .where(
                        WorkspaceOperationModel.op_id == handle.op_id,
                        WorkspaceOperationModel.attempts == handle.attempt,
                        WorkspaceOperationModel.status.in_(RESUMABLE_STATUSES),
                    )
                    .values(**values, detail=json.dumps(merged), updated_at=_now())
                )
                if not result.rowcount:
                    logger.warning(
                        "workspace_operation_transition_lost",
                        op_id=handle.op_id,
                        attempted=event,
                    )
                    return
        except Exception as exc:  # noqa: BLE001 - recording must not break the operation
            logger.error(
                "workspace_operation_record_failed",
                op_id=handle.op_id,
                attempted=event,
                error=str(exc),
            )
            return
        logger.info(
            event,
            op_id=handle.op_id,
            attempt=handle.attempt,
            **{key: value for key, value in values.items() if key != "error"},
        )


_recorder: t.Optional[OperationRecorder] = None


def get_operation_recorder() -> OperationRecorder:
    """Get or create the global :class:`OperationRecorder` (stateless)."""
    global _recorder
    if _recorder is None:
        _recorder = OperationRecorder()
    return _recorder


def set_operation_recorder(recorder: t.Optional[OperationRecorder]) -> None:
    """Replace the global recorder (tests)."""
    global _recorder
    _recorder = recorder
