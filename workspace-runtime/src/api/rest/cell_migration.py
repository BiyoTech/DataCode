"""Cross-cell migration for the tables THIS service owns (design 06 P7).

A tenant moving between cells is orchestrated by the control plane, but the data
is moved by whoever owns it. This module is the runtime's half: ``file_drafts``,
``plan_apply_history`` and ``knowledge_documents`` — the three tables this
service's ``init_db`` creates.

Why the runtime owns its half rather than the control plane mirroring it
-----------------------------------------------------------------------
The control plane used to declare Core mirrors of these tables and call
``create_all`` on them during import. Two things were wrong with that, and both
had already happened:

* **A second creator.** One creator per table is a hard invariant here (see
  ``src.workspace.database.init_db``): concurrent ``CREATE TABLE IF NOT EXISTS``
  can fail inside PostgreSQL's catalog with a duplicate key on
  ``pg_type_typname_nsp_index``, an intermittent boot failure that looks like
  nothing else.
* **Drifted column definitions.** The mirror declared ``file_drafts.content`` as
  plain ``Text``, while the real column is ``MEDIUMTEXT`` on MySQL. A cell whose
  table was created from the mirror caps drafts at 64 KiB instead of 16 MiB, so
  the migration meant to preserve a large draft would truncate it.

Owning the tables end to end removes both by construction: there is no second
definition to drift and no second creator to race.

Security
--------
Internal-only, guarded by the same shared-secret dependency as the rest of
``/internal/*`` and fail closed when no token is configured. Migration has
destructive phases and must never be reachable through the gateway.
"""

from __future__ import annotations

import typing as t
from datetime import datetime, timezone

from datacode_common.cell_migration import (
    InvalidTenantIdError,
    deserialize_row,
    serialize_row,
    tenant_path_prefix,
    validate_tenant_id,
)
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Table, delete, insert, inspect, select

from src.api.dagster.internal_auth import InternalCaller
from src.config import settings
from src.utils.logging import get_logger
from src.workspace.database import get_session
from src.workspace.models import (
    FileDraftModel,
    KnowledgeDocumentModel,
    PlanApplyHistoryModel,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/internal/cell-migration", tags=["cell-migration"])


class _TableSpec(t.NamedTuple):
    """One runtime-owned table's place in the migration set."""

    name: str
    table: Table
    # Timestamp columns needing ISO round-tripping. Declared rather than
    # inferred: guessing would eventually convert a text value that merely looks
    # like a timestamp.
    datetime_columns: tuple[str, ...]


# All three tables are scoped by workspace_path prefix, so order is irrelevant
# for referential integrity — but it is kept stable so counts read the same way
# in every phase's log line.
MIGRATION_TABLES: list[_TableSpec] = [
    _TableSpec(
        "knowledge_documents",
        KnowledgeDocumentModel.__table__,
        ("created_at", "updated_at"),
    ),
    _TableSpec("file_drafts", FileDraftModel.__table__, ("updated_at",)),
    _TableSpec("plan_apply_history", PlanApplyHistoryModel.__table__, ("created_at",)),
]

TABLE_NAMES: tuple[str, ...] = tuple(spec.name for spec in MIGRATION_TABLES)


class MigrationExport(BaseModel):
    """The wire format between two cells' runtimes."""

    tenant_id: str
    exported_at: str = ""
    tables: t.Dict[str, t.List[t.Dict[str, t.Any]]] = Field(default_factory=dict)


class MigrationCounts(BaseModel):
    """Row counts per table under the migration scope."""

    tenant_id: str
    counts: t.Dict[str, int] = Field(default_factory=dict)


def _prefix(tenant_id: str) -> str:
    """Path prefix selecting one tenant's rows, or 400 on an unusable id.

    Both services derive the prefix from the same platform-level
    ``workspace.base_path``, so the two halves of a migration agree on scope.
    """
    try:
        validate_tenant_id(tenant_id)
        return tenant_path_prefix(settings.workspace_base_path, tenant_id)
    except InvalidTenantIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _filter_for(spec: _TableSpec, prefix: str):
    """The WHERE clause selecting this tenant's rows.

    One definition for all four phases: if purge selected more rows than export
    did, the retention cleanup would erase data that was never copied.
    """
    return spec.table.c.workspace_path.like(f"{prefix}%")


async def _existing_tables(session) -> set[str]:
    """Tables physically present in this cell's database.

    A cell where this service has not yet started has no such data, which export
    and verify must report as ZERO ROWS: migrating out of a half-provisioned cell
    is legitimate. Import, by contrast, creates nothing — the tables appear when
    this service starts, which is a precondition the orchestrator checks.
    """
    conn = await session.connection()
    return await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))


@router.get("/{tenant_id}/export", response_model=MigrationExport)
async def export_tenant_rows(tenant_id: str, _internal: InternalCaller = None) -> MigrationExport:
    """Serialize this tenant's runtime-owned rows (source cell's half)."""
    prefix = _prefix(tenant_id)

    async with get_session() as session:
        present = await _existing_tables(session)
        tables: dict[str, list[dict]] = {}
        counts: dict[str, int] = {}
        for spec in MIGRATION_TABLES:
            if spec.table.name not in present:
                tables[spec.name] = []
                counts[spec.name] = 0
                continue
            rows = (
                (await session.execute(select(spec.table).where(_filter_for(spec, prefix))))
                .mappings()
                .all()
            )
            tables[spec.name] = [serialize_row(dict(row)) for row in rows]
            counts[spec.name] = len(rows)

    logger.info("cell_migration_exported", tenant_id=tenant_id, counts=counts)
    return MigrationExport(
        tenant_id=tenant_id,
        exported_at=datetime.now(timezone.utc).isoformat(),
        tables=tables,
    )


@router.post("/{tenant_id}/import", response_model=MigrationCounts)
async def import_tenant_rows(
    tenant_id: str, payload: MigrationExport, _internal: InternalCaller = None
) -> MigrationCounts:
    """Write an export into this cell (target cell's half).

    Idempotent by replacement: the tenant's rows under the same filter export
    used are deleted then re-inserted in one transaction, so a retried migration
    converges instead of duplicating.
    """
    prefix = _prefix(tenant_id)
    if payload.tenant_id != tenant_id:
        raise HTTPException(
            status_code=400, detail="payload tenant does not match the target tenant"
        )

    async with get_session() as session:
        counts: dict[str, int] = {}
        # The presence probe and the writes share ONE transaction: probing on the
        # session first would autobegin it and make the explicit begin() below
        # fail with "a transaction is already begun".
        async with session.begin():
            present = await _existing_tables(session)
            missing = [
                spec.name for spec in MIGRATION_TABLES if spec.table.name not in present
            ]
            if missing:
                # Creating them here would make this a second creator for tables
                # this service already creates at startup, which is exactly the
                # concurrent-DDL race the ownership rule exists to prevent.
                logger.error("cell_migration_import_tables_missing", tables=missing)
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"runtime tables not initialized on this cell: {missing}; "
                        "start workspace-runtime before importing"
                    ),
                )

            for spec in MIGRATION_TABLES:
                rows = payload.tables.get(spec.name, [])
                await session.execute(delete(spec.table).where(_filter_for(spec, prefix)))
                if rows:
                    await session.execute(
                        insert(spec.table),
                        [deserialize_row(row, spec.datetime_columns) for row in rows],
                    )
                counts[spec.name] = len(rows)

    logger.info("cell_migration_imported", tenant_id=tenant_id, counts=counts)
    return MigrationCounts(tenant_id=tenant_id, counts=counts)


@router.get("/{tenant_id}/verify", response_model=MigrationCounts)
async def verify_tenant_rows(tenant_id: str, _internal: InternalCaller = None) -> MigrationCounts:
    """Count this tenant's rows per table — the verifying phase's handshake."""
    prefix = _prefix(tenant_id)

    async with get_session() as session:
        present = await _existing_tables(session)
        counts: dict[str, int] = {}
        for spec in MIGRATION_TABLES:
            if spec.table.name not in present:
                counts[spec.name] = 0
                continue
            rows = (
                (await session.execute(select(spec.table).where(_filter_for(spec, prefix))))
                .mappings()
                .all()
            )
            counts[spec.name] = len(rows)

    logger.info("cell_migration_verified", tenant_id=tenant_id, counts=counts)
    return MigrationCounts(tenant_id=tenant_id, counts=counts)


@router.post("/{tenant_id}/purge")
async def purge_tenant_rows(tenant_id: str, _internal: InternalCaller = None) -> dict:
    """Erase this tenant's runtime-owned rows — post-retention cleanup."""
    prefix = _prefix(tenant_id)

    async with get_session() as session:
        deleted: dict[str, int] = {}
        async with session.begin():
            present = await _existing_tables(session)
            for spec in reversed(MIGRATION_TABLES):
                if spec.table.name not in present:
                    deleted[spec.name] = 0
                    continue
                result = await session.execute(
                    delete(spec.table).where(_filter_for(spec, prefix))
                )
                deleted[spec.name] = result.rowcount or 0

    logger.info("cell_migration_purged", tenant_id=tenant_id, deleted=deleted)
    return {"tenant_id": tenant_id, "deleted": deleted}
