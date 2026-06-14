"""Task domain entity — pure business object with no ORM dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4


class TaskStatus(StrEnum):
    """Allowed task lifecycle states."""

    INBOX = "inbox"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"


class TaskPriority(StrEnum):
    """Task priority levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


# Terminal statuses that cannot transition further
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.DONE})

# Valid status transitions: from → set of allowed targets
VALID_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.INBOX: frozenset({TaskStatus.IN_PROGRESS, TaskStatus.REVIEW, TaskStatus.DONE}),
    TaskStatus.IN_PROGRESS: frozenset({TaskStatus.INBOX, TaskStatus.REVIEW, TaskStatus.DONE}),
    TaskStatus.REVIEW: frozenset({TaskStatus.INBOX, TaskStatus.IN_PROGRESS, TaskStatus.DONE}),
    TaskStatus.DONE: frozenset({TaskStatus.INBOX, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW}),
}


@dataclass
class TaskEntity:
    """Pure domain entity representing a board-scoped work item."""

    id: UUID = field(default_factory=uuid4)
    project_id: UUID | None = None
    title: str = ""
    description: str | None = None
    status: TaskStatus = TaskStatus.INBOX
    status_reason: str | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    due_at: datetime | None = None
    in_progress_at: datetime | None = None
    previous_in_progress_at: datetime | None = None
    completed_at: datetime | None = None
    created_by_user_id: UUID | None = None
    created_by_agent_id: UUID | None = None
    assigned_agent_id: UUID | None = None
    auto_created: bool = False
    auto_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def can_transition_to(self, target: TaskStatus) -> bool:
        """Check whether a transition to *target* is valid from current status."""
        allowed = VALID_TRANSITIONS.get(self.status, frozenset())
        return target in allowed

    def transition_to(self, target: TaskStatus, now: datetime) -> None:
        """Apply status transition and update timestamps."""
        self.status = target
        if target == TaskStatus.IN_PROGRESS:
            self.previous_in_progress_at = self.in_progress_at
            self.in_progress_at = now
        elif target == TaskStatus.DONE:
            self.completed_at = now

    @classmethod
    def from_model(cls, model: object) -> TaskEntity:
        """Map an ORM Task model to a pure domain entity."""
        return cls(
            id=model.id,
            project_id=model.project_id,
            title=model.title,
            description=model.description,
            status=TaskStatus(model.status),
            status_reason=model.status_reason,
            priority=TaskPriority(model.priority),
            due_at=model.due_at,
            in_progress_at=model.in_progress_at,
            previous_in_progress_at=model.previous_in_progress_at,
            completed_at=model.completed_at,
            created_by_user_id=model.created_by_user_id,
            created_by_agent_id=model.created_by_agent_id,
            assigned_agent_id=model.assigned_agent_id,
            auto_created=model.auto_created,
            auto_reason=model.auto_reason,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    def apply_to_model(self, model: object) -> None:
        """Write domain entity fields back onto an ORM model instance."""
        model.title = self.title
        model.description = self.description
        model.status = str(self.status)
        model.status_reason = self.status_reason
        model.priority = str(self.priority)
        model.due_at = self.due_at
        model.in_progress_at = self.in_progress_at
        model.previous_in_progress_at = self.previous_in_progress_at
        model.completed_at = self.completed_at
        model.assigned_agent_id = self.assigned_agent_id
