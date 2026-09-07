"""Knowledge document METADATA endpoints (design 06 P8 — route repatriation).

The knowledge surface split in two, along the line P5 drew for everything
else: ``list`` is pure metadata over the shared database, so it belongs to the
control plane (plain load-balanced, no lease resolution); create / upload /
read / delete touch the working copy's files, so they stay on workspace-runtime
behind the lease router.

The ``knowledge_documents`` table is OWNED by workspace-runtime (its
``create_all`` creates it, per the P5 ownership note in the workspace models).
Reading it here therefore uses a Core ``Table`` — the same one-definition rule
``task_records`` and ``lease_history`` already follow: the owner holds the
authoritative declaration, every other service mirrors it deliberately, and a
column change on one side surfaces as a loud review comment on the other.

The response shape is byte-compatible with the runtime endpoint this replaces:
the frontend store must not learn which service answered.
"""

from __future__ import annotations

import typing as t

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, func, or_, select

from src.api.deps import Caller
from src.config import settings
from src.utils.errors import WorkspacePathInvalidError
from src.utils.logging import get_logger
from src.workspace.database import get_session

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])

# Mirror of workspace-runtime's KnowledgeDocumentModel — see the module
# docstring for why this is a Core table rather than an ORM model here.
_metadata = MetaData()

knowledge_documents = Table(
    "knowledge_documents",
    _metadata,
    Column("id", String(64), primary_key=True),
    Column("workspace_path", String(512), nullable=False),
    Column("title", String(256), nullable=False),
    Column("filename", String(512), nullable=False),
    Column("content_type", String(32), nullable=False),
    Column("file_size", Integer(), nullable=False),
    Column("tags", String(512), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class KnowledgeDocumentResponse(BaseModel):
    """Single knowledge document metadata (content returned separately)."""

    id: str
    workspace_path: str
    title: str
    filename: str
    content_type: str = "markdown"
    file_size: int = 0
    tags: t.Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


class KnowledgeListResponse(BaseModel):
    """Knowledge document listing (same shape the runtime endpoint served)."""

    documents: t.List[KnowledgeDocumentResponse] = []
    total: int = 0


def _validated_workspace_path(workspace_path: str) -> str:
    """Shape-check the working-copy path without touching any filesystem.

    The gateway already authorized the caller for this workspace (the route's
    permission is workspace-path scoped); this check adds the same layout
    defence the runtime applies, so a malformed path is a 400 rather than an
    empty (and misleading) result set.
    """
    from datacode_common.workspace_paths import (
        WorkspacePathLayoutError,
        parse_workspace_path,
    )

    try:
        parse_workspace_path(settings.workspace_base_path, workspace_path)
    except (WorkspacePathLayoutError, ValueError) as exc:
        raise WorkspacePathInvalidError(workspace_path, reason=str(exc)) from exc
    return workspace_path


@router.get("/documents/list", response_model=KnowledgeListResponse)
async def list_documents(
    caller: Caller,
    workspace_path: str = Query(..., description="Working-copy path to list."),
    search: str = "",
    limit: int = 100,
):
    """List a workspace's knowledge documents, with optional title/tag search."""
    workspace_path = _validated_workspace_path(workspace_path)
    if limit < 1 or limit > 500:
        limit = 100

    async with get_session() as session:
        q = select(knowledge_documents).where(
            knowledge_documents.c.workspace_path == workspace_path
        )
        if search:
            pattern = f"%{search}%"
            q = q.where(
                or_(
                    knowledge_documents.c.title.ilike(pattern),
                    knowledge_documents.c.tags.ilike(pattern),
                )
            )

        total = (
            await session.execute(select(func.count()).select_from(q.subquery()))
        ).scalar() or 0

        rows = (
            (
                await session.execute(
                    q.order_by(knowledge_documents.c.created_at.desc()).limit(limit)
                )
            )
            .mappings()
            .all()
        )

    documents = [
        KnowledgeDocumentResponse(
            id=row["id"],
            workspace_path=row["workspace_path"],
            title=row["title"],
            filename=row["filename"],
            content_type=row["content_type"],
            file_size=row["file_size"],
            tags=row["tags"],
            created_at=row["created_at"].isoformat() if row["created_at"] else "",
            updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
        )
        for row in rows
    ]
    logger.info(
        "knowledge_listed",
        workspace=workspace_path,
        tenant_id=caller.tenant_id,
        total=total,
        search=search,
    )
    return KnowledgeListResponse(documents=documents, total=total)
