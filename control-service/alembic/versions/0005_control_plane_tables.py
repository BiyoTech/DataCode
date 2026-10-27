"""Control-plane bookkeeping tables (design 06 P5).

Four tables land with the control-service extraction:

* ``tenant_quotas``      — per-tenant resource limits (enforcement: P8);
* ``tenant_usage``       — per-tenant live counters those limits compare against;
* ``task_records``       — cross-service archive of long-running operations
                           (the runtime's tracker is per-process by design);
* ``lease_history``      — append-only audit of lease placement decisions.

Like every step in this chain the migration is IDEMPOTENT: a database
bootstrapped through ``create_all`` already carries the tables, so each one is
inspected before it is created. All timestamps are stored in UTC.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0005_control_plane_tables"
down_revision: Union[str, None] = "0004_environment_applied_dirty"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    """Whether the live schema currently carries ``name``."""
    return inspect(op.get_bind()).has_table(name)


def _has_index(table: str, index: str) -> bool:
    """Whether ``table`` currently carries ``index``."""
    bind = op.get_bind()
    if not inspect(bind).has_table(table):
        return False
    return index in {item["name"] for item in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    if not _has_table("tenant_quotas"):
        op.create_table(
            "tenant_quotas",
            sa.Column("tenant_id", sa.String(length=64), primary_key=True),
            sa.Column("max_concurrent_leases", sa.Integer(), nullable=False, server_default="64"),
            sa.Column("max_environments", sa.Integer(), nullable=False, server_default="500"),
            sa.Column("max_workspaces", sa.Integer(), nullable=False, server_default="50"),
            sa.Column("max_runtime_replicas", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not _has_table("tenant_usage"):
        op.create_table(
            "tenant_usage",
            sa.Column("tenant_id", sa.String(length=64), primary_key=True),
            sa.Column("live_environments", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("live_workspaces", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("concurrent_leases", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("peak_concurrent_leases", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not _has_table("task_records"):
        op.create_table(
            "task_records",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("workspace_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("environment", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("runtime_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("actor_user_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("result_json", sa.Text(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        if not _has_index("task_records", "idx_task_records_workspace"):
            op.create_index(
                "idx_task_records_workspace", "task_records", ["workspace_id", "created_at"]
            )
        if not _has_index("task_records", "idx_task_records_status"):
            op.create_index(
                "idx_task_records_status", "task_records", ["status", "created_at"]
            )

    if not _has_table("lease_history"):
        op.create_table(
            "lease_history",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("lease_key", sa.String(length=320), nullable=False),
            sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("workspace_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("user_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("environment", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("runtime_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("action", sa.String(length=16), nullable=False),
            sa.Column("reason", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        )
        if not _has_index("lease_history", "idx_lease_history_key"):
            op.create_index(
                "idx_lease_history_key", "lease_history", ["lease_key", "occurred_at"]
            )
        if not _has_index("lease_history", "idx_lease_history_tenant"):
            op.create_index(
                "idx_lease_history_tenant", "lease_history", ["tenant_id", "occurred_at"]
            )


def downgrade() -> None:
    """Drop the control-plane tables (data loss is accepted on downgrade)."""
    op.drop_table("lease_history")
    op.drop_table("task_records")
    op.drop_table("tenant_usage")
    op.drop_table("tenant_quotas")
