"""Cell migration data plane (design 06 P7 — ``scripts/ops/cell-migrate.py`` server side).

A tenant moving between cells is a staged hand-over (see the placement package's
state machine): identity flips ``migration_state`` while THIS service answers
the data half — what the tenant owns, copying it to the target cell, proving
the copy landed, and eventually erasing the source.

Ownership: each service moves the tables it creates
---------------------------------------------------
This module moves the SIX tables the control plane owns. The three tables
workspace-runtime owns (``knowledge_documents`` / ``file_drafts`` /
``plan_apply_history``) are moved by workspace-runtime's own
``/internal/cell-migration/*`` endpoints, which this module calls and whose
counts it merges into its own.

That split is not tidiness. It previously mirrored those tables here as Core
``Table`` objects and called ``create_all`` on them during import, which produced
two concrete failures:

* a SECOND creator for tables whose owner already creates them, and concurrent
  ``CREATE TABLE IF NOT EXISTS`` can fail inside PostgreSQL's catalog
  (``pg_type_typname_nsp_index`` duplicate key) — an intermittent boot failure
  that looks like nothing else;
* a DRIFTED definition: the mirror declared ``file_drafts.content`` as plain
  ``Text`` while the real column is ``MEDIUMTEXT`` on MySQL, so a cell whose
  table came from the mirror truncates at 64 KiB instead of 16 MiB — the
  migration meant to preserve a large draft would silently destroy it.

What moves and what stays, stated rather than implied
-----------------------------------------------------
MOVES — every row the tenant's continued operation needs:

* ``workspaces`` / ``environments`` / ``environment_events`` — the registry
  the reconciler rebuilds orchestrator deployments from (that repair path is
  exactly how the target cell's deployments come back: no orchestration is
  migrated, it is RECONSTRUCTED);
* ``user_git_credentials`` — without them the target cell cannot push;
* ``tenant_quotas`` / ``tenant_usage`` — governance follows the tenant;
* ``knowledge_documents`` / ``file_drafts`` / ``plan_apply_history`` (via the
  runtime) — metadata and drafts. Document CONTENT lives in the Git repository
  and re-materialises with the working copies (design: working copies are never
  migrated).

STAYS — deliberately, and for reasons worth restating:

* ``lease_history`` / ``task_records`` — append-only audit of what HAPPENED on
  the source cell; moving it would rewrite where an event occurred;
* agent-service's session/message store — a SEPARATE database (service
  ``agent``), holding conversational context rather than workspace state; it
  does not gate workspace availability and remains readable on the source
  cell during the retention window.

Security
--------
Every endpoint is internal-only, guarded by ``X-Internal-Token``
(``CONTROL_SERVICE_CELL_MIGRATION_TOKEN``, fail closed when unset — the same
rule the runtime's internal surface applies). The migration tool is an
operator action with destructive phases; it must never be reachable through
the gateway's public routing.
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
from datacode_common.security.internal_token import (
    INTERNAL_TOKEN_HEADER,
    extract_token,
    token_matches,
)
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Table, delete, insert, inspect, select

from src.config import settings
from src.environments.models import EnvironmentEventModel, EnvironmentModel
from src.quota.models import TenantQuotaModel, TenantUsageModel
from src.runtime.client import get_runtime_client
from src.utils.errors import ServiceError
from src.utils.logging import get_logger
from src.workspace.database import get_session
from src.workspace.models import UserGitCredentialModel, WorkspaceModel

logger = get_logger(__name__)

router = APIRouter(prefix="/internal/cell-migration", tags=["cell-migration"])

# The runtime's half of the migration set. Named here only so this service can
# report a complete per-table count; the definitions live in workspace-runtime.
RUNTIME_TABLES: tuple[str, ...] = (
    "knowledge_documents",
    "file_drafts",
    "plan_apply_history",
)

_RUNTIME_MIGRATION_PREFIX = "/internal/cell-migration"


def _require_internal_token(
    x_internal_token: str = Header(default="", alias=INTERNAL_TOKEN_HEADER),
    authorization: str = Header(default=""),
) -> None:
    """Fail closed: the migration surface must never answer unauthenticated.

    Uses the platform's shared helpers rather than a local comparison. A plain
    ``!=`` returns as soon as two bytes differ, which leaks the secret one
    character at a time to a caller who can measure the response, and it also
    rejects the ``Authorization: Bearer`` form some service meshes are the only
    ones able to set.
    """
    expected = settings.cell_migration_token
    if not expected:
        logger.warning("cell_migration_disabled_request_rejected")
        raise HTTPException(status_code=401, detail="internal token required")

    presented = extract_token(
        internal_header=x_internal_token, authorization=authorization
    )
    if not token_matches(expected, presented):
        # Deliberately vague: which part failed is the caller's business only.
        logger.warning("cell_migration_token_rejected", presented=bool(presented))
        raise HTTPException(status_code=401, detail="internal token required")


# ── migration set declaration ───────────────────────────────────────────────
# One place describes WHAT moves; every endpoint walks this list, so export,
# import, verify and purge can never disagree about scope.


class _TableSpec(t.NamedTuple):
    """One control-owned table's place in the migration set."""

    name: str
    table: Table  # ORM __table__
    # Timestamp columns needing ISO round-tripping. Declared rather than
    # inferred: guessing would eventually convert a text value that merely looks
    # like a timestamp.
    datetime_columns: tuple[str, ...]


# Order matters: parents before children, so imports satisfy logical
# dependencies (workspaces -> environments -> events).
MIGRATION_TABLES: list[_TableSpec] = [
    _TableSpec(
        "workspaces",
        WorkspaceModel.__table__,
        ("created_at", "updated_at", "last_active_at"),
    ),
    _TableSpec(
        "environments",
        EnvironmentModel.__table__,
        ("created_at", "updated_at", "last_applied_at", "expires_at"),
    ),
    _TableSpec("environment_events", EnvironmentEventModel.__table__, ("created_at",)),
    _TableSpec(
        "user_git_credentials",
        UserGitCredentialModel.__table__,
        ("created_at", "updated_at"),
    ),
    _TableSpec("tenant_quotas", TenantQuotaModel.__table__, ("created_at", "updated_at")),
    _TableSpec("tenant_usage", TenantUsageModel.__table__, ("updated_at",)),
]


class MigrationExport(BaseModel):
    """The wire format between two cells' control planes.

    ``tables`` carries BOTH halves: this service's six tables and the three the
    runtime returned, so the bundle a target cell receives is complete and the
    operator tool sees one export regardless of which service owns what.
    """

    tenant_id: str
    # Member user ids the credentials filter used (replayed on import for
    # scope validation — see import's consistency check).
    user_ids: t.List[str] = Field(default_factory=list)
    exported_at: str = ""
    tables: t.Dict[str, t.List[t.Dict[str, t.Any]]] = Field(default_factory=dict)


class MigrationCounts(BaseModel):
    """Row counts per table under the migration scope (verify handshake)."""

    tenant_id: str
    counts: t.Dict[str, int] = Field(default_factory=dict)


# ── scope resolution ────────────────────────────────────────────────────────


async def _existing_tables(session) -> set[str]:
    """Tables physically present in THIS cell's database."""
    conn = await session.connection()
    names = await conn.run_sync(
        lambda sync_conn: set(inspect(sync_conn).get_table_names())
    )
    return names


def _validate_tenant_id(tenant_id: str) -> str:
    """Reject ids that could escape the workspace-path prefix filter."""
    try:
        return validate_tenant_id(tenant_id)
    except InvalidTenantIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _resolve_scope(session, tenant_id: str, user_ids: t.List[str]) -> dict:
    """Resolve the filters every table selection needs.

    The registry tables hang off ids (workspace_id / environment_id); the prefix
    is resolved too so this service and the runtime agree on which working copies
    belong to the tenant.
    """
    prefix = tenant_path_prefix(settings.workspace_base_path, tenant_id)

    workspace_ids = [
        row[0]
        for row in (
            await session.execute(
                select(WorkspaceModel.id).where(WorkspaceModel.tenant_id == tenant_id)
            )
        ).fetchall()
    ]
    environment_ids = [
        row[0]
        for row in (
            await session.execute(
                select(EnvironmentModel.id).where(
                    EnvironmentModel.workspace_id.in_(workspace_ids or ["-"])
                )
            )
        ).fetchall()
    ]
    return {
        "tenant_id": tenant_id,
        "user_ids": user_ids,
        "workspace_ids": workspace_ids,
        "environment_ids": environment_ids,
        "prefix": prefix,
    }


def _filter_for(spec: _TableSpec, scope: dict):
    """The WHERE clause selecting this tenant's rows in one table."""
    table = spec.table
    name = spec.name
    if name == "workspaces":
        return table.c.tenant_id == scope["tenant_id"]
    if name == "environments":
        return table.c.workspace_id.in_(scope["workspace_ids"] or ["-"])
    if name == "environment_events":
        return table.c.environment_id.in_(scope["environment_ids"] or ["-"])
    if name == "user_git_credentials":
        return table.c.user_id.in_(scope["user_ids"] or ["-"])
    # tenant_quotas / tenant_usage
    return table.c.tenant_id == scope["tenant_id"]


# ── runtime half ────────────────────────────────────────────────────────────


async def _runtime_call(action: str, tenant_id: str, payload: dict | None = None) -> dict:
    """Delegate one migration phase for the runtime-owned tables.

    Failure PROPAGATES rather than degrading to zero rows. An unreachable runtime
    is indistinguishable from "this tenant has no drafts" if we swallowed it, and
    the purge phase would then erase rows the export never carried.
    """
    client = get_runtime_client()
    path = f"{_RUNTIME_MIGRATION_PREFIX}/{tenant_id}/{action}"
    try:
        if payload is None:
            return await client.get(path)
        return await client.post(path, payload)
    except ServiceError as exc:
        logger.error(
            "cell_migration_runtime_call_failed",
            action=action,
            tenant_id=tenant_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"workspace-runtime could not {action} its tables for this tenant: "
                f"{exc}. The migration must not proceed on a partial set."
            ),
        ) from exc


def _runtime_counts(body: dict, key: str) -> dict[str, int]:
    """Per-table counts from a runtime response, defaulting missing tables to 0."""
    reported = body.get(key) or {}
    return {name: int(reported.get(name, 0)) for name in RUNTIME_TABLES}


# ── endpoints ───────────────────────────────────────────────────────────────


@router.get("/{tenant_id}/export", response_model=MigrationExport)
async def export_tenant_rows(
    tenant_id: str,
    user_ids: str = "",
    _token: None = Depends(_require_internal_token),
):
    """Serialize one tenant's migration set (the source cell's half).

    ``user_ids`` scopes ``user_git_credentials``: membership lives in the
    GLOBAL tier, which the operator's tooling (cell-migrate.py) reads and
    passes down — the cell database cannot answer "who is in this tenant".
    """
    tenant_id = _validate_tenant_id(tenant_id)
    members = [value.strip() for value in user_ids.split(",") if value.strip()]

    async with get_session() as session:
        scope = await _resolve_scope(session, tenant_id, members)
        present = await _existing_tables(session)
        tables: dict[str, list[dict]] = {}
        counts: dict[str, int] = {}
        for spec in MIGRATION_TABLES:
            if spec.table.name not in present:
                # A cell whose migrations have not run has no such data, which
                # is an empty export for that table rather than an error.
                tables[spec.name] = []
                counts[spec.name] = 0
                continue
            rows = (
                (
                    await session.execute(
                        select(spec.table).where(_filter_for(spec, scope))
                    )
                )
                .mappings()
                .all()
            )
            tables[spec.name] = [serialize_row(dict(row)) for row in rows]
            counts[spec.name] = len(rows)

    runtime_export = await _runtime_call("export", tenant_id)
    runtime_tables = runtime_export.get("tables") or {}
    for name in RUNTIME_TABLES:
        rows = runtime_tables.get(name) or []
        tables[name] = rows
        counts[name] = len(rows)

    logger.info(
        "cell_migration_exported",
        tenant_id=tenant_id,
        members=len(members),
        workspaces=len(scope["workspace_ids"]),
        counts=counts,
    )
    return MigrationExport(
        tenant_id=tenant_id,
        user_ids=members,
        exported_at=datetime.now(timezone.utc).isoformat(),
        tables=tables,
    )


@router.post("/{tenant_id}/import", response_model=MigrationCounts)
async def import_tenant_rows(
    tenant_id: str,
    payload: MigrationExport,
    _token: None = Depends(_require_internal_token),
):
    """Write an export into THIS cell (the target cell's half).

    Idempotent by replacement: the tenant's rows under the export's own scope
    are deleted then re-inserted in one transaction, so a retried migration
    converges instead of duplicating. The payload must be about the SAME
    tenant as the path — a mismatched bundle is rejected rather than written
    under a foreign scope.
    """
    tenant_id = _validate_tenant_id(tenant_id)
    if payload.tenant_id != tenant_id:
        raise HTTPException(
            status_code=400,
            detail="payload tenant does not match the target tenant",
        )

    async with get_session() as session:
        counts: dict[str, int] = {}
        async with session.begin():
            # Scope resolution INSIDE the transaction: the select would
            # otherwise autobegin and make the explicit begin() above fail.
            scope = await _resolve_scope(session, tenant_id, payload.user_ids)
            for spec in MIGRATION_TABLES:
                rows = payload.tables.get(spec.name, [])
                # Replace-in-place under the SAME filter export used.
                await session.execute(
                    delete(spec.table).where(_filter_for(spec, scope))
                )
                if rows:
                    await session.execute(
                        insert(spec.table),
                        [deserialize_row(row, spec.datetime_columns) for row in rows],
                    )
                counts[spec.name] = len(rows)

    runtime_result = await _runtime_call(
        "import",
        tenant_id,
        {
            "tenant_id": tenant_id,
            "exported_at": payload.exported_at,
            "tables": {
                name: payload.tables.get(name, []) for name in RUNTIME_TABLES
            },
        },
    )
    counts.update(_runtime_counts(runtime_result, "counts"))

    logger.info("cell_migration_imported", tenant_id=tenant_id, counts=counts)
    return MigrationCounts(tenant_id=tenant_id, counts=counts)


@router.get("/{tenant_id}/verify", response_model=MigrationCounts)
async def verify_tenant_rows(
    tenant_id: str,
    user_ids: str = "",
    _token: None = Depends(_require_internal_token),
):
    """Count the tenant's rows per table — the verifying state's handshake."""
    tenant_id = _validate_tenant_id(tenant_id)
    members = [value.strip() for value in user_ids.split(",") if value.strip()]

    async with get_session() as session:
        scope = await _resolve_scope(session, tenant_id, members)
        present = await _existing_tables(session)
        counts: dict[str, int] = {}
        for spec in MIGRATION_TABLES:
            if spec.table.name not in present:
                counts[spec.name] = 0
                continue
            rows = (
                (
                    await session.execute(
                        select(spec.table).where(_filter_for(spec, scope))
                    )
                )
                .mappings()
                .all()
            )
            counts[spec.name] = len(rows)

    runtime_result = await _runtime_call("verify", tenant_id)
    counts.update(_runtime_counts(runtime_result, "counts"))

    logger.info("cell_migration_verified", tenant_id=tenant_id, counts=counts)
    return MigrationCounts(tenant_id=tenant_id, counts=counts)


@router.post("/{tenant_id}/purge")
async def purge_tenant_rows(
    tenant_id: str,
    user_ids: str = "",
    _token: None = Depends(_require_internal_token),
):
    """Erase the tenant from THIS cell — the post-retention cleanup phase.

    Called on the source cell after the retention window; the export filter
    and this delete share one scope definition, so "what moved" and "what is
    erased" cannot drift apart.

    The runtime's tables are purged FIRST: if that call fails the control-plane
    rows are still present, so the operator retries a partial cleanup rather than
    losing the registry that says which working copies the leftovers belong to.
    """
    tenant_id = _validate_tenant_id(tenant_id)
    members = [value.strip() for value in user_ids.split(",") if value.strip()]

    runtime_result = await _runtime_call("purge", tenant_id, {})
    deleted: dict[str, int] = _runtime_counts(runtime_result, "deleted")

    async with get_session() as session:
        async with session.begin():
            # Same ordering rule as import: scope resolution inside the
            # transaction, children deleted before parents.
            scope = await _resolve_scope(session, tenant_id, members)
            present = await _existing_tables(session)
            for spec in reversed(MIGRATION_TABLES):
                if spec.table.name not in present:
                    deleted[spec.name] = 0
                    continue
                result = await session.execute(
                    delete(spec.table).where(_filter_for(spec, scope))
                )
                deleted[spec.name] = result.rowcount or 0

    logger.info("cell_migration_purged", tenant_id=tenant_id, deleted=deleted)
    return {"tenant_id": tenant_id, "deleted": deleted}
