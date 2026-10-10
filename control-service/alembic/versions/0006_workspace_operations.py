"""``workspace_operations`` — persisted progress of workspace create/delete.

Idempotent like every step of this chain: a database bootstrapped by
``create_all`` already carries the table, so it is inspected first. All
timestamps are UTC.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0006_workspace_operations"
down_revision: Union[str, None] = "0005_control_plane_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "workspace_operations"


def _has_index(index: str) -> bool:
    bind = op.get_bind()
    if not inspect(bind).has_table(_TABLE):
        return False
    return index in {item["name"] for item in inspect(bind).get_indexes(_TABLE)}


def upgrade() -> None:
    if not inspect(op.get_bind()).has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("op_id", sa.String(length=64), primary_key=True),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=False),
            sa.Column("tenant_id", sa.String(length=64), nullable=False),
            sa.Column("actor_user_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("step", sa.String(length=32), nullable=False, server_default="begun"),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if not _has_index("idx_workspace_operations_status"):
        op.create_index(
            "idx_workspace_operations_status", _TABLE, ["status", "updated_at"]
        )
    if not _has_index("idx_workspace_operations_workspace"):
        op.create_index(
            "idx_workspace_operations_workspace", _TABLE, ["workspace_id", "created_at"]
        )


def downgrade() -> None:
    if inspect(op.get_bind()).has_table(_TABLE):
        op.drop_table(_TABLE)
