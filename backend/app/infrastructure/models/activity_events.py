"""Activity event model persisted for audit and feed use-cases."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column
from sqlmodel import Field

from app.shared.time import utcnow
from app.infrastructure.models.base import QueryModel

RUNTIME_ANNOTATION_TYPES = (datetime,)


class ActivityEvent(QueryModel, table=True):
    """Discrete activity event tied to project/task/agent context."""

    __tablename__ = "activity_events"  # pyright: ignore[reportAssignmentType]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_type: str = Field(index=True)
    message: str | None = None
    payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    agent_id: UUID | None = Field(default=None, foreign_key="agents.id", index=True)
    task_id: UUID | None = Field(default=None, foreign_key="tasks.id", index=True)
    project_id: UUID | None = Field(default=None, foreign_key="projects.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
