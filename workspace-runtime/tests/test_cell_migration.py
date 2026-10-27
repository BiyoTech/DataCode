"""Cross-cell migration for the tables this service owns (design 06 P7).

Three properties are pinned here.

**Ownership.** This service moves ``knowledge_documents`` / ``file_drafts`` /
``plan_apply_history`` because it CREATES them. The control plane used to mirror
them and call ``create_all`` during import, which made it a second creator for
tables whose owner already creates them (concurrent ``CREATE TABLE IF NOT
EXISTS`` can fail inside PostgreSQL's catalog) and left a mirrored column free to
drift — the mirror declared ``content`` as plain ``Text`` while the real column is
``MEDIUMTEXT`` on MySQL, capping drafts at 64 KiB instead of 16 MiB.

**Scope.** The trailing slash after the tenant segment is what keeps tenant
``t1`` from matching tenant ``t10``'s rows. Export, import-replacement, verify and
purge all use one filter, so "what moved" and "what is erased" cannot diverge.

**Import refuses to create tables.** A cell where this service has not started
has no tables, and import says so instead of creating them — creating them here
is exactly the second-creator race the ownership rule exists to prevent.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlalchemy.dialects.mysql as _mysql_dialect
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, select, text

import src.workspace.database as database

BASE = "/workspaces"
T1_PATH = f"{BASE}/t1/ws-t1-shop/alice/envs/dev"
# A tenant whose id is a PREFIX of t1: the control group for the trailing-slash
# rule. Without it, t1's filter would sweep in (and later erase) these rows.
T10_PATH = f"{BASE}/t10/ws-t10-other/bob/envs/dev"
TOKEN = "runtime-migration-test-token"

NOW = datetime.now(timezone.utc)


@pytest.fixture()
async def migration_db(tmp_path, monkeypatch):
    """A throwaway SQLite database holding this service's owned tables."""
    from src.config import settings

    url = f"sqlite+aiosqlite:///{tmp_path / 'runtime_migration.db'}"
    # Drop any engine a previous test built; monkeypatch restores the
    # URL factory afterwards, so the next caller rebuilds from settings.
    database.reset_engine()
    monkeypatch.setattr(database, "_build_database_url", lambda: url)
    monkeypatch.setattr(settings, "internal_token", TOKEN)
    monkeypatch.setattr(settings, "workspace_base_path", BASE)

    from src.workspace.models import (
        Base,
        FileDraftModel,
        KnowledgeDocumentModel,
        PlanApplyHistoryModel,
    )

    engine = database.get_engine()
    owned = [
        KnowledgeDocumentModel.__table__,
        FileDraftModel.__table__,
        PlanApplyHistoryModel.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=owned)

    async with database.get_session() as session:
        await session.execute(
            insert(KnowledgeDocumentModel.__table__).values(
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
            insert(FileDraftModel.__table__).values(
                id="draft-1",
                workspace_path=T1_PATH,
                file_path="models/stg.sql",
                content="SELECT 1",
                updated_at=NOW,
            )
        )
        await session.execute(
            insert(PlanApplyHistoryModel.__table__).values(
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
        # Control group: the prefix-collision tenant.
        await session.execute(
            insert(KnowledgeDocumentModel.__table__).values(
                id="other.md",
                workspace_path=T10_PATH,
                title="Other tenant",
                filename="other.md",
                content_type="markdown",
                file_size=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.commit()

    yield
    await database.close_db()


@pytest.fixture()
async def client(migration_db):
    from src.main import app

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://runtime",
        headers={"X-Internal-Token": TOKEN},
    ) as api:
        yield api


def _url(tenant: str, action: str) -> str:
    return f"/internal/cell-migration/{tenant}/{action}"


class TestScope:
    async def test_exports_only_the_tenants_rows(self, client):
        response = await client.get(_url("t1", "export"))
        assert response.status_code == 200
        body = response.json()

        assert body["tenant_id"] == "t1"
        assert [d["id"] for d in body["tables"]["knowledge_documents"]] == ["sales.md"]
        assert body["tables"]["file_drafts"][0]["content"] == "SELECT 1"
        assert body["tables"]["plan_apply_history"][0]["added_count"] == 2

    async def test_prefix_collision_tenant_is_not_swept_in(self, client):
        """t10's rows must not match t1's filter — the trailing-slash rule."""
        body = (await client.get(_url("t1", "export"))).json()
        paths = [d["workspace_path"] for d in body["tables"]["knowledge_documents"]]
        assert paths == [T1_PATH]

        counts = (await client.get(_url("t10", "verify"))).json()["counts"]
        assert counts["knowledge_documents"] == 1  # its own row, not t1's

    async def test_dangerous_tenant_ids_are_rejected(self, client):
        assert (await client.get(_url("t1..x", "export"))).status_code == 400
        # A slash never reaches the handler: route matching refuses it first.
        assert (await client.get(_url("t1/..", "export"))).status_code == 404


class TestAuth:
    async def test_unauthenticated_is_refused(self, migration_db):
        from src.main import app

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://runtime") as api:
            response = await api.get(_url("t1", "export"))
        assert response.status_code == 403

    async def test_wrong_token_is_refused(self, migration_db):
        from src.main import app

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://runtime",
            headers={"X-Internal-Token": "nope"},
        ) as api:
            response = await api.get(_url("t1", "export"))
        assert response.status_code == 403


class TestRoundTrip:
    async def test_export_purge_import_verify(self, client):
        export = (await client.get(_url("t1", "export"))).json()
        source = {name: len(rows) for name, rows in export["tables"].items()}

        purged = await client.post(_url("t1", "purge"))
        assert purged.status_code == 200
        assert purged.json()["deleted"]["knowledge_documents"] == 1

        wiped = (await client.get(_url("t1", "verify"))).json()["counts"]
        assert set(wiped.values()) == {0}

        imported = await client.post(_url("t1", "import"), json=export)
        assert imported.status_code == 200

        verified = (await client.get(_url("t1", "verify"))).json()["counts"]
        assert verified == source

    async def test_values_survive_the_json_round_trip(self, client):
        """Timestamps included: they are the part a naive copy loses."""
        export = (await client.get(_url("t1", "export"))).json()
        await client.post(_url("t1", "purge"))
        await client.post(_url("t1", "import"), json=export)

        from src.workspace.models import FileDraftModel

        async with database.get_session() as session:
            row = (
                await session.execute(
                    select(FileDraftModel).where(FileDraftModel.id == "draft-1")
                )
            ).scalar_one()
        assert row.content == "SELECT 1"
        assert row.updated_at is not None
        assert row.workspace_path == T1_PATH

    async def test_import_is_idempotent(self, client):
        export = (await client.get(_url("t1", "export"))).json()

        first = await client.post(_url("t1", "import"), json=export)
        second = await client.post(_url("t1", "import"), json=export)

        assert first.json()["counts"] == second.json()["counts"]
        verified = (await client.get(_url("t1", "verify"))).json()["counts"]
        assert verified["knowledge_documents"] == 1  # not 2

    async def test_import_rejects_a_foreign_bundle(self, client):
        export = (await client.get(_url("t1", "export"))).json()
        export["tenant_id"] = "t10"

        response = await client.post(_url("t1", "import"), json=export)
        assert response.status_code == 400

    async def test_the_control_group_survives_every_phase(self, client):
        from src.workspace.models import KnowledgeDocumentModel

        async def _control_rows() -> int:
            async with database.get_session() as session:
                rows = (
                    await session.execute(
                        select(KnowledgeDocumentModel.id).where(
                            KnowledgeDocumentModel.workspace_path == T10_PATH
                        )
                    )
                ).fetchall()
            return len(rows)

        before = await _control_rows()
        export = (await client.get(_url("t1", "export"))).json()
        await client.post(_url("t1", "purge"))
        await client.post(_url("t1", "import"), json=export)
        await client.post(_url("t1", "purge"))

        assert await _control_rows() == before


class TestTableOwnership:
    async def test_missing_tables_export_as_zero_rows(self, client):
        """Migrating out of a half-provisioned cell is legitimate."""
        engine = database.get_engine()
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE file_drafts"))

        body = (await client.get(_url("t1", "export"))).json()
        assert body["tables"]["file_drafts"] == []
        assert body["tables"]["knowledge_documents"]  # the rest still moves

        counts = (await client.get(_url("t1", "verify"))).json()["counts"]
        assert counts["file_drafts"] == 0

    async def test_import_refuses_to_create_missing_tables(self, client):
        """The invariant: one creator per table, and it is init_db.

        Creating a table here would reintroduce the concurrent-DDL race the
        ownership rule exists to prevent, so a cell whose runtime has not started
        must be reported rather than repaired from a migration payload.
        """
        export = (await client.get(_url("t1", "export"))).json()

        engine = database.get_engine()
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE file_drafts"))

        response = await client.post(_url("t1", "import"), json=export)
        assert response.status_code == 503
        # This service's handler renders a string detail as "message".
        assert "file_drafts" in response.json()["message"]

        # And it really did not create it.
        async with database.get_session() as session:
            conn = await session.connection()
            names = await conn.run_sync(
                lambda sync_conn: set(sync_conn.dialect.get_table_names(sync_conn))
            )
        assert "file_drafts" not in names


async def test_the_large_draft_column_keeps_its_mysql_variant():
    """``file_drafts.content`` must compile to MEDIUMTEXT on MySQL.

    This is the column the deleted control-plane mirror used to get wrong: its
    plain ``Text`` compiled to 64 KiB on MySQL while the real column allows
    16 MiB, so a cell built from the mirror truncated large drafts during the
    very migration meant to preserve them. The mirror is gone, and this pins
    the one remaining definition so the same silent downgrade cannot come back
    through a well-intentioned "simplification".
    """
    from src.workspace.models import FileDraftModel

    compiled = FileDraftModel.__table__.c.content.type.compile(
        dialect=_mysql_dialect.dialect()
    )
    assert compiled.upper().startswith("MEDIUMTEXT"), (
        f"file_drafts.content compiles to {compiled!r} on MySQL; a downgrade "
        "from MEDIUMTEXT silently truncates large drafts at 64 KiB"
    )
