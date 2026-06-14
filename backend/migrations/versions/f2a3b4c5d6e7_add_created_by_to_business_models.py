"""add created_by to business models for ownership scoping

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-06-12

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add created_by FK column to user-creatable business objects."""
    for table in [
        "projects",
        "agents",
        "gateways",
        "tags",
        "workspaces",
        "approvals",
    ]:
        op.add_column(
            table,
            sa.Column("created_by", sa.UUID(), nullable=True),
        )
        op.create_foreign_key(
            f"fk_{table}_created_by_users",
            table,
            "users",
            ["created_by"],
            ["id"],
        )
        op.create_index(
            f"ix_{table}_created_by",
            table,
            ["created_by"],
        )


def downgrade() -> None:
    """Remove created_by columns from business models."""
    for table in [
        "approvals",
        "workspaces",
        "tags",
        "gateways",
        "agents",
        "projects",
    ]:
        op.drop_index(f"ix_{table}_created_by", table_name=table)
        op.drop_constraint(f"fk_{table}_created_by_users", table, type_="foreignkey")
        op.drop_column(table, "created_by")
