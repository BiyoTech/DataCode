"""Knowledge document management endpoints.

Handles CRUD for business-knowledge documents stored under
{workspace}/knowledge/. Metadata is persisted in MySQL; file content
lives on the local filesystem via file_ops for safe path handling.

Documents are stored on disk using a human-readable filename derived from
their title (no opaque UUIDs), so global agents can locate a document by
name. The filename doubles as the document identity (primary key).
"""

import os
import re

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from sqlalchemy import select, func, or_

from src.api.deps import WorkspacePath, validate_workspace_path

from src.api.models.requests import (
    CreateKnowledgeRequest,
    DeleteKnowledgeRequest,
)
from src.api.models.responses import (
    KnowledgeContentResponse,
    KnowledgeDocumentResponse,
    KnowledgeListResponse,
)
from src.utils.logging import get_logger
from src.workspace.database import get_session
from src.workspace.file_ops import delete_file as _delete_file, read_file as _read_file, write_file as _write_file
from src.workspace.models import KnowledgeDocumentModel

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])

# Directory under each workspace where knowledge docs are stored
KNOWLEDGE_DIR = "knowledge"

# Allowed upload extensions (whitelist)
ALLOWED_EXTENSIONS = {".md", ".txt", ".pdf", ".csv", ".json", ".yaml", ".yml"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


# ─────────── helpers ───────────

def _infer_content_type(ext: str) -> str:
    """Map file extension to a human-readable content type label."""
    mapping = {
        ".md": "markdown",
        ".txt": "text",
        ".pdf": "pdf",
        ".csv": "csv",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
    }
    return mapping.get(ext.lower(), "unknown")


def _model_to_response(doc: KnowledgeDocumentModel) -> KnowledgeDocumentResponse:
    """Convert ORM model to Pydantic response."""
    return KnowledgeDocumentResponse(
        id=doc.id,
        workspace_path=doc.workspace_path,
        title=doc.title,
        filename=doc.filename,
        content_type=doc.content_type,
        file_size=doc.file_size,
        tags=doc.tags,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
        updated_at=doc.updated_at.isoformat() if doc.updated_at else "",
    )


# Max length for the derived filename base, bounded by the DB id column (64).
MAX_SLUG_LEN = 55


def _slugify(name: str) -> str:
    """Turn a human title/filename into a filesystem-safe, predictable base name.

    Keeps unicode word chars (letters/digits/underscore, incl. CJK), converts
    whitespace runs to a single '_', and strips path separators / unsafe
    characters. This makes the result safe to use directly as a filename
    (prevents path traversal) while staying human-readable for agent search.
    """
    # Drop any directory components to prevent path traversal (../, absolute).
    name = name.replace("\\", "/").split("/")[-1]
    # Collapse whitespace runs into a single underscore.
    name = re.sub(r"\s+", "_", name.strip())
    # Keep only word chars, hyphen and dot; drop everything else.
    name = re.sub(r"[^\w\-.]", "", name, flags=re.UNICODE)
    # Avoid leading dots (hidden files) and guarantee a non-empty result.
    name = name.lstrip(".") or "untitled"
    return name[:MAX_SLUG_LEN]


def _rel_path(filename: str) -> str:
    """Build the relative file path within workspace (knowledge dir + filename)."""
    return f"{KNOWLEDGE_DIR}/{filename}"


async def _ensure_unique_id(session, workspace_path: str, doc_id: str) -> None:
    """Reject creation if a document with the same filename already exists.

    Since the filename is derived from the title and used as the identity,
    a duplicate name would overwrite an existing file — so we fail fast.
    """
    q = select(KnowledgeDocumentModel).where(
        KnowledgeDocumentModel.id == doc_id,
        KnowledgeDocumentModel.workspace_path == workspace_path,
    )
    if (await session.execute(q)).scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"A knowledge document named '{doc_id}' already exists",
        )


def _validate_upload(filename: str, size: int):
    """Validate uploaded file extension and size. Raises HTTPException on failure."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size} bytes). Maximum: {MAX_UPLOAD_BYTES} bytes",
        )


# ─────────── endpoints ───────────

@router.get("/documents/list", response_model=KnowledgeListResponse)
async def list_documents(
    workspace_path: WorkspacePath,
    search: str = "",
    limit: int = 100,
):
    """List knowledge documents in a workspace, with optional title search."""
    if limit < 1 or limit > 500:
        limit = 100

    async with get_session() as session:
        q = select(KnowledgeDocumentModel).where(
            KnowledgeDocumentModel.workspace_path == workspace_path
        )

        if search:
            pattern = f"%{search}%"
            q = q.where(
                or_(
                    KnowledgeDocumentModel.title.ilike(pattern),
                    KnowledgeDocumentModel.tags.ilike(pattern),
                )
            )

        # Get total count
        count_q = select(func.count()).select_from(q.subquery())
        total = (await session.execute(count_q)).scalar() or 0

        # Fetch ordered page
        q = q.order_by(KnowledgeDocumentModel.created_at.desc()).limit(limit)
        rows = (await session.execute(q)).scalars().all()

    documents = [_model_to_response(r) for r in rows]
    logger.info("knowledge_listed", workspace=workspace_path, total=total, search=search)
    return KnowledgeListResponse(documents=documents, total=total)


@router.post("/documents/create", response_model=KnowledgeDocumentResponse)
async def create_document(req: CreateKnowledgeRequest):
    """Create a new Markdown knowledge document.

    Derives a human-readable filename from the title, writes the content to
    disk under {workspace}/knowledge/, and inserts metadata into the database.
    The filename doubles as the document id so agents can find it by name.
    """
    # Derive a predictable, human-readable filename from the title.
    filename = f"{_slugify(req.title)}.md"
    doc_id = filename  # filename is the document identity
    rel_path = _rel_path(filename)
    file_size = len(req.content.encode("utf-8"))

    async with get_session() as session:
        # Fail fast on name collision to avoid overwriting an existing file.
        await _ensure_unique_id(session, req.workspace_path, doc_id)

        # Write file via safe file_ops (auto-creates parent dirs).
        try:
            _write_file(req.workspace_path, rel_path, req.content)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        model = KnowledgeDocumentModel(
            id=doc_id,
            workspace_path=req.workspace_path,
            title=req.title,
            filename=filename,
            content_type="markdown",
            file_size=file_size,
            tags=req.tags,
        )
        session.add(model)
        await session.commit()
        await session.refresh(model)

    logger.info(
        "knowledge_created",
        doc_id=doc_id,
        workspace=req.workspace_path,
        title=req.title,
        size=file_size,
    )
    return _model_to_response(model)


@router.post("/documents/upload", response_model=KnowledgeDocumentResponse)
async def upload_document(
    workspace_path: str = Form(...),
    file: UploadFile = File(...),
):
    """Upload a business-knowledge file."""
    workspace_path = validate_workspace_path(workspace_path)

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    # Read file content (streaming not necessary for ≤10 MB files)
    content_bytes = await file.read()

    _validate_upload(file.filename, len(content_bytes))

    # Keep the original (sanitized) filename so agents can search by name.
    ext = os.path.splitext(file.filename)[1].lower()
    title = os.path.splitext(file.filename)[0]  # strip extension for title
    filename = f"{_slugify(title)}{ext}"
    doc_id = filename  # filename is the document identity
    rel_path = _rel_path(filename)
    content_type = _infer_content_type(ext)

    # Decode text files to string for file_ops; binary files (PDF) go as-is
    # but for now we write everything as binary-safe
    try:
        content_str = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Binary file — store as latin-1 encoded string (file_ops uses text mode)
        content_str = content_bytes.decode("latin-1")

    async with get_session() as session:
        # Fail fast on name collision to avoid overwriting an existing file.
        await _ensure_unique_id(session, workspace_path, doc_id)

        try:
            _write_file(workspace_path, rel_path, content_str)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        model = KnowledgeDocumentModel(
            id=doc_id,
            workspace_path=workspace_path,
            title=title,
            filename=filename,
            content_type=content_type,
            file_size=len(content_bytes),
            tags=None,
        )
        session.add(model)
        await session.commit()
        await session.refresh(model)

    logger.info(
        "knowledge_uploaded",
        doc_id=doc_id,
        workspace=workspace_path,
        original_name=file.filename,
        content_type=content_type,
        size=len(content_bytes),
    )
    return _model_to_response(model)


@router.get("/documents/read", response_model=KnowledgeContentResponse)
async def read_document(
    workspace_path: WorkspacePath,
    document_id: str,
):
    """Read the full content of a knowledge document from disk."""
    # Fetch metadata from DB
    async with get_session() as session:
        q = select(KnowledgeDocumentModel).where(
            KnowledgeDocumentModel.id == document_id,
            KnowledgeDocumentModel.workspace_path == workspace_path,
        )
        result = (await session.execute(q)).scalar_one_or_none()

    if not result:
        raise HTTPException(status_code=404, detail="Document not found")

    rel_path = _rel_path(result.filename)
    content = _read_file(workspace_path, rel_path)

    if content is None:
        raise HTTPException(status_code=404, detail="Document file not found on disk")

    return KnowledgeContentResponse(
        id=result.id,
        title=result.title,
        content_type=result.content_type,
        content=content,
        size=len(content.encode("utf-8")),
    )


@router.post("/documents/delete")
async def delete_document(req: DeleteKnowledgeRequest):
    """Delete a knowledge document (metadata from DB + file from disk)."""
    # Fetch metadata first
    async with get_session() as session:
        q = select(KnowledgeDocumentModel).where(
            KnowledgeDocumentModel.id == req.document_id,
            KnowledgeDocumentModel.workspace_path == req.workspace_path,
        )
        doc = (await session.execute(q)).scalar_one_or_none()

        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

        # Delete the file from disk
        rel_path = _rel_path(doc.filename)
        _delete_file(req.workspace_path, rel_path)

        # Delete the metadata row
        await session.delete(doc)
        await session.commit()

    logger.info(
        "knowledge_deleted",
        doc_id=req.document_id,
        workspace=req.workspace_path,
        title=doc.title,
    )
    return {"success": True, "document_id": req.document_id}
