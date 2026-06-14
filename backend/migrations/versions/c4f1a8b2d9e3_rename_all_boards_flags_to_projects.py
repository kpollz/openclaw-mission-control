"""rename all_boards_* access flags to all_projects_* and merge heads

Completes the board→project rename: the original rename migration
(b0a1d2e3f4a5) renamed every board table/column except the
``all_boards_read``/``all_boards_write`` access flags on
``organization_members`` and ``organization_invites``.  This migration renames
those columns and merges the two outstanding migration heads.

Revision ID: c4f1a8b2d9e3
Revises: a7c9d1e2f3b4, f2a3b4c5d6e7
Create Date: 2026-06-12
"""

from __future__ import annotations

from alembic import op

revision = "c4f1a8b2d9e3"
down_revision = ("a7c9d1e2f3b4", "f2a3b4c5d6e7")
branch_labels = None
depends_on = None


def _rename_col(table: str, old: str, new: str) -> None:
    op.alter_column(table, old, new_column_name=new)


def upgrade() -> None:
    _rename_col("organization_members", "all_boards_read", "all_projects_read")
    _rename_col("organization_members", "all_boards_write", "all_projects_write")
    _rename_col("organization_invites", "all_boards_read", "all_projects_read")
    _rename_col("organization_invites", "all_boards_write", "all_projects_write")


def downgrade() -> None:
    _rename_col("organization_members", "all_projects_read", "all_boards_read")
    _rename_col("organization_members", "all_projects_write", "all_boards_write")
    _rename_col("organization_invites", "all_projects_read", "all_boards_read")
    _rename_col("organization_invites", "all_projects_write", "all_boards_write")
