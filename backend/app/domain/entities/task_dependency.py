"""TaskDependency domain entity — pure business object with no ORM dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4


@dataclass
class TaskDependencyEntity:
    """Pure domain entity representing a directed dependency edge between tasks."""

    id: UUID = field(default_factory=uuid4)
    project_id: UUID | None = None
    task_id: UUID | None = None
    depends_on_task_id: UUID | None = None

    @classmethod
    def from_model(cls, model: object) -> TaskDependencyEntity:
        return cls(
            id=model.id,
            project_id=model.project_id,
            task_id=model.task_id,
            depends_on_task_id=model.depends_on_task_id,
        )
