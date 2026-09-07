"""Database layer — SQLAlchemy async engine + table definitions.

Tables:
  - sessions: Chat session metadata (user, workspace, mode, timestamps)
  - messages: LLM conversation context (role + content, feeds into model input)
  - timeline_events: Frontend rendering events (tool calls, thoughts, artifacts, etc.)
  - memory_events: L1 memory event index (key events + timestamps)
  - memory_summaries: L2 memory summaries (structured descriptions of key events)
"""

import os
import typing as t
from datetime import datetime, timezone

from datacode_common.database.engine import create_async_engine_for_url
from datacode_common.database.url import safe_url
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy import (
    inspect as sqla_inspect,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


# ============================================================
# SQLAlchemy Base
# ============================================================

class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


# ============================================================
# Table Definitions
# ============================================================

class SessionRow(Base):
    """Persistent chat session record.

    Note: workspace_path and environment are NOT stored here. They are runtime
    parameters passed per chat message and injected into agent execution context.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    mode: Mapped[str] = mapped_column(String(16), default="ask")
    # Execution style: 'agent' = autonomous (default) | 'interactive' = step-by-step.
    # Orthogonal to `mode`: mode = capability/permissions, style = autonomy.
    execution_style: Mapped[str] = mapped_column(String(16), default="agent")
    # Session scenario: 'dev' = development assistant | 'chatbi' = ChatBI analyst.
    # Drives entry-agent selection and session-list filtering.
    scenario: Mapped[str] = mapped_column(String(32), default="dev", index=True)
    # Last-known runtime context (restored on session reload).
    last_workspace_path: Mapped[str] = mapped_column(String(512), default="")
    last_environment: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_summarized_message_count: Mapped[t.Optional[int]] = mapped_column(
    Integer, nullable=True, default=None
)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    messages: Mapped[t.List["MessageRow"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    timeline_events: Mapped[t.List["TimelineEventRow"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class MessageRow(Base):
    """Persistent message record within a session."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | tool
    content: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    session: Mapped["SessionRow"] = relationship(back_populates="messages")


class TimelineEventRow(Base):
    """ACP v2 timeline event — persisted for frontend restoration on page refresh.

    Captures the full agent execution timeline (tool calls, thoughts, artifacts,
    agent handoffs, human approvals, system events) as they are streamed via SSE.
    On page refresh, the frontend rebuilds its Timeline component from these rows.

    Distinct from MessageRow: messages are LLM conversation context (role + content);
    timeline events are UI rendering data (event_type + structured payload).
    """

    __tablename__ = "timeline_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_event_id: Mapped[t.Optional[str]] = mapped_column(String(36), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    session: Mapped["SessionRow"] = relationship(back_populates="timeline_events")

    __table_args__ = (
        Index("ix_timeline_events_session", "session_id", "id"),
    )


# ============================================================
# Memory Tables (Three-Layer Memory Architecture)
# ============================================================

class MemoryEventRow(Base):
    """L1 memory event index — key events with UTC timestamps.

    Stores lightweight index entries (event_key + timestamp) that form
    a timeline of significant workspace activities. Designed for fast
    scanning and injection into agent system prompts.
    """

    __tablename__ = "memory_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_path: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_key: Mapped[str] = mapped_column(String(512), nullable=False)
    event_type: Mapped[str] = mapped_column(
        String(32), default="action"
    )  # decision | action | insight | error
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    summaries: Mapped[t.List["MemorySummaryRow"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_memory_events_workspace_created", "workspace_path", "created_at"),
    )


class MemorySummaryRow(Base):
    """L2 memory summary — structured description of a key event.

    Each summary is associated with an L1 event and provides richer
    context including decision rationale, related files, and conclusions.
    Accessed on-demand when the agent needs deeper recall.
    """

    __tablename__ = "memory_summaries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("memory_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_path: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    event: Mapped["MemoryEventRow"] = relationship(back_populates="summaries")


# ============================================================
# Engine & Session Factory (singleton)
# ============================================================

_engine: t.Optional[AsyncEngine] = None
_session_factory: t.Optional[async_sessionmaker[AsyncSession]] = None


def get_engine() -> AsyncEngine:
    """Get or create the async SQLAlchemy engine."""
    global _engine
    if _engine is None:
        _engine = create_async_engine_for_url(
            settings.database_url,
            db_type=settings.database_type,
        )
        logger.info("database_engine_created", url=safe_url(settings.database_url))
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the async session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def init_db() -> None:
    """Initialize database — create tables or run Alembic when AUTO_MIGRATE=true."""
    if os.getenv("AUTO_MIGRATE", "").lower() in ("1", "true", "yes"):
        from pathlib import Path

        from alembic.config import Config

        from alembic import command

        ini_path = Path(__file__).resolve().parents[2] / "alembic.ini"
        cfg = Config(str(ini_path))
        command.upgrade(cfg, "head")
        logger.info("database_migrated_via_alembic")
        return

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Auto-heal: add ORM columns missing from existing tables so the live
    # schema stays in sync with the models without manual migrations.
    await _sync_table_schema(engine, *Base.metadata.tables.values())

    logger.info("database_tables_initialized")


# Strict identifier / literal validators for the raw ALTER TABLE DDL below.
# Although all values currently come from ORM model definitions (not user
# input), validating them before interpolation is defense-in-depth against a
# future code path that might introduce dynamic names.
import re as _re  # noqa: E402

_IDENTIFIER_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Allow standard SQL type expressions, e.g. VARCHAR(255), INTEGER, TIMESTAMP.
_TYPE_SQL_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_ ]*(?:\(\d+\))?$")
# Default literals produced by _server_default_for are either a bare number,
# a quoted string (with internal single quotes doubled per SQL escaping), or
# 0/1 for booleans. Reject anything containing a statement separator or
# comment marker. The quoted-string branch allows '' inside to support the
# standard SQL escape of a literal single quote.
_DEFAULT_LITERAL_RE = _re.compile(r"^(?:'(?:[^']|'')*'|[0-9]+)$")


def _validate_ddl_identifier(name: str, kind: str) -> None:
    if not name or not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL {kind} identifier: {name!r}")


def _validate_ddl_type_sql(type_sql: str) -> None:
    if not type_sql or not _TYPE_SQL_RE.match(type_sql):
        raise ValueError(f"Invalid SQL type expression: {type_sql!r}")


def _validate_ddl_default_literal(literal: str) -> None:
    if not literal or not _DEFAULT_LITERAL_RE.match(literal):
        raise ValueError(f"Invalid SQL DEFAULT literal: {literal!r}")


def _server_default_for(col: Column) -> t.Optional[str]:
    """Derive a SQL literal for a column's Python default, if possible.

    Used when ALTER-ing a new column into an existing table so existing rows
    are backfilled and NOT NULL constraints can be satisfied. Returns None
    when no usable default can be derived.
    """
    default = col.default
    if default is None:
        return None
    val = default.arg
    if callable(val):
        try:
            val = val(None)
        except Exception:
            return None
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        return "'" + val.replace("'", "''") + "'"
    return None


async def _sync_table_schema(engine: AsyncEngine, *tables) -> None:
    """Add ORM-defined columns that are missing from existing DB tables.

    `Base.metadata.create_all` only creates tables that do not exist — it will
    NOT add new columns to tables created by an earlier schema version. This
    helper introspects each table and ALTERs in any missing columns (with a
    server default backfilled from the ORM's Python default) so the live
    schema stays in sync with the ORM without manual migrations.

    Security: table/column names and compiled type strings are interpolated
    into a raw ``ALTER TABLE`` statement. Although they originate from ORM
    model definitions (not user input), we validate them against strict
    identifier / type patterns before interpolation as defense-in-depth against
    a future code path that might introduce dynamic names.
    """
    for table in tables:
        async with engine.begin() as conn:
            def _find_missing(conn, _table=table):
                insp = sqla_inspect(conn)
                if not insp.has_table(_table.name):
                    return []
                existing = {c["name"] for c in insp.get_columns(_table.name)}
                return [c for c in _table.columns if c.name not in existing]

            missing = await conn.run_sync(_find_missing)
            for col in missing:
                type_sql = col.type.compile(dialect=engine.dialect)
                _validate_ddl_identifier(table.name, "table")
                _validate_ddl_identifier(col.name, "column")
                _validate_ddl_type_sql(type_sql)
                fragments = [f"ADD COLUMN {col.name} {type_sql}"]
                if not col.nullable:
                    fragments.append("NOT NULL")
                srv = _server_default_for(col)
                if srv is not None:
                    _validate_ddl_default_literal(srv)
                    fragments.append(f"DEFAULT {srv}")
                stmt = text(f"ALTER TABLE {table.name} " + " ".join(fragments))
                await conn.execute(stmt)
                logger.info(
                    "schema_column_added",
                    table=table.name,
                    column=col.name,
                    type=type_sql,
                    nullable=col.nullable,
                    default=srv,
                )
            if missing:
                logger.info("schema_synced", table=table.name, added=len(missing))


async def close_db() -> None:
    """Dispose engine connections. Called during shutdown."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("database_engine_closed")
