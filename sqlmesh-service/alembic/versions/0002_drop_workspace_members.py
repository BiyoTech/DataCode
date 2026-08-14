"""Drop workspace_members: authorization moved to the gateway.

Workspace role bindings are now owned by the gateway, which is the platform's
single authorization center. Keeping a second copy of the membership here would
create two sources of truth, so the table is removed.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002_drop_workspace_members"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove the membership table if it is still present."""
    op.execute("DROP TABLE IF EXISTS workspace_members")


def downgrade() -> None:
    """Recreate the table shell so a rollback leaves a loadable schema.

    The rows are not restored: they live in the gateway database now.
    """
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_members (
            workspace_id VARCHAR(128) NOT NULL,
            user_id      VARCHAR(64)  NOT NULL,
            role         VARCHAR(32)  NOT NULL DEFAULT 'viewer',
            created_at   TIMESTAMP    NULL,
            PRIMARY KEY (workspace_id, user_id)
        )
        """
    )
