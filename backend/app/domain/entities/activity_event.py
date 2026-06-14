"""ActivityEvent domain entity — pure business object with no ORM dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4


@dataclass
class ActivityEventEntity:
    """Pure domain entity representing an audit activity event."""

    id: UUID = field(default_factory=uuid4)
    event_type: str = ""
    message: str = ""
    payload: dict[str, Any] | None = None
    agent_id: UUID | None = None
    task_id: UUID | None = None
    project_id: UUID | None = None
    user_id: UUID | None = None
    organization_id: UUID | None = None
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, model: object) -> ActivityEventEntity:
        return cls(
            id=model.id,
            event_type=model.event_type,
            message=model.message,
            payload=model.payload,
            agent_id=model.agent_id,
            task_id=model.task_id,
            project_id=model.project_id,
            user_id=getattr(model, "user_id", None),
            organization_id=getattr(model, "organization_id", None),
            created_at=model.created_at,
        )
