"""Initial schema — mirrors ORM models at introduction of Alembic."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Tables are created via ORM metadata on fresh installs; this revision
    # establishes the Alembic baseline for future incremental migrations.
    pass


def downgrade() -> None:
    pass
