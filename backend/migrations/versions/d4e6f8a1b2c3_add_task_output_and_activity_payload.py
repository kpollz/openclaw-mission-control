"""Add task output gates and structured activity payloads."""

from __future__ import annotations

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "d4e6f8a1b2c3"
down_revision = "a9b1c2d3e4f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply schema changes."""
    op.add_column("activity_events", sa.Column("payload", sa.JSON(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("status_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.add_column("tasks", sa.Column("completed_at", sa.DateTime(), nullable=True))
    op.add_column("tasks", sa.Column("created_by_agent_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_tasks_created_by_agent_id"),
        "tasks",
        ["created_by_agent_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_tasks_created_by_agent_id_agents",
        "tasks",
        "agents",
        ["created_by_agent_id"],
        ["id"],
    )
    op.add_column(
        "task_custom_field_definitions",
        sa.Column(
            "required_for_done",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("task_custom_field_definitions", "required_for_done", server_default=None)


def downgrade() -> None:
    """Revert schema changes."""
    op.drop_column("task_custom_field_definitions", "required_for_done")
    op.drop_constraint("fk_tasks_created_by_agent_id_agents", "tasks", type_="foreignkey")
    op.drop_index(op.f("ix_tasks_created_by_agent_id"), table_name="tasks")
    op.drop_column("tasks", "created_by_agent_id")
    op.drop_column("tasks", "completed_at")
    op.drop_column("tasks", "status_reason")
    op.drop_column("activity_events", "payload")
