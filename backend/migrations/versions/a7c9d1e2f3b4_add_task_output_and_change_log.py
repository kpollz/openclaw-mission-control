"""Add first-class task output and change log fields."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a7c9d1e2f3b4"
down_revision = "d4e6f8a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply schema changes."""
    op.add_column("tasks", sa.Column("output", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("change_log", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Revert schema changes."""
    op.drop_column("tasks", "change_log")
    op.drop_column("tasks", "output")
