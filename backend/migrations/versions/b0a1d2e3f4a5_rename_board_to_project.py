"""rename board tables and columns to project/workspace

Revision ID: b0a1d2e3f4a5
Revises:
Create Date: 2026-06-12

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "b0a1d2e3f4a5"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Rename board* tables/columns to project/workspace naming."""

    # --- Table renames ---
    op.rename_table("boards", "projects")
    op.rename_table("board_groups", "workspaces")
    op.rename_table("board_memory", "project_memory")
    op.rename_table("board_group_memory", "workspace_memory")
    op.rename_table("board_webhooks", "project_webhooks")
    op.rename_table("board_webhook_payloads", "project_webhook_payloads")
    op.rename_table("board_onboarding_sessions", "project_onboarding_sessions")
    op.rename_table("board_task_custom_fields", "project_task_custom_fields")
    op.rename_table("organization_board_access", "organization_project_access")
    op.rename_table("organization_invite_board_access", "organization_invite_project_access")

    # --- Column renames: board_id → project_id ---
    # (on tables that reference boards)
    _rename_col("tasks", "board_id", "project_id")
    _rename_col("agents", "board_id", "project_id")
    _rename_col("approvals", "board_id", "project_id")
    _rename_col("activity_events", "board_id", "project_id")
    _rename_col("task_dependencies", "board_id", "project_id")
    _rename_col("task_fingerprints", "board_id", "project_id")
    _rename_col("project_memory", "board_id", "project_id")
    _rename_col("project_webhooks", "board_id", "project_id")
    _rename_col("project_webhook_payloads", "board_id", "project_id")
    _rename_col("project_onboarding_sessions", "board_id", "project_id")
    _rename_col("organization_project_access", "board_id", "project_id")
    _rename_col("organization_invite_project_access", "board_id", "project_id")

    # --- Column renames: board_group_id → workspace_id ---
    _rename_col("projects", "board_group_id", "workspace_id")
    _rename_col("workspace_memory", "board_group_id", "workspace_id")

    # --- Column renames: is_board_lead → is_project_lead ---
    _rename_col("agents", "is_board_lead", "is_project_lead")

    # --- Column renames: board_type → project_type ---
    _rename_col("projects", "board_type", "project_type")

    # --- Rename unique constraints ---
    op.drop_constraint("uq_org_board_access_member_board", "organization_project_access")
    op.create_unique_constraint(
        "uq_org_project_access_member_project",
        "organization_project_access",
        ["organization_member_id", "project_id"],
    )
    op.drop_constraint("uq_org_invite_board_access_invite_board", "organization_invite_project_access")
    op.create_unique_constraint(
        "uq_org_invite_project_access_invite_project",
        "organization_invite_project_access",
        ["organization_invite_id", "project_id"],
    )

    # --- Rename FK on task_custom_fields ---
    _rename_col("project_task_custom_fields", "board_id", "project_id")


def downgrade() -> None:
    """Reverse: project/workspace → board naming."""

    # --- Column renames back ---
    _rename_col("project_task_custom_fields", "project_id", "board_id")

    op.drop_constraint("uq_org_project_access_member_project", "organization_project_access")
    op.create_unique_constraint(
        "uq_org_board_access_member_board",
        "organization_project_access",
        ["organization_member_id", "project_id"],
    )
    op.drop_constraint("uq_org_invite_project_access_invite_project", "organization_invite_project_access")
    op.create_unique_constraint(
        "uq_org_invite_board_access_invite_board",
        "organization_invite_project_access",
        ["organization_invite_id", "project_id"],
    )

    _rename_col("projects", "project_type", "board_type")
    _rename_col("agents", "is_project_lead", "is_board_lead")
    _rename_col("workspace_memory", "workspace_id", "board_group_id")
    _rename_col("projects", "workspace_id", "board_group_id")

    for table in [
        "organization_invite_project_access",
        "organization_project_access",
        "project_onboarding_sessions",
        "project_webhook_payloads",
        "project_webhooks",
        "project_memory",
        "task_fingerprints",
        "task_dependencies",
        "activity_events",
        "approvals",
        "agents",
        "tasks",
    ]:
        _rename_col(table, "project_id", "board_id")

    op.rename_table("organization_invite_project_access", "organization_invite_board_access")
    op.rename_table("organization_project_access", "organization_board_access")
    op.rename_table("project_task_custom_fields", "board_task_custom_fields")
    op.rename_table("project_onboarding_sessions", "board_onboarding_sessions")
    op.rename_table("project_webhook_payloads", "board_webhook_payloads")
    op.rename_table("project_webhooks", "board_webhooks")
    op.rename_table("workspace_memory", "board_group_memory")
    op.rename_table("project_memory", "board_memory")
    op.rename_table("workspaces", "board_groups")
    op.rename_table("projects", "boards")


def _rename_col(table: str, old: str, new: str) -> None:
    """Rename a column if it exists (safe for re-runs)."""
    op.alter_column(table, old, new_column_name=new)
