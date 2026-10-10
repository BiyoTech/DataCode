"""Record whether an apply ran from an uncommitted working copy.

``last_applied_commit`` alone cannot express "this data was produced by code
that is not in Git": recording the HEAD of a dirty working copy would report
the environment as in sync with a commit whose content never ran, forging the
"code and data share one source" invariant the platform is built on.

Applying from a dirty tree is a legitimate, high-frequency action (a sandbox
exists precisely to iterate before committing), so it is recorded rather than
rejected: ``last_applied_dirty`` makes the state observable, and the UI reports
it as drift that a commit + re-apply resolves.

Existing rows default to 0: an apply performed before this column existed can
no longer be classified, and the optimistic assumption matches the previous
behaviour of the UI.

The column is added only when missing: a database bootstrapped via
``create_all`` (``AUTO_MIGRATE`` unset) already carries it — albeit without the
server default, which is why the backfill below re-asserts 0 for legacy NULLs.

Handed over verbatim from the legacy sqlmesh-service chain (P5).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0004_environment_applied_dirty"
down_revision: Union[str, None] = "0003_environments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    """Column names of ``table`` right now."""
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    """Add ``environments.last_applied_dirty`` when it is not there yet."""
    if "last_applied_dirty" in _columns("environments"):
        return
    with op.batch_alter_table("environments") as batch:
        batch.add_column(
            sa.Column(
                "last_applied_dirty",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    """Drop ``environments.last_applied_dirty`` when it is still there."""
    if "last_applied_dirty" not in _columns("environments"):
        return
    with op.batch_alter_table("environments") as batch:
        batch.drop_column("last_applied_dirty")
