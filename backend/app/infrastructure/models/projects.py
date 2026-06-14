"""Project model for organization workspaces and goal configuration."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column
from sqlmodel import Field

from app.shared.time import utcnow
from app.infrastructure.models.tenancy import TenantScoped

RUNTIME_ANNOTATION_TYPES = (datetime,)


class Project(TenantScoped, table=True):
    """Primary project entity grouping tasks, agents, and goal metadata."""

    __tablename__ = "projects"  # pyright: ignore[reportAssignmentType]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(foreign_key="organizations.id", index=True)
    name: str
    slug: str = Field(index=True)
    description: str = Field(default="")
    gateway_id: UUID | None = Field(default=None, foreign_key="gateways.id", index=True)
    project_type: str = Field(default="goal", index=True)
    objective: str | None = None
    success_metrics: dict[str, object] | None = Field(
        default=None,
        sa_column=Column(JSON),
    )
    target_date: datetime | None = None
    goal_confirmed: bool = Field(default=False)
    goal_source: str | None = None
    require_approval_for_done: bool = Field(default=True)
    require_review_before_done: bool = Field(default=False)
    comment_required_for_review: bool = Field(default=False)
    block_status_changes_with_pending_approval: bool = Field(default=False)
    only_lead_can_change_status: bool = Field(default=False)
    max_agents: int = Field(default=1)
    created_by: UUID | None = Field(default=None, foreign_key="users.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
