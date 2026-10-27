"""Environment registry: environments + environment_events, workspace env columns.

Introduces the platform Environment as a first-class entity. Consequences for
the existing schema:

* ``workspaces.git_branch`` is dropped: a branch belongs to an environment now,
  not to a workspace.
* ``workspaces.production_env`` / ``default_env`` are added so a workspace names
  its production environment and the one members land on.
* every existing workspace is backfilled with its production environment, so it
  stays openable after the upgrade.

The migration is IDEMPOTENT and DIALECT-PORTABLE on purpose:

* A deployment that boots with ``AUTO_MIGRATE`` unset initializes its schema
  with ``create_all``, which creates missing tables but never alters existing
  ones. Such a database already carries the new tables (and any columns
  ``create_all`` could add) when alembic later runs from the empty baseline,
  so every step below inspects the live schema before touching it.
* The backfill runs row-by-row from Python instead of ``INSERT ... SELECT``:
  ``||`` concatenates strings on SQLite/PostgreSQL but is logical OR on MySQL,
  and one portable implementation beats dialect-branched SQL.

On-disk working copies are NOT migrated: they are disposable and re-materialized
as Git worktrees on the next open.

Handed over verbatim from the legacy sqlmesh-service chain (P5): same revision
ids, same shared database, so a deployment already at head keeps its history.
"""

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from datacode_common.naming import (
    ENV_TYPE_PRODUCTION,
    PRODUCTION_ENV,
    STATUS_ACTIVE,
    branch_of,
    deployment_name,
    environment_id,
)
from sqlalchemy import inspect

from alembic import op

revision: str = "0003_environments"
down_revision: Union[str, None] = "0002_drop_workspace_members"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    """Whether the live schema currently carries ``name``."""
    return inspect(op.get_bind()).has_table(name)


def _columns(table: str) -> set[str]:
    """Column names of ``table`` right now (empty set when the table is absent)."""
    bind = op.get_bind()
    if not inspect(bind).has_table(table):
        return set()
    return {column["name"] for column in inspect(bind).get_columns(table)}


def upgrade() -> None:
    """Create the registry tables and re-shape the workspace columns."""
    if not _has_table("environments"):
        op.create_table(
            "environments",
            sa.Column("id", sa.String(length=128), primary_key=True),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("tenant_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=40), nullable=False),
            sa.Column("branch", sa.String(length=128), nullable=False),
            sa.Column("env_type", sa.String(length=16), nullable=False, server_default="sandbox"),
            sa.Column("base_env", sa.String(length=40), nullable=False, server_default="prod"),
            sa.Column("owner_user_id", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="creating"),
            sa.Column("deployment_name", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("schedules_enabled", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_applied_commit", sa.String(length=64), nullable=True),
            sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_promoted_to", sa.String(length=40), nullable=True),
            sa.Column("ttl_hours", sa.Integer(), nullable=False, server_default="168"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("workspace_id", "name", name="uq_env_workspace_name"),
            sa.UniqueConstraint("workspace_id", "branch", name="uq_env_workspace_branch"),
        )
        op.create_index(
            "idx_env_workspace_status", "environments", ["workspace_id", "status"]
        )
        op.create_index("idx_env_expiry", "environments", ["status", "expires_at"])

    if not _has_table("environment_events"):
        op.create_table(
            "environment_events",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("environment_id", sa.String(length=128), nullable=False),
            sa.Column("action", sa.String(length=16), nullable=False),
            sa.Column("actor_user_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("trigger_type", sa.String(length=16), nullable=False, server_default="human"),
            sa.Column("commit_hash", sa.String(length=64), nullable=True),
            sa.Column("succeeded", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("detail_json", sa.Text(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "idx_env_event_env", "environment_events", ["environment_id", "created_at"]
        )

    # The chain evolves an existing schema, it never bootstraps one: base
    # tables are owned by the ORM metadata (0001 is an empty baseline), so the
    # workspace reshaping and its backfill only run when the table is there.
    workspace_columns = _columns("workspaces")
    if workspace_columns:
        if "production_env" not in workspace_columns:
            with op.batch_alter_table("workspaces") as batch:
                batch.add_column(
                    sa.Column(
                        "production_env",
                        sa.String(length=40),
                        nullable=False,
                        server_default="prod",
                    )
                )
        if "default_env" not in workspace_columns:
            with op.batch_alter_table("workspaces") as batch:
                batch.add_column(
                    sa.Column(
                        "default_env",
                        sa.String(length=40),
                        nullable=False,
                        server_default="prod",
                    )
                )

        _backfill_production_environments()

        # The check runs last and fresh: this is the one legacy column the
        # step removes, and the state above may have come from any path.
        if "git_branch" in _columns("workspaces"):
            with op.batch_alter_table("workspaces") as batch:
                batch.drop_column("git_branch")


def _backfill_production_environments() -> None:
    """Register the production environment of every workspace that lacks one.

    Mirrors ``EnvironmentRegistry.ensure_production`` so the backfilled rows are
    indistinguishable from the ones a fresh workspace creation writes: the name
    follows the workspace's own ``production_env`` (falling back to ``prod``),
    production is the only scheduled environment and never expires, and a
    workspace whose production row already exists is left untouched so the step
    is safe to re-run.
    """
    bind = op.get_bind()
    workspace_columns = _columns("workspaces")
    environment_columns = _columns("environments")

    selected = ["id", "tenant_id", "owner_user_id", "status", "created_at", "updated_at"]
    # ``production_env`` may just have been added above; ``git_branch`` is the
    # legacy shape. Read whichever the live table offers.
    for column in ("production_env", "git_branch"):
        if column in workspace_columns:
            selected.append(column)
    rows = bind.execute(
        sa.text(f"SELECT {', '.join(selected)} FROM workspaces")
    ).mappings().all()

    registered = {
        row_id for (row_id,) in bind.execute(sa.text("SELECT id FROM environments"))
    }

    # ``last_applied_dirty`` only exists once 0004 (or a ``create_all`` boot)
    # has provided it; the INSERT must match whichever shape is live.
    insert_columns = [
        "id",
        "workspace_id",
        "tenant_id",
        "name",
        "branch",
        "env_type",
        "base_env",
        "owner_user_id",
        "status",
        "deployment_name",
        "schedules_enabled",
        "ttl_hours",
        "created_at",
        "updated_at",
    ]
    if "last_applied_dirty" in environment_columns:
        insert_columns.append("last_applied_dirty")
    insert = sa.text(
        f"INSERT INTO environments ({', '.join(insert_columns)}) "
        f"VALUES ({', '.join(f':{column}' for column in insert_columns)})"
    )

    now = datetime.now(timezone.utc)
    inserted = 0
    for row in rows:
        if row["status"] == "deleted":
            continue
        name = row.get("production_env") or PRODUCTION_ENV
        env_id = environment_id(row["id"], name)
        if env_id in registered:
            continue
        values = {
            "id": env_id,
            "workspace_id": row["id"],
            "tenant_id": row["tenant_id"],
            "name": name,
            # A branch outside the platform namespace cannot back an
            # environment, so the branch is always derived from the name.
            "branch": branch_of(name),
            "env_type": ENV_TYPE_PRODUCTION,
            "base_env": name,
            "owner_user_id": row["owner_user_id"],
            "status": STATUS_ACTIVE,
            "deployment_name": deployment_name(row["id"], name),
            "schedules_enabled": 1,
            "ttl_hours": 0,
            "created_at": row["created_at"] or now,
            "updated_at": row["updated_at"] or now,
        }
        if "last_applied_dirty" in insert_columns:
            values["last_applied_dirty"] = 0
        bind.execute(insert, values)
        registered.add(env_id)
        inserted += 1

    if inserted:
        # Alembic migrations have no structured logger; the console output of
        # `alembic upgrade head` is where an operator looks for this number.
        print(f"backfilled {inserted} production environment(s)")


def downgrade() -> None:
    """Restore the workspace branch column and drop the registry tables."""
    # Snapshot before entering the batch: SQLite rebuilds the table only when
    # the batch context exits, so introspecting inside it would see stale state.
    workspace_columns = _columns("workspaces")
    with op.batch_alter_table("workspaces") as batch:
        if "git_branch" not in workspace_columns:
            batch.add_column(
                sa.Column(
                    "git_branch",
                    sa.String(length=64),
                    nullable=False,
                    server_default="main",
                )
            )
        if "default_env" in workspace_columns:
            batch.drop_column("default_env")
        if "production_env" in workspace_columns:
            batch.drop_column("production_env")

    op.drop_index("idx_env_event_env", table_name="environment_events")
    op.drop_table("environment_events")
    op.drop_index("idx_env_expiry", table_name="environments")
    op.drop_index("idx_env_workspace_status", table_name="environments")
    op.drop_table("environments")
