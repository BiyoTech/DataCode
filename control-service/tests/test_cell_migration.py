"""Cell migration endpoint tests (design 06 P7 — the data half).

The contract worth pinning is a ROUND TRIP against a real database: seed a
tenant (plus a second tenant as a control group), export, wipe the tenant,
import, verify — the counts must match, the OTHER tenant must never have been
touched, and a mismatched bundle must be rejected rather than written under a
foreign scope.

The token guard gets its own test because the surface is deliberately
destructive (purge): unauthenticated must mean 401, not a different route.

Since the ownership split, the three runtime-owned tables are moved by
workspace-runtime and this service DELEGATES to it. The fake below is a real
implementation of that contract over the same SQLite database, so the round trip
still exercises all nine tables while proving this service never creates or
writes the runtime's three itself.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from datacode_common.cell_migration import (
    deserialize_row,
    serialize_row,
    tenant_path_prefix,
)
from fastapi.testclient import TestClient
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    insert,
    select,
)
from sqlalchemy import inspect as sa_inspect

import src.workspace.database as database
from src.api.rest import cell_migration as cm
from src.main import app
from src.utils.errors import RuntimeUnavailableError

BASE = "/workspaces"
T1_WS = "ws-t1-shop"
T1_ENV = f"{T1_WS}:dev"
T1_PATH = f"{BASE}/t1/{T1_WS}/alice/envs/dev"
T2_PATH = f"{BASE}/t10/{'ws-t10-other'}/bob/envs/dev"  # prefix-collision probe
TOKEN = "cell-migration-test-token"

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The runtime half, implemented rather than stubbed
# ---------------------------------------------------------------------------
# These three tables belong to workspace-runtime. Their definitions appear here,
# in a test, ON PURPOSE: production has exactly one definition (the runtime's),
# which is what stops a mirrored column from drifting. The previous mirror lived
# in the control plane's own module and declared file_drafts.content as plain
# Text while the real column is MEDIUMTEXT on MySQL, capping drafts at 64 KiB
# instead of 16 MiB.

RUNTIME_METADATA = MetaData()

knowledge_documents = Table(
    "knowledge_documents",
    RUNTIME_METADATA,
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

file_drafts = Table(
    "file_drafts",
    RUNTIME_METADATA,
    Column("id", String(64), primary_key=True),
    Column("workspace_path", String(512), nullable=False),
    Column("file_path", String(512), nullable=False),
    Column("content", Text(), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

plan_apply_history = Table(
    "plan_apply_history",
    RUNTIME_METADATA,
    Column("id", String(64), primary_key=True),
    Column("workspace_path", String(512), nullable=False),
    Column("environment", String(64), nullable=False),
    Column("user_id", String(64), nullable=False),
    Column("session_id", String(64), nullable=False),
    Column("trigger_type", String(16), nullable=False),
    Column("applied", Integer(), nullable=False),
    Column("reason", String(256), nullable=True),
    Column("has_changes", Integer(), nullable=False),
    Column("added_count", Integer(), nullable=False),
    Column("removed_count", Integer(), nullable=False),
    Column("direct_count", Integer(), nullable=False),
    Column("indirect_count", Integer(), nullable=False),
    Column("metadata_count", Integer(), nullable=False),
    Column("backfill_count", Integer(), nullable=False),
    Column("plan_result_json", Text(), nullable=True),
    Column("duration_ms", Integer(), nullable=False),
    Column("error_message", Text(), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

RUNTIME_TABLES = {
    "knowledge_documents": (knowledge_documents, ("created_at", "updated_at")),
    "file_drafts": (file_drafts, ("updated_at",)),
    "plan_apply_history": (plan_apply_history, ("created_at",)),
}


class FakeRuntimeMigration:
    """The runtime's four migration phases, over the same database.

    A real implementation rather than a stub, so the round-trip assertions still
    cover all nine tables end to end and a scope disagreement between the two
    halves would surface as mismatched counts.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.unavailable = False

    async def get(self, path: str, params=None) -> dict:
        return await self._dispatch(path, None)

    async def post(self, path: str, payload: dict) -> dict:
        return await self._dispatch(path, payload)

    async def _dispatch(self, path: str, payload) -> dict:
        _, tenant_id, action = path.rsplit("/", 2)
        self.calls.append((action, tenant_id))
        if self.unavailable:
            raise RuntimeUnavailableError(f"POST {path}", "connection refused")

        prefix = tenant_path_prefix(BASE, tenant_id)
        if action == "export":
            return {"tenant_id": tenant_id, "tables": await self._export(prefix)}
        if action == "verify":
            return {"tenant_id": tenant_id, "counts": await self._counts(prefix)}
        if action == "import":
            return {
                "tenant_id": tenant_id,
                "counts": await self._import(prefix, payload or {}),
            }
        if action == "purge":
            return {"tenant_id": tenant_id, "deleted": await self._purge(prefix)}
        raise AssertionError(f"unexpected runtime migration action: {action}")

    async def _present(self, session) -> set[str]:
        conn = await session.connection()
        return await conn.run_sync(
            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
        )

    async def _export(self, prefix: str) -> dict:
        out: dict[str, list[dict]] = {}
        async with database.get_session() as session:
            present = await self._present(session)
            for name, (table, _) in RUNTIME_TABLES.items():
                if name not in present:
                    out[name] = []
                    continue
                rows = (
                    (
                        await session.execute(
                            select(table).where(
                                table.c.workspace_path.like(f"{prefix}%")
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                out[name] = [serialize_row(dict(row)) for row in rows]
        return out

    async def _counts(self, prefix: str) -> dict:
        exported = await self._export(prefix)
        return {name: len(rows) for name, rows in exported.items()}

    async def _import(self, prefix: str, payload: dict) -> dict:
        tables = payload.get("tables") or {}
        counts: dict[str, int] = {}
        async with database.get_session() as session:
            async with session.begin():
                for name, (table, dt_columns) in RUNTIME_TABLES.items():
                    rows = tables.get(name) or []
                    await session.execute(
                        delete(table).where(table.c.workspace_path.like(f"{prefix}%"))
                    )
                    if rows:
                        await session.execute(
                            insert(table),
                            [deserialize_row(row, dt_columns) for row in rows],
                        )
                    counts[name] = len(rows)
        return counts

    async def _purge(self, prefix: str) -> dict:
        deleted: dict[str, int] = {}
        async with database.get_session() as session:
            async with session.begin():
                present = await self._present(session)
                for name, (table, _) in RUNTIME_TABLES.items():
                    if name not in present:
                        deleted[name] = 0
                        continue
                    result = await session.execute(
                        delete(table).where(table.c.workspace_path.like(f"{prefix}%"))
                    )
                    deleted[name] = result.rowcount or 0
        return deleted


@pytest.fixture()
async def migration_db(tmp_path, monkeypatch):
    """A throwaway SQLite database with the full migration schema."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'cell_migration.db'}"
    # Drop any engine a previous test built; monkeypatch restores the
    # URL factory afterwards, so the next caller rebuilds from settings.
    database.reset_engine()
    monkeypatch.setattr(database, "_build_database_url", lambda: url)
    # The settings singleton is built at import time, so the token is patched
    # on the instance; the base path is a property reading env on every call.
    from src.config import settings

    monkeypatch.setattr(settings, "cell_migration_token", TOKEN)
    monkeypatch.setenv("CONTROL_SERVICE_WORKSPACE_BASE_PATH", BASE)

    # Control-owned tables via this service's ORM metadata. The three
    # runtime-owned tables are created here only to stand in for the runtime
    # having started: their definitions come from the fake, which mirrors the
    # runtime's ORM, and this service never creates them in production.
    from src.environments.models import EnvironmentEventModel, EnvironmentModel
    from src.quota.models import TenantQuotaModel
    from src.workspace.models import UserGitCredentialModel, WorkspaceModel

    engine = database.get_engine()
    async with engine.begin() as conn:
        from src.workspace.models import Base

        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(RUNTIME_METADATA.create_all)

    async with database.get_session() as session:
        await session.execute(
            insert(WorkspaceModel.__table__).values(
                id=T1_WS,
                tenant_id="t1",
                owner_user_id="alice",
                name="shop",
                dialect="duckdb",
                git_repo_url="https://git.example/shop.git",
                production_env="prod",
                default_env="dev",
                status="active",
                created_at=NOW,
                updated_at=NOW,
                last_active_at=NOW,
            )
        )
        await session.execute(
            insert(EnvironmentModel.__table__).values(
                id=T1_ENV,
                workspace_id=T1_WS,
                tenant_id="t1",
                name="dev",
                branch="env/dev",
                env_type="sandbox",
                base_env="prod",
                owner_user_id="alice",
                status="active",
                deployment_name="",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.execute(
            insert(EnvironmentEventModel.__table__).values(
                id="evt-1",
                environment_id=T1_ENV,
                action="create",
                actor_user_id="alice",
                trigger_type="human",
                succeeded=1,
                duration_ms=12,
                created_at=NOW,
            )
        )
        await session.execute(
            insert(UserGitCredentialModel.__table__).values(
                id="cred-1",
                user_id="alice",
                provider="github",
                auth_type="pat",
                token="enc:whatever",
                is_default=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.execute(
            insert(TenantQuotaModel.__table__).values(
                tenant_id="t1",
                max_workspaces=7,
                max_environments=70,
                max_concurrent_leases=7,
                max_runtime_replicas=2,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.execute(
            insert(knowledge_documents).values(
                id="sales.md",
                workspace_path=T1_PATH,
                title="Sales",
                filename="sales.md",
                content_type="markdown",
                file_size=10,
                tags="kpi",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.execute(
            insert(file_drafts).values(
                id="draft-1",
                workspace_path=T1_PATH,
                file_path="models/stg.sql",
                content="SELECT 1",
                updated_at=NOW,
            )
        )
        await session.execute(
            insert(plan_apply_history).values(
                id="apply-1",
                workspace_path=T1_PATH,
                environment="dev",
                user_id="alice",
                session_id="",
                trigger_type="human",
                applied=1,
                has_changes=1,
                added_count=2,
                removed_count=0,
                direct_count=2,
                indirect_count=0,
                metadata_count=0,
                backfill_count=0,
                duration_ms=1000,
                created_at=NOW,
            )
        )
        # Control group: a tenant whose id is a PREFIX of t1 (t10) plus an
        # unrelated user's credential.
        await session.execute(
            insert(WorkspaceModel.__table__).values(
                id="ws-t10-other",
                tenant_id="t10",
                owner_user_id="bob",
                name="other",
                dialect="duckdb",
                production_env="prod",
                default_env="dev",
                status="active",
                created_at=NOW,
                updated_at=NOW,
                last_active_at=NOW,
            )
        )
        await session.execute(
            insert(knowledge_documents).values(
                id="other.md",
                workspace_path=T2_PATH,
                title="Other tenant",
                filename="other.md",
                content_type="markdown",
                file_size=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.execute(
            insert(UserGitCredentialModel.__table__).values(
                id="cred-2",
                user_id="mallory",
                provider="github",
                auth_type="pat",
                token="enc:other",
                is_default=0,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.commit()

    yield
    await database.close_db()


@pytest.fixture()
def runtime(migration_db, monkeypatch):
    """Install the runtime half this service delegates its three tables to."""
    from src.runtime import client as runtime_client

    fake = FakeRuntimeMigration()
    monkeypatch.setattr(runtime_client, "get_runtime_client", lambda: fake)
    monkeypatch.setattr(cm, "get_runtime_client", lambda: fake)
    return fake


@pytest.fixture()
def client(runtime):
    return TestClient(app, headers={"X-Internal-Token": TOKEN})


def _export(client, tenant="t1", users="alice"):
    return client.get(
        f"/internal/cell-migration/{tenant}/export", params={"user_ids": users}
    )


class TestExport:
    def test_exports_exactly_the_tenant_scope(self, client):
        response = _export(client)
        assert response.status_code == 200
        body = response.json()

        assert body["tenant_id"] == "t1"
        assert body["user_ids"] == ["alice"]
        assert [w["id"] for w in body["tables"]["workspaces"]] == [T1_WS]
        assert [e["id"] for e in body["tables"]["environments"]] == [T1_ENV]
        assert body["tables"]["environment_events"][0]["environment_id"] == T1_ENV
        assert [c["id"] for c in body["tables"]["user_git_credentials"]] == ["cred-1"]
        assert body["tables"]["tenant_quotas"][0]["max_workspaces"] == 7
        assert body["tables"]["tenant_usage"] == []  # no row seeded
        assert [d["id"] for d in body["tables"]["knowledge_documents"]] == ["sales.md"]
        assert body["tables"]["file_drafts"][0]["content"] == "SELECT 1"
        assert body["tables"]["plan_apply_history"][0]["added_count"] == 2

    def test_prefix_collision_tenant_is_not_swept_in(self, client):
        """t10's rows must not match t1's path-prefix filter (the trailing
        slash rule) — a false positive would COPY another tenant's data."""
        body = _export(client).json()
        assert all(
            d["workspace_path"].startswith(f"{BASE}/t1/")
            for d in body["tables"]["knowledge_documents"]
        )
        ids = {w["id"] for w in body["tables"]["workspaces"]}
        assert "ws-t10-other" not in ids

    def test_credentials_scope_follows_membership(self, client):
        body = _export(client, users="alice,mallory").json()
        ids = {c["id"] for c in body["tables"]["user_git_credentials"]}
        assert ids == {"cred-1", "cred-2"}

    def test_unauthenticated_is_refused(self, migration_db):
        api = TestClient(app)
        response = api.get("/internal/cell-migration/t1/export")
        assert response.status_code == 401

    def test_wrong_token_is_refused(self, migration_db):
        api = TestClient(app, headers={"X-Internal-Token": "nope"})
        response = api.get("/internal/cell-migration/t1/export")
        assert response.status_code == 401

    def test_dangerous_tenant_ids_are_rejected(self, client):
        # A slash never reaches the handler (FastAPI route matching), which is
        # the first line of defence; traversal dots that DO reach it are
        # refused by the explicit validator.
        assert _export(client, tenant="t1/..").status_code == 404
        assert _export(client, tenant="t1..x").status_code == 400
        assert _export(client, tenant="").status_code == 404


class TestRoundTrip:
    def test_export_wipe_import_verify(self, client):
        """The full staged hand-over against a real database."""
        export = _export(client).json()
        source_counts = {
            name: len(rows) for name, rows in export["tables"].items()
        }

        # Wipe the tenant from the (source) database, keeping the control
        # group intact — the migration's data movement, compressed.
        purged = client.post(
            "/internal/cell-migration/t1/purge", params={"user_ids": "alice"}
        )
        assert purged.status_code == 200
        assert purged.json()["deleted"]["workspaces"] == 1

        wiped = client.get(
            "/internal/cell-migration/t1/verify", params={"user_ids": "alice"}
        ).json()
        assert set(wiped["counts"].values()) == {0}

        imported = client.post("/internal/cell-migration/t1/import", json=export)
        assert imported.status_code == 200

        verified = client.get(
            "/internal/cell-migration/t1/verify", params={"user_ids": "alice"}
        ).json()
        assert verified["counts"] == source_counts

        # Values survived the JSON round trip, datetimes included.
        async def _check_values():
            from src.workspace.models import WorkspaceModel

            async with database.get_session() as session:
                row = (
                    await session.execute(
                        select(WorkspaceModel).where(WorkspaceModel.id == T1_WS)
                    )
                ).scalar_one()
                assert row.tenant_id == "t1"
                assert row.last_active_at is not None

        import asyncio

        asyncio.get_event_loop_policy()
        asyncio.run(_check_values())

    def test_the_control_group_survives_every_phase(self, client):
        """Purge/import of t1 must never touch t10's or mallory's rows."""

        async def _control_counts() -> tuple[int, int, int]:
            from src.workspace.models import UserGitCredentialModel, WorkspaceModel

            async with database.get_session() as session:
                workspaces = len(
                    (
                        await session.execute(
                            select(WorkspaceModel.id).where(
                                WorkspaceModel.tenant_id == "t10"
                            )
                        )
                    ).fetchall()
                )
                docs = len(
                    (
                        await session.execute(
                            select(knowledge_documents).where(
                                knowledge_documents.c.workspace_path == T2_PATH
                            )
                        )
                    ).fetchall()
                )
                creds = len(
                    (
                        await session.execute(
                            select(UserGitCredentialModel.id).where(
                                UserGitCredentialModel.user_id == "mallory"
                            )
                        )
                    ).fetchall()
                )
            return workspaces, docs, creds

        import asyncio

        before = asyncio.run(_control_counts())
        export = _export(client).json()

        client.post("/internal/cell-migration/t1/purge", params={"user_ids": "alice"})
        client.post("/internal/cell-migration/t1/import", json=export)
        client.post("/internal/cell-migration/t1/purge", params={"user_ids": "alice"})

        assert asyncio.run(_control_counts()) == before

    def test_import_rejects_mismatched_bundle(self, client):
        export = _export(client).json()
        export["tenant_id"] = "t10"  # foreign bundle

        response = client.post("/internal/cell-migration/t1/import", json=export)
        assert response.status_code == 400

    def test_import_is_idempotent(self, client):
        """A retried migration converges instead of duplicating rows."""
        export = _export(client).json()

        first = client.post("/internal/cell-migration/t1/import", json=export)
        second = client.post("/internal/cell-migration/t1/import", json=export)

        assert first.json()["counts"] == second.json()["counts"]
        verified = client.get(
            "/internal/cell-migration/t1/verify", params={"user_ids": "alice"}
        ).json()
        assert verified["counts"]["workspaces"] == 1  # not 2

    def test_missing_runtime_tables_export_as_zero_rows(self, client):
        """A half-provisioned cell (control up, runtime never started) has no
        runtime-owned tables: export/verify must answer ZERO ROWS, not 500 —
        migrating OUT of such a cell is legitimate. Caught by the local
        two-cell drill against live services."""
        import asyncio

        from sqlalchemy import text

        async def _drop():
            engine = database.get_engine()
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE plan_apply_history"))
                await conn.execute(text("DROP TABLE knowledge_documents"))

        asyncio.run(_drop())

        response = _export(client)
        assert response.status_code == 200
        body = response.json()
        assert body["tables"]["plan_apply_history"] == []
        assert body["tables"]["knowledge_documents"] == []
        assert body["tables"]["workspaces"]  # control-owned data still moves

        verified = client.get(
            "/internal/cell-migration/t1/verify", params={"user_ids": "alice"}
        )
        assert verified.status_code == 200
        assert verified.json()["counts"]["plan_apply_history"] == 0


class TestOwnershipSplit:
    """The runtime owns three of the nine tables; this service delegates them.

    The regression these guard: this service used to declare Core mirrors of
    those tables and create them during import. That made it a SECOND creator
    for tables their owner already creates (concurrent CREATE TABLE can fail
    inside PostgreSQL's catalog) and left a mirrored column free to drift — the
    mirror's file_drafts.content was plain Text while the real column is
    MEDIUMTEXT on MySQL, so a cell built from the mirror truncates a large draft
    at 64 KiB during the very migration meant to preserve it.
    """

    def test_this_service_declares_no_runtime_table(self):
        """Structural guard: no mirror means no definition that can drift."""
        from sqlalchemy import MetaData as SAMetaData

        import src.api.rest.cell_migration as module

        declared: set[str] = set()
        for value in vars(module).values():
            if isinstance(value, SAMetaData):
                declared |= set(value.tables)
        leaked = declared & set(cm.RUNTIME_TABLES)
        assert not leaked, f"runtime-owned tables declared here: {sorted(leaked)}"

    def test_every_phase_delegates_to_the_runtime(self, client, runtime):
        """All four phases, so none can silently skip the runtime's half."""
        export = _export(client).json()
        client.post("/internal/cell-migration/t1/import", json=export)
        client.get("/internal/cell-migration/t1/verify", params={"user_ids": "alice"})
        client.post("/internal/cell-migration/t1/purge", params={"user_ids": "alice"})

        delegated = {action for action, _ in runtime.calls}
        assert delegated == {"export", "import", "verify", "purge"}
        assert all(tenant == "t1" for _, tenant in runtime.calls)

    def test_runtime_rows_travel_in_the_bundle(self, client):
        """The operator tool sees ONE export covering all nine tables."""
        body = _export(client).json()
        for name in cm.RUNTIME_TABLES:
            assert name in body["tables"], f"{name} missing from the bundle"
        assert [d["id"] for d in body["tables"]["knowledge_documents"]] == ["sales.md"]
        assert body["tables"]["file_drafts"][0]["content"] == "SELECT 1"

    def test_an_unreachable_runtime_fails_instead_of_reporting_zero(
        self, client, runtime
    ):
        """The most dangerous failure mode this split introduces.

        If a failed delegation degraded to "no rows", export would produce an
        incomplete bundle and the later purge would erase rows that were never
        copied. Every phase must refuse instead.
        """
        runtime.unavailable = True

        assert _export(client).status_code == 503
        assert (
            client.get(
                "/internal/cell-migration/t1/verify", params={"user_ids": "alice"}
            ).status_code
            == 503
        )
        assert (
            client.post(
                "/internal/cell-migration/t1/purge", params={"user_ids": "alice"}
            ).status_code
            == 503
        )

    def test_purge_keeps_control_rows_when_the_runtime_half_fails(
        self, client, runtime
    ):
        """A partial cleanup must stay retryable.

        The runtime is purged first on purpose: if it fails, the registry that
        says which working copies the leftovers belong to is still there.
        """
        runtime.unavailable = True
        client.post("/internal/cell-migration/t1/purge", params={"user_ids": "alice"})

        runtime.unavailable = False
        surviving = client.get(
            "/internal/cell-migration/t1/verify", params={"user_ids": "alice"}
        ).json()
        assert surviving["counts"]["workspaces"] == 1
        assert surviving["counts"]["knowledge_documents"] == 1
