"""add password auth fields to users

Revision ID: e1f2a3b4c5d6
Revises: b0a1d2e3f4a5
Create Date: 2026-06-12

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "e1f2a3b4c5d6"
down_revision = "b0a1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add password auth fields to users table."""
    op.add_column("users", sa.Column("password_hash", sa.String(), nullable=True))
    op.add_column("users", sa.Column("auth_provider", sa.String(), server_default="local", nullable=False))
    op.add_column("users", sa.Column("email_verified", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("users", sa.Column("created_by", sa.UUID(), nullable=True))
    op.add_column("users", sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True))

    # Make clerk_user_id nullable for password-auth users (they won't have a Clerk ID)
    op.alter_column("users", "clerk_user_id", existing_type=sa.String(), nullable=True)
    # Set empty string to NULL for existing rows so unique constraint works
    op.execute("UPDATE users SET clerk_user_id = NULL WHERE clerk_user_id = ''")


def downgrade() -> None:
    """Remove password auth fields from users table."""
    # Restore empty string for NULL clerk_user_id
    op.execute("UPDATE users SET clerk_user_id = '' WHERE clerk_user_id IS NULL")
    op.alter_column("users", "clerk_user_id", existing_type=sa.String(), nullable=False)

    op.drop_column("users", "created_at")
    op.drop_column("users", "created_by")
    op.drop_column("users", "email_verified")
    op.drop_column("users", "auth_provider")
    op.drop_column("users", "password_hash")
