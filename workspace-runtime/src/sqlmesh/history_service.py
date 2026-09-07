"""Apply history service — persists and queries plan apply audit records.

Provides save() for writing history after each apply and
list()/get_detail() for the History tab in the frontend PlanPanel.

Only **apply** operations are tracked (plan previews are read-only).

Attribution fields (user_id, session_id, trigger_type) are consumed from the
CLI wrapper's injected environment variables, passed through the /apply endpoint.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import desc, func, select

from src.api.models.responses import (
    ApplyHistoryDetail,
    ApplyHistoryListResponse,
    ApplyHistoryRecord,
    PlanResult,
)
from src.sqlmesh.event_bus import EventTopic, SSEEvent, get_event_bus
from src.utils.logging import get_logger
from src.workspace.database import get_session
from src.workspace.models import PlanApplyHistoryModel

logger = get_logger(__name__)


class PlanHistoryService:
    """Read/write service for plan apply history records."""

    async def save(
        self,
        *,
        workspace_path: str,
        environment: str,
        applied: bool,
        reason: Optional[str],
        has_changes: bool,
        plan_result: Optional[Dict[str, Any]],
        user_id: str = "",
        session_id: str = "",
        trigger_type: str = "human",
        duration_ms: int = 0,
        error_message: Optional[str] = None,
    ) -> str:
        """Persist a single apply history record and broadcast via SSE.

        Returns the record ID so callers can reference it (e.g. in SSE events).
        """
        record_id = uuid.uuid4().hex

        # Compute summary counts from plan_result for list-level display
        added_count = 0
        removed_count = 0
        direct_count = 0
        indirect_count = 0
        metadata_count = 0
        backfill_count = 0

        if plan_result:
            changes = plan_result.get("changes") or {}
            added_count = len(changes.get("added", []) or [])
            removed_count = len(changes.get("removed", []) or [])
            modified = changes.get("modified") or {}
            direct_count = len(modified.get("direct", []) or [])
            indirect_count = len(modified.get("indirect", []) or [])
            metadata_count = len(modified.get("metadata", []) or [])
            backfill_count = len(plan_result.get("backfills", []) or [])

        plan_result_json = json.dumps(plan_result, default=str) if plan_result else None

        async with get_session() as session:
            model = PlanApplyHistoryModel(
                id=record_id,
                workspace_path=workspace_path,
                environment=environment,
                user_id=user_id,
                session_id=session_id,
                trigger_type=trigger_type,
                applied=1 if applied else 0,
                reason=reason,
                has_changes=1 if has_changes else 0,
                added_count=added_count,
                removed_count=removed_count,
                direct_count=direct_count,
                indirect_count=indirect_count,
                metadata_count=metadata_count,
                backfill_count=backfill_count,
                plan_result_json=plan_result_json,
                duration_ms=duration_ms,
                error_message=error_message,
                created_at=datetime.now(timezone.utc),
            )
            session.add(model)

        logger.info(
            "apply_history_saved",
            record_id=record_id,
            workspace_path=workspace_path,
            environment=environment,
            applied=applied,
            trigger_type=trigger_type,
            user_id=user_id,
        )

        # Broadcast SSE event so the frontend can refresh the History tab
        bus = get_event_bus()
        await bus.publish(SSEEvent(
            topic=EventTopic.PLAN_APPLY,
            data={
                "stage": "history_updated",
                "status": "success",
                "record_id": record_id,
            },
            workspace_path=workspace_path,
        ))

        return record_id

    async def list(
        self,
        workspace_path: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> ApplyHistoryListResponse:
        """Return paginated apply history records for a workspace (newest first).

        Returns summary fields only; call get_detail() for the full plan_result.
        """
        async with get_session() as session:
            # Count total
            count_q = select(func.count(PlanApplyHistoryModel.id)).where(
                PlanApplyHistoryModel.workspace_path == workspace_path
            )
            count_result = await session.execute(count_q)
            total = count_result.scalar() or 0

            # Fetch page
            q = (
                select(PlanApplyHistoryModel)
                .where(PlanApplyHistoryModel.workspace_path == workspace_path)
                .order_by(desc(PlanApplyHistoryModel.created_at))
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(q)
            rows = result.scalars().all()

        records = [_history_model_to_record(m) for m in rows]
        logger.debug(
            "apply_history_listed",
            workspace_path=workspace_path,
            total=total,
            returned=len(records),
        )
        return ApplyHistoryListResponse(records=records, total=total)

    async def get_detail(self, record_id: str) -> Optional[ApplyHistoryDetail]:
        """Return a single history record with its full plan_result payload."""
        async with get_session() as session:
            q = select(PlanApplyHistoryModel).where(PlanApplyHistoryModel.id == record_id)
            result = await session.execute(q)
            model = result.scalar_one_or_none()

        if not model:
            logger.warning("apply_history_detail_not_found", record_id=record_id)
            return None

        detail = _history_model_to_detail(model)
        logger.debug("apply_history_detail_fetched", record_id=record_id)
        return detail


def _history_model_to_record(m: PlanApplyHistoryModel) -> ApplyHistoryRecord:
    """Map ORM model to list-level Pydantic response (no plan_result)."""
    return ApplyHistoryRecord(
        id=m.id,
        workspace_path=m.workspace_path,
        environment=m.environment,
        user_id=m.user_id,
        session_id=m.session_id,
        trigger_type=m.trigger_type,
        applied=bool(m.applied),
        reason=m.reason,
        has_changes=bool(m.has_changes),
        added_count=m.added_count,
        removed_count=m.removed_count,
        direct_count=m.direct_count,
        indirect_count=m.indirect_count,
        metadata_count=m.metadata_count,
        backfill_count=m.backfill_count,
        duration_ms=m.duration_ms,
        error_message=m.error_message,
        created_at=m.created_at.isoformat() if m.created_at else "",
    )


def _sanitize_sqlmesh_json(obj: Any) -> Any:
    """Recursively fix Python-repr artefacts in SQLMesh's JSON output.

    SQLMesh serialises empty sets as the string ``"set()"`` instead of ``[]``,
    which breaks Pydantic deserialisation of ``Set[str]`` fields.
    """
    if isinstance(obj, dict):
        return {k: _sanitize_sqlmesh_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_sqlmesh_json(item) for item in obj]
    if isinstance(obj, str) and obj.startswith("set(") and obj.endswith(")"):
        # "set()" → []
        return []
    return obj


def _history_model_to_detail(m: PlanApplyHistoryModel) -> ApplyHistoryDetail:
    """Map ORM model to detail Pydantic response (with full plan_result)."""
    plan_result: Optional[PlanResult] = None
    if m.plan_result_json:
        try:
            raw = json.loads(m.plan_result_json)
            sanitized = _sanitize_sqlmesh_json(raw)
            plan_result = PlanResult.model_validate(sanitized)
        except Exception:
            logger.warning(
                "apply_history_plan_result_parse_failed",
                record_id=m.id,
                exc_info=True,
            )

    return ApplyHistoryDetail(
        id=m.id,
        workspace_path=m.workspace_path,
        environment=m.environment,
        user_id=m.user_id,
        session_id=m.session_id,
        trigger_type=m.trigger_type,
        applied=bool(m.applied),
        reason=m.reason,
        has_changes=bool(m.has_changes),
        added_count=m.added_count,
        removed_count=m.removed_count,
        direct_count=m.direct_count,
        indirect_count=m.indirect_count,
        metadata_count=m.metadata_count,
        backfill_count=m.backfill_count,
        duration_ms=m.duration_ms,
        error_message=m.error_message,
        created_at=m.created_at.isoformat() if m.created_at else "",
        plan_result=plan_result,
    )


# Singleton
_history_service: Optional[PlanHistoryService] = None


def get_history_service() -> PlanHistoryService:
    global _history_service
    if _history_service is None:
        _history_service = PlanHistoryService()
    return _history_service
