"""Project domain entity — pure business object with no ORM dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class ProjectType(StrEnum):
    """Project type classification."""

    GOAL = "goal"
    KANBAN = "kanban"


@dataclass
class ProjectEntity:
    """Pure domain entity representing an organization workspace."""

    id: UUID = field(default_factory=uuid4)
    organization_id: UUID | None = None
    name: str = ""
    slug: str = ""
    description: str = ""
    gateway_id: UUID | None = None
    project_type: ProjectType = ProjectType.GOAL
    objective: str | None = None
    success_metrics: dict[str, Any] | None = None
    target_date: datetime | None = None
    goal_confirmed: bool = False
    goal_source: str | None = None
    require_approval_for_done: bool = True
    require_review_before_done: bool = False
    comment_required_for_review: bool = False
    block_status_changes_with_pending_approval: bool = False
    only_lead_can_change_status: bool = False
    max_agents: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_model(cls, model: object) -> ProjectEntity:
        """Map an ORM model to a pure domain entity."""
        return cls(
            id=model.id,
            organization_id=model.organization_id,
            name=model.name,
            slug=model.slug,
            description=model.description,
            gateway_id=model.gateway_id,
            project_type=ProjectType(model.project_type),
            objective=model.objective,
            success_metrics=model.success_metrics,
            target_date=model.target_date,
            goal_confirmed=model.goal_confirmed,
            goal_source=model.goal_source,
            require_approval_for_done=model.require_approval_for_done,
            require_review_before_done=model.require_review_before_done,
            comment_required_for_review=model.comment_required_for_review,
            block_status_changes_with_pending_approval=model.block_status_changes_with_pending_approval,
            only_lead_can_change_status=model.only_lead_can_change_status,
            max_agents=model.max_agents,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    def apply_to_model(self, model: object) -> None:
        """Write domain entity fields back onto an ORM model instance."""
        model.name = self.name
        model.slug = self.slug
        model.description = self.description
        model.gateway_id = self.gateway_id
        model.project_type = str(self.project_type)
        model.objective = self.objective
        model.success_metrics = self.success_metrics
        model.target_date = self.target_date
        model.goal_confirmed = self.goal_confirmed
        model.require_approval_for_done = self.require_approval_for_done
        model.require_review_before_done = self.require_review_before_done
        model.comment_required_for_review = self.comment_required_for_review
        model.block_status_changes_with_pending_approval = self.block_status_changes_with_pending_approval
        model.only_lead_can_change_status = self.only_lead_can_change_status
        model.max_agents = self.max_agents
